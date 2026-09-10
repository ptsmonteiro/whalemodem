"""HC1W's link-layer adapter.

HC1W is a fixed-length DATA mode with its native fixed preamble. Its checked payload
identifies the 23-carrier waveform and K=9 convolutional code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .. import framing
from . import hc1w

#: On-air identifier; mode IDs identify one immutable waveform globally.
HC1W_MODE_ID = 16

#: Largest DATA body one HC1W frame can carry after the link air header.
CHUNK_SIZE = hc1w.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES

#: `hc1w.ACQUISITION_THRESHOLD` under the name the link reads it by.  The
#: link uses it for two decisions: whether a decode attempt counts as a real
#: sync lock worth waiting on, and whether the buffer is stale enough to
#: prune.
CONFIDENCE_THRESHOLD = hc1w.ACQUISITION_THRESHOLD


class Hc1wCodec:
    """Bridges the link's codec calls onto `whale.modes.hc1w`."""

    tx_sample_rate = hc1w.SAMPLE_RATE
    rx_sample_rate = hc1w.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hc1wMode") -> np.ndarray:
        if len(payload) > hc1w.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{hc1w.MAX_PAYLOAD_BYTES}")
        return hc1w.modulate(bytes(payload))

    def decode(self, audio, mode: "Hc1wMode", **kwargs) -> dict:
        del mode, kwargs
        return hc1w.demodulate(audio)

    def airtime(self, payload_len: int, mode: "Hc1wMode") -> float:
        del payload_len  # an HC1W frame is fixed length
        return ((hc1w.LEAD_IN_SAMPLES
                 + hc1w.TOTAL_SYMBOLS * hc1w.SYMBOL_SAMPLES
                 + hc1w.TAIL_SAMPLES) / hc1w.SAMPLE_RATE)


HC1W_CODEC = Hc1wCodec()


@dataclass(frozen=True)
class Hc1wMode:
    """The default wideband HF differential-QPSK mode."""

    name: str = "hc1w"
    mode_id: int = HC1W_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    codec: Hc1wCodec = field(default=HC1W_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        """HC1W's OFDM symbol rate, 75 symbol/s."""
        return hc1w.SAMPLE_RATE / hc1w.SYMBOL_SAMPLES

    def encode(self, payload: bytes):
        return self.codec.encode(payload, self)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HC1W = Hc1wMode()
