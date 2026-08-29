"""Full radio-free acceptance test over an interconnected audio channel.

This retains the public TCP command/data API, both StationServers,
ModemServices, Links, framing, modulation, demodulation and ARQ.  Only the
physical boundary is replaced: instead of sound cards, PTT and radios, each
transport puts its transmitted waveform into its peer's receive buffer.

Two sessions run here.  The first is the shipped CPFSK ladder.  The second
adds VF3 (`whale/modes/vf3_mode.py`) as a fourth rung, which is the test that
the `WaveformMode` boundary is real: a waveform sharing no DSP, no framing and
no synchronisation with CPFSK carries a session through the same link, ARQ
and TCP front end, with nothing above the mode changed to admit it.

Because the transports hand audio over instantly, wall-clock time here means
nothing.  What is measured instead is airtime -- the seconds of audio each
station actually transmitted -- which is the quantity a radio would spend and
the one GOALS.md asks to be competitive on.
"""

import socket
import threading

import numpy as np

from acceptance_test import StationClient
from whale import afsk, link, rx_audio
from whale.channel import (AwgnChannel, ChannelChain, ChannelResult,
                           ClippingChannel, DelayChannel, FilterChannel,
                           FrequencyOffsetChannel, IdentityChannel,
                           SampleClockChannel, SnrSpec)
from whale.fm_channel import ComplexFmChannel
from whale.link import Link
from whale.modes.hc0_mode import HC0
from whale.modes.hc1_mode import HC1
from whale.modes.vf3_mode import VF3
from whale.policy import HF_SSB, VHF_FM
from whale.service import ModemService
from whale.vara_server import StationServer


class PairedAudioTransport:
    """A radio-shaped endpoint connected through its outbound audio channel.

    Each endpoint owns the channel applied to what it transmits.  Thus the
    transport at station A holds A->B and the transport at station B holds
    B->A; asymmetric paths never share channel state or randomness.
    """

    def __init__(self, tx_channel=None):
        self.peer = None
        self.tx_channel = (IdentityChannel(rx_audio.CAPTURE_SAMPLE_RATE)
                           if tx_channel is None else tx_channel)
        if self.tx_channel.sample_rate != rx_audio.CAPTURE_SAMPLE_RATE:
            raise ValueError(
                "paired-audio channels must operate at the 48000 Hz capture rate")
        self._audio = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._transmitting = threading.Event()
        #: Results are retained for later trial reporting and channel
        #: diagnostics. One entry corresponds to each call to send().
        self.channel_results = []
        #: Seconds of audio this station has transmitted.  A real radio would
        #: be keyed for this long; wall-clock time here would not be.
        self.airtime = 0.0

    def start_receiving(self):
        pass

    def stop_receiving(self):
        pass

    def is_transmitting(self):
        return self._transmitting.is_set()

    def snapshot_rx(self):
        with self._lock:
            return self._audio.copy()

    def consume_rx(self, upto_sample):
        with self._lock:
            self._audio = self._audio[upto_sample:]

    def send(self, tx_audio, **kwargs):
        self._transmitting.set()
        try:
            waveform = np.asarray(tx_audio, dtype=np.float32)
            channel_result = self.tx_channel.process(waveform)
            channel_audio = np.asarray(channel_result.audio, dtype=np.float32)
            if channel_audio.ndim != 1:
                raise ValueError("channel returned non-mono audio")
            self.channel_results.append(channel_result)
            received = rx_audio.downsample(np.concatenate((
                channel_audio,
                np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32),
            )))
            with self.peer._lock:
                self.peer._audio = np.concatenate((self.peer._audio, received))
            keyed = len(waveform) / afsk.SAMPLE_RATE
            self.airtime += keyed
            return keyed
        finally:
            self._transmitting.clear()


