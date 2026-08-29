"""Radio-free, full-stack audio link and session support.

The simulation stops at the same audio boundary as ``RadioTransport``.  Each
direction has an independent channel, and transmitted audio is delivered
synchronously, so elapsed wall-clock time is intentionally not a metric.
"""

from __future__ import annotations

from dataclasses import dataclass
import socket
import threading
from typing import Callable

import numpy as np

from acceptance_test import StationClient
from whale import afsk, link, rx_audio
from whale.channel import AudioChannel, ChannelResult, IdentityChannel
from whale.link import Link
from whale.policy import ChannelPolicy, VHF_FM
from whale.service import ModemService
from whale.vara_server import StationServer


FrameDropHook = Callable[[str, int, np.ndarray], bool]
TransportFaultHook = Callable[[str, int, np.ndarray], np.ndarray]


@dataclass(frozen=True)
class TransmissionRecord:
    """One simulated keying and its channel outcome."""

    direction: str
    waveform: np.ndarray
    result: ChannelResult
    airtime: float
    dropped: bool = False


class PairedAudioTransport:
    """One radio-shaped endpoint owned by :class:`DirectionalAudioLink`."""

    def __init__(self, owner: "DirectionalAudioLink", direction: str,
                 tx_channel: AudioChannel):
        self._owner = owner
        self.direction = direction
        self.tx_channel = tx_channel
        self.peer: PairedAudioTransport | None = None
        self._audio = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._transmitting = threading.Event()
        self.channel_results: list[ChannelResult] = []
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
        del kwargs
        self._transmitting.set()
        try:
            waveform = np.asarray(tx_audio, dtype=np.float32)
            if waveform.ndim != 1:
                raise ValueError("paired-audio transport requires mono audio")
            index = len(self.channel_results)
            keyed = len(waveform) / afsk.SAMPLE_RATE
            self.airtime += keyed

            dropped = bool(self._owner.frame_drop and
                           self._owner.frame_drop(self.direction, index, waveform.copy()))
            if dropped:
                result = ChannelResult(np.zeros(0, dtype=np.float32),
                                       {"transport_dropped": True})
            else:
                channel_input = waveform
                if self._owner.transport_fault is not None:
                    channel_input = np.asarray(self._owner.transport_fault(
                        self.direction, index, waveform.copy()), dtype=np.float32)
                    if channel_input.ndim != 1:
                        raise ValueError("transport fault hook returned non-mono audio")
                result = self.tx_channel.process(channel_input)

            channel_audio = np.asarray(result.audio, dtype=np.float32)
            if channel_audio.ndim != 1:
                raise ValueError("channel returned non-mono audio")
            self.channel_results.append(result)
            self._owner._capture(self, waveform, result, keyed, dropped)

            received = rx_audio.downsample(np.concatenate((
                channel_audio,
                np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32),
            )))
            assert self.peer is not None
            with self.peer._lock:
                self.peer._audio = np.concatenate((self.peer._audio, received))
            return keyed
        finally:
            self._transmitting.clear()

    def _reset(self):
        with self._lock:
            self._audio = np.zeros(0, dtype=np.float32)
        self.channel_results.clear()
        self.airtime = 0.0
        self._transmitting.clear()


class DirectionalAudioLink:
    """A paired transport with distinct A-to-B and B-to-A channel state.

    ``reset()`` restores both seeded channels and clears observations.
    ``replay()`` resets the link and repeats the captured keying sequence.
    Hooks may drop a whole frame or alter/raise on transport audio before it
    reaches the channel.
    """

    def __init__(self, channel_ab: AudioChannel | None = None,
                 channel_ba: AudioChannel | None = None, *,
                 frame_drop: FrameDropHook | None = None,
                 transport_fault: TransportFaultHook | None = None):
        rate = rx_audio.CAPTURE_SAMPLE_RATE
        self.channel_ab = IdentityChannel(rate) if channel_ab is None else channel_ab
        self.channel_ba = IdentityChannel(rate) if channel_ba is None else channel_ba
        if self.channel_ab is self.channel_ba:
            raise ValueError("A-to-B and B-to-A channels must be separate instances")
        for channel in (self.channel_ab, self.channel_ba):
            if channel.sample_rate != rate:
                raise ValueError(f"paired-audio channels must operate at the {rate} Hz capture rate")
        self.frame_drop = frame_drop
        self.transport_fault = transport_fault
        self.a = PairedAudioTransport(self, "A->B", self.channel_ab)
        self.b = PairedAudioTransport(self, "B->A", self.channel_ba)
        self.a.peer, self.b.peer = self.b, self.a
        self.records: list[TransmissionRecord] = []
        self._records_lock = threading.Lock()

    @property
    def airtime(self) -> float:
        return self.a.airtime + self.b.airtime

    def _capture(self, endpoint, waveform, result, airtime, dropped):
        with self._records_lock:
            self.records.append(TransmissionRecord(
                endpoint.direction, waveform.copy(), result, airtime, dropped))

    def reset(self):
        self.channel_ab.reset()
        self.channel_ba.reset()
        for hook in (self.frame_drop, self.transport_fault):
            reset = getattr(hook, "reset", None)
            if reset is not None:
                reset()
        self.a._reset()
        self.b._reset()
        with self._records_lock:
            self.records.clear()

    def replay(self) -> tuple[TransmissionRecord, ...]:
        with self._records_lock:
            sequence = tuple((record.direction, record.waveform.copy())
                             for record in self.records)
        self.reset()
        for direction, waveform in sequence:
            (self.a if direction == "A->B" else self.b).send(waveform)
        return tuple(self.records)


