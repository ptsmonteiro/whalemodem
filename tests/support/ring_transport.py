"""A radio-free transport that honours the real receive contract.

The point of this module is that it does *not* reimplement the receive side.
It holds a real :class:`whale.transport.ReceiveStream`, so the rolling ring,
the monotonic stream position, the discard marker and the gap report are the
same code the radio runs.  The previous fakes each grew their own buffer with
its own coordinates and none of them cleared on send or reported a position at
all, which left the live decode path's coordinate handling untested -- and a
coordinate bug reached the air.

Delivery is chunked at the capture callback's size, because a fake that hands
over an entire keying in one call cannot show what a receiver does while a
frame is only half here.
"""

from __future__ import annotations

import threading

import numpy as np

from whale import afsk, rx_audio, transport as transport_mod

#: 100 ms of 12 kHz audio: what the real capture callback delivers per call.
CHUNK_SAMPLES = 1200


class RingTransport:
    """RadioTransport's receive/transmit contract without a sound card.

    ``send`` behaves like the real one: it marks the station as transmitting
    for the whole call and voids its own capture before and after, so a
    station never decodes its own keying and the decode loop meets a real
    gap when it comes back.
    """

    def __init__(self):
        self._rx = transport_mod.ReceiveStream()
        self._transmitting = threading.Event()
        self.peer: RingTransport | None = None
        # Optional f(audio) -> audio applied to one transmission, so a test
        # can lose a DATA frame or an ACK outright.
        self.corrupt = None
        self.keyings = 0
        self.sent: list[np.ndarray] = []

    # -- receive ------------------------------------------------------

    def start_receiving(self):
        pass

    def stop_receiving(self):
        pass

    def deliver(self, decoded):
        """Write 12 kHz samples in, in capture-callback-sized pieces."""
        decoded = np.asarray(decoded, dtype=np.float32)
        for start in range(0, len(decoded), CHUNK_SAMPLES):
            self._rx.write(decoded[start:start + CHUNK_SAMPLES])

    def read_rx(self, since=None):
        return self._rx.read(since)

    def snapshot_rx(self):
        return self._rx.read().audio

    def discard_rx(self):
        return self._rx.discard()

    @property
    def rx_stream_position(self):
        return self._rx.position

    @property
    def rx_discard_position(self):
        return self._rx.discard_position

    def is_transmitting(self):
        return self._transmitting.is_set()

    # -- transmit -----------------------------------------------------

    def transmit_audio(self, tx_audio):
        """The 48 kHz transmission as the peer's capture would hear it."""
        return rx_audio.downsample(np.concatenate((
            np.asarray(tx_audio, dtype=np.float32),
            np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32),
        )))

    def send(self, tx_audio, **kwargs):
        del kwargs
        self.keyings += 1
        if self.corrupt is not None:
            tx_audio = self.corrupt(tx_audio)
        tx_audio = np.asarray(tx_audio, dtype=np.float32)
        self._transmitting.set()
        self.discard_rx()
        try:
            self.sent.append(tx_audio)
            self._deliver_to_peer(tx_audio)
            return len(tx_audio) / afsk.SAMPLE_RATE
        finally:
            self.discard_rx()
            self._transmitting.clear()

    def _deliver_to_peer(self, tx_audio):
        if self.peer is not None:
            self.peer.deliver(self.transmit_audio(tx_audio))