def _server(transport, callsign, mode_registry=None, policy=VHF_FM):
    station = Link(transport, callsign, mode_registry=mode_registry, policy=policy)
    service = ModemService(station, poll_interval=0.01)
    server = StationServer(service, callsign, 0, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert server.ready.wait(5), f"{callsign} server did not become ready"
    return server, thread, station


def _close_client(client):
    if client.data is not None:
        client.data.close()
    try:
        client.cmd.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    client.cmd.close()


def _run_session(payload_ab, payload_ba, mode_registry=None, policy=VHF_FM,
                 channel_ab=None, channel_ba=None):
    """One complete session -- connect, both transfers, disconnect.

    Returns the two Links and the two transports so a caller can assert on
    the modes the session settled at and the airtime it spent.  Both are
    only meaningful after the session has finished, which is why they come
    back rather than being asserted here.
    """
    saved_turnaround = link.TX_TURNAROUND_DELAY
    saved_poll = link.DECODE_POLL_INTERVAL
    link.TX_TURNAROUND_DELAY = 0.02
    link.DECODE_POLL_INTERVAL = 0.01

    ta = PairedAudioTransport(channel_ab)
    tb = PairedAudioTransport(channel_ba)
    ta.peer, tb.peer = tb, ta
    server_a = server_b = client_a = client_b = None
    link_a = link_b = None
    threads = []
    try:
        server_a, thread_a, link_a = _server(ta, "STA1", mode_registry, policy)
        server_b, thread_b, link_b = _server(tb, "STA2", mode_registry, policy)
        threads = [thread_a, thread_b]
        client_a = StationClient("A", "127.0.0.1", server_a.cmd_port, server_a.data_port)
        client_b = StationClient("B", "127.0.0.1", server_b.cmd_port, server_b.data_port)

        client_b.send_cmd("MYCALL STA2")
        client_b.send_cmd("LISTEN ON")
        client_a.send_cmd("CONNECT STA1 STA2")
        client_a.wait_for("CONNECTED", 15)
        client_b.wait_for("CONNECTED", 15)

        client_a.open_data()
        client_b.open_data()

        client_a.send_data(payload_ab)
        assert client_b.recv_data(len(payload_ab), 120) == payload_ab
        client_b.send_data(payload_ba)
        assert client_a.recv_data(len(payload_ba), 120) == payload_ba

        client_a.send_cmd("DISCONNECT")
        client_a.wait_for("DISCONNECTED", 15)
        client_b.wait_for("DISCONNECTED", 15)
    finally:
        for client in (client_a, client_b):
            if client is not None:
                _close_client(client)
        for server in (server_a, server_b):
            if server is not None:
                server.stop()
        for thread in threads:
            thread.join(timeout=3)
        link.TX_TURNAROUND_DELAY = saved_turnaround
        link.DECODE_POLL_INTERVAL = saved_poll
    return link_a, link_b, ta, tb


def _payload(length, step, offset):
    return bytes((i * step + offset) % 256 for i in range(length))


def test_full_tcp_stack_over_paired_audio():
    """Transfer bytes both ways through every layer except physical radios."""
    _run_session(_payload(180, 7, 11), _payload(190, 13, 5))


def test_paired_transports_apply_independent_directional_channels():
    class TaggedChannel:
        sample_rate = rx_audio.CAPTURE_SAMPLE_RATE

        def __init__(self, gain, tag):
            self.gain, self.tag = gain, tag

        def process(self, audio):
            return ChannelResult(np.asarray(audio) * self.gain, {"path": self.tag})

        def reset(self):
            pass

        def describe(self):
            return {"type": "tagged", "path": self.tag}

    channel_ab = TaggedChannel(0.5, "A->B")
    channel_ba = TaggedChannel(-0.25, "B->A")
    ta = PairedAudioTransport(channel_ab)
    tb = PairedAudioTransport(channel_ba)
    ta.peer, tb.peer = tb, ta
    waveform = np.ones(400, dtype=np.float32)

    ta.send(waveform)
    tb.send(waveform)

    expected_ab = rx_audio.downsample(np.concatenate((
        waveform * 0.5, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES))))
    expected_ba = rx_audio.downsample(np.concatenate((
        waveform * -0.25, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES))))
    assert np.allclose(tb.snapshot_rx(), expected_ab)
    assert np.allclose(ta.snapshot_rx(), expected_ba)
    assert ta.channel_results[0].measurements == {"path": "A->B"}
    assert tb.channel_results[0].measurements == {"path": "B->A"}


def test_full_stack_session_over_composed_asymmetric_channels():
    def path(offset, drift, delay, clock, seed):
        return ChannelChain((
            FrequencyOffsetChannel(48_000, offset, drift),
            DelayChannel(48_000, delay),
            FilterChannel(48_000, low_hz=500, high_hz=3_500, order=4),
            ClippingChannel(48_000, 0.55),
            SampleClockChannel(48_000, clock),
            AwgnChannel(48_000, SnrSpec(25.0), seed),
        ))

    channel_ab = path(+4.0, +0.05, 0.008, +20.0, 101)
    channel_ba = path(-7.0, -0.03, 0.013, -15.0, 202)
    _, _, ta, tb = _run_session(
        _payload(180, 7, 11), _payload(190, 13, 5),
        channel_ab=channel_ab, channel_ba=channel_ba)

    assert ta.tx_channel is channel_ab
    assert tb.tx_channel is channel_ba
    assert ta.channel_results and tb.channel_results