@dataclass(frozen=True)
class AudioSessionResult:
    link_a: Link
    link_b: Link
    audio_link: DirectionalAudioLink
    setup_airtime: float = 0.0
    transfer_airtime: float = 0.0
    disconnect_airtime: float = 0.0


def _server(transport, callsign, mode_registry, policy):
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


def run_audio_session(payload_ab: bytes, payload_ba: bytes, mode_registry=None,
                      policy: ChannelPolicy = VHF_FM,
                      audio_link: DirectionalAudioLink | None = None,
                      channel_ab: AudioChannel | None = None,
                      channel_ba: AudioChannel | None = None,
                      on_phase: Callable[[str, float], None] | None = None
                      ) -> AudioSessionResult:
    """Connect, transfer both ways, and disconnect through the full TCP stack."""
    if audio_link is not None and (channel_ab is not None or channel_ba is not None):
        raise ValueError("pass audio_link or directional channels, not both")
    pair = audio_link or DirectionalAudioLink(channel_ab, channel_ba)
    saved_turnaround = link.TX_TURNAROUND_DELAY
    saved_poll = link.DECODE_POLL_INTERVAL
    link.TX_TURNAROUND_DELAY = 0.02
    link.DECODE_POLL_INTERVAL = 0.01
    servers = []
    clients = []
    threads = []
    try:
        server_a, thread_a, link_a = _server(pair.a, "STA1", mode_registry, policy)
        servers.append(server_a)
        threads.append(thread_a)
        server_b, thread_b, link_b = _server(pair.b, "STA2", mode_registry, policy)
        servers.append(server_b)
        threads.append(thread_b)
        client_a = StationClient("A", "127.0.0.1", server_a.cmd_port, server_a.data_port)
        client_b = StationClient("B", "127.0.0.1", server_b.cmd_port, server_b.data_port)
        clients.extend((client_a, client_b))
        client_b.send_cmd("MYCALL STA2")
        client_b.send_cmd("LISTEN ON")
        client_a.send_cmd("CONNECT STA1 STA2")
        client_a.wait_for("CONNECTED", 15)
        client_b.wait_for("CONNECTED", 15)
        setup_airtime = pair.airtime
        if on_phase is not None:
            on_phase("connected", setup_airtime)
        client_a.open_data()
        client_b.open_data()
        client_a.send_data(payload_ab)
        assert client_b.recv_data(len(payload_ab), 120) == payload_ab
        client_b.send_data(payload_ba)
        assert client_a.recv_data(len(payload_ba), 120) == payload_ba
        transfer_done_airtime = pair.airtime
        if on_phase is not None:
            on_phase("transferred", transfer_done_airtime)
        client_a.send_cmd("DISCONNECT")
        client_a.wait_for("DISCONNECTED", 15)
        client_b.wait_for("DISCONNECTED", 15)
        if on_phase is not None:
            on_phase("disconnected", pair.airtime)
        return AudioSessionResult(
            link_a, link_b, pair, setup_airtime,
            transfer_done_airtime - setup_airtime,
            pair.airtime - transfer_done_airtime)
    finally:
        for client in clients:
            _close_client(client)
        for server in servers:
            server.stop()
        for thread in threads:
            thread.join(timeout=3)
        link.TX_TURNAROUND_DELAY = saved_turnaround
        link.DECODE_POLL_INTERVAL = saved_poll
