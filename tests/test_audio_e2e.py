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

import numpy as np

from support.audio_link import DirectionalAudioLink, run_audio_session
from whale import afsk, rx_audio
from whale.channel import (AwgnChannel, ChannelChain, ChannelResult,
                           ClippingChannel, DelayChannel, FilterChannel,
                           FrequencyOffsetChannel, SampleClockChannel, SnrSpec)
from whale.fm_channel import ComplexFmChannel
from whale.modes.hc0_mode import HC0
from whale.modes.hc1_mode import HC1
from whale.modes.vf3_mode import VF3
from whale.policy import HF_SSB, VHF_FM


def _run_session(*args, **kwargs):
    result = run_audio_session(*args, **kwargs)
    return result.link_a, result.link_b, result.audio_link.a, result.audio_link.b


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

        def drain(self, audio=None):
            samples = np.zeros(0, np.float32) if audio is None else np.asarray(audio)
            return ChannelResult(samples * self.gain, {"path": self.tag})

        def reset(self):
            pass

        def describe(self):
            return {"type": "tagged", "path": self.tag}

    channel_ab = TaggedChannel(0.5, "A->B")
    channel_ba = TaggedChannel(-0.25, "B->A")
    pair = DirectionalAudioLink(channel_ab, channel_ba)
    ta, tb = pair.a, pair.b
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


def test_directional_audio_link_resets_and_replays_seeded_channels():
    pair = DirectionalAudioLink(
        AwgnChannel(48_000, SnrSpec(29.0308998699), seed=41),
        AwgnChannel(48_000, SnrSpec(29.0308998699), seed=42),
    )
    pair.a.send(np.ones(480, dtype=np.float32))
    pair.b.send(np.full(240, 0.5, dtype=np.float32))
    first = tuple(record.result.audio.copy() for record in pair.records)
    first_airtime = pair.airtime

    replayed = pair.replay()

    assert [record.direction for record in replayed] == ["A->B", "B->A"]
    assert all(np.array_equal(record.result.audio, expected)
               for record, expected in zip(replayed, first))
    assert pair.airtime == first_airtime
    assert pair.a.channel_results == [replayed[0].result]
    assert pair.b.channel_results == [replayed[1].result]


def test_directional_audio_link_drop_and_transport_fault_hooks():
    def drop(direction, index, waveform):
        return direction == "A->B" and index == 0

    def invert(direction, index, waveform):
        assert direction == "B->A" and index == 0
        return -waveform

    pair = DirectionalAudioLink(frame_drop=drop, transport_fault=invert)
    waveform = np.ones(400, dtype=np.float32)

    pair.a.send(waveform)
    pair.b.send(waveform)

    assert pair.records[0].dropped
    assert pair.records[0].result.measurements == {"transport_dropped": True}
    assert pair.b.snapshot_rx().size == rx_audio.downsample(np.zeros(
        rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32)).size
    expected = rx_audio.downsample(np.concatenate((
        -waveform, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES))))
    assert np.allclose(pair.a.snapshot_rx(), expected)
    assert pair.airtime == 2 * len(waveform) / afsk.SAMPLE_RATE


def test_full_stack_session_over_composed_asymmetric_channels():
    def path(offset, drift, delay, clock, seed):
        return ChannelChain((
            FrequencyOffsetChannel(48_000, offset, drift),
            DelayChannel(48_000, delay),
            FilterChannel(48_000, low_hz=500, high_hz=3_500, order=4),
            ClippingChannel(48_000, 0.55),
            SampleClockChannel(48_000, clock),
            AwgnChannel(48_000, SnrSpec(34.0308998699), seed),
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