def test_full_stack_session_over_directional_complex_fm_presets():
    channel_ab = ComplexFmChannel.from_preset(
        48_000, "ic705_to_kg_uv9d", carrier_to_noise_db=25, seed=301)
    channel_ba = ComplexFmChannel.from_preset(
        48_000, "kg_uv9d_to_ic705", carrier_to_noise_db=25, seed=302)
    _, _, ta, tb = _run_session(
        _payload(20, 7, 11), _payload(20, 13, 5),
        channel_ab=channel_ab, channel_ba=channel_ba)
    assert ta.channel_results and tb.channel_results
    assert ta.channel_results[0].measurements["rf_carrier_to_noise_db"] == 25
    assert tb.channel_results[0].measurements["rf_carrier_to_noise_db"] == 25


def test_vf3_carries_a_session_through_the_same_stack():
    """A non-CPFSK waveform, negotiated and driven by the unchanged link.

    The transfers are sized to climb the ladder: each rung steps up after one
    clean chunk, so 88 + 193 + 402 bytes at 300/600/1200 baud is enough to
    arrive at VF3 with several full 1,426-byte chunks still to send.  Nothing
    pins the mode -- reaching VF3 is the negotiation's own doing, which is the
    part worth testing.
    """
    payload_ab = _payload(4_000, 7, 11)
    payload_ba = _payload(4_000, 13, 5)
    # No mode_registry: VF3 is on the default ladder, so this also asserts
    # that an ordinary station reaches it without being told to.
    link_a, link_b, ta, tb = _run_session(payload_ab, payload_ba)

    # Both directions climbed to VF3 and stayed there.  rx_profile is the
    # peer's tx as far as each station knows, so agreement across the two
    # links is also the mode-confirmation path working.
    assert link_a.tx_profile is VF3 and link_b.rx_profile is VF3
    assert link_b.tx_profile is VF3 and link_a.rx_profile is VF3

    # And it was worth doing.  Airtime is what a radio would spend; the
    # CPFSK-only ladder carrying the same bytes is the thing to beat.
    #
    # Measured at 4,000 bytes each way: 66.6 s against 93.9 s, a 1.41x
    # saving.  The ratio is this modest only because the session is short --
    # climbing the ladder costs the same three slow chunks either way, and a
    # 5.2 s VF3 chunk is still answered by a 0.70 s ACK on the 300-baud
    # control plane.  At 16,000 bytes each way the same measurement gives
    # 161.0 s against 315.7 s, or 1.96x.  The floor here is deliberately well
    # under 1.41 so this catches a regression rather than ordinary variation.
    fast = ta.airtime + tb.airtime
    slow = sum(t.airtime for t in _run_session(
        payload_ab, payload_ba, mode_registry=afsk.default_registry())[2:])
    assert slow > 1.25 * fast, (
        f"VF3 session spent {fast:.1f}s of air against CPFSK's {slow:.1f}s")


def test_the_hf_channel_carries_a_session_with_hc0_in_control():
    """The HF station, whole: HF_SSB's policy, HF_SSB's ladder, HC1 on air.

    This is the software half of the HF acceptance test -- everything
    `scripts/run_acceptance_test.py --channel hf-ssb` does except the
    radios.  It matters more than the VF3 session does, because HC0 is the
    *control* mode: the connect handshake, the timing calibration, every
    ACK, the floor handover and the disconnect all ride a waveform that
    shares no DSP with CPFSK -- and, HC0 being MFSK, none with the OFDM
    modes either.

    Nothing is passed but the policy.  The ladder comes from
    `HF_SSB.mode_ladder`, which is the pairing whale/policy.py exists to
    keep from drifting apart.
    """
    payload_ab = _payload(600, 7, 11)
    payload_ba = _payload(600, 13, 5)
    link_a, link_b, ta, tb = _run_session(payload_ab, payload_ba, policy=HF_SSB)

    # HC0 is the control mode, so the handshake, the calibration exchange,
    # every ACK and the disconnect all rode the 16-FSK waveform.
    assert link_a.modes.control is HC0 and link_b.modes.control is HC0
    assert link_a.modes.supported_ids == (HC0.mode_id, HC1.mode_id)
    # And the data plane climbed to the fast rung, which is the ladder
    # working rather than one mode being hardcoded.
    assert link_a.tx_profile is HC1 and link_b.rx_profile is HC1
    assert link_b.tx_profile is HC1 and link_a.rx_profile is HC1


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
