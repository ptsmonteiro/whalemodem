"""Full radio-free acceptance test over an interconnected audio channel.

This retains the public TCP command/data API, both StationServers,
ModemServices, Links, framing, modulation, demodulation and ARQ.  Only the
physical boundary is replaced: instead of sound cards, PTT and radios, each
transport puts its transmitted waveform into its peer's receive buffer.
"""

import socket
import threading

import numpy as np

from acceptance_test import StationClient
from whale import afsk, link
from whale.link import Link
from whale.service import ModemService
from whale.vara_server import StationServer


class PairedAudioTransport:
    """A radio-shaped endpoint connected to its peer by raw audio samples."""

    def __init__(self):
        self.peer = None
        self._audio = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._transmitting = threading.Event()

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
            with self.peer._lock:
                self.peer._audio = np.concatenate((self.peer._audio, waveform))
            return len(waveform) / afsk.SAMPLE_RATE
        finally:
            self._transmitting.clear()


def _server(transport, callsign):
    service = ModemService(Link(transport, callsign), poll_interval=0.01)
    server = StationServer(service, callsign, 0, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert server.ready.wait(5), f"{callsign} server did not become ready"
    return server, thread


def _close_client(client):
    if client.data is not None:
        client.data.close()
    try:
        client.cmd.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    client.cmd.close()


def test_full_tcp_stack_over_paired_audio():
    """Transfer bytes both ways through every layer except physical radios."""
    saved_turnaround = link.TX_TURNAROUND_DELAY
    saved_poll = link.DECODE_POLL_INTERVAL
    link.TX_TURNAROUND_DELAY = 0.02
    link.DECODE_POLL_INTERVAL = 0.01

    ta, tb = PairedAudioTransport(), PairedAudioTransport()
    ta.peer, tb.peer = tb, ta
    server_a = server_b = client_a = client_b = None
    threads = []
    try:
        server_a, thread_a = _server(ta, "STA1")
        server_b, thread_b = _server(tb, "STA2")
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
        payload_ab = bytes((i * 7 + 11) % 256 for i in range(180))
        payload_ba = bytes((i * 13 + 5) % 256 for i in range(190))

        client_a.send_data(payload_ab)
        assert client_b.recv_data(len(payload_ab), 30) == payload_ab
        client_b.send_data(payload_ba)
        assert client_a.recv_data(len(payload_ba), 30) == payload_ba

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
