"""HF5: HF4-derived level-3 resilient single-carrier data mode.

HF5 keeps HF4's 8PSK/1500-baud waveform and fast acquisition, but adds a
terminated K=7 rate-1/2 convolutional code and a block interleaver.  Its
full-capacity frame is sized for just over 2 kbit/s net application rate,
leaving coding margin for moderate Watterson fading.  This is an experimental
mode until the requested 16 dB Watterson envelope has promotion-sized evidence.

The waveform is `whale/phy/sc_resilient.py` (developed as
`experiments/hf15_resilient/sc_resilient.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from whale.phy import sc_resilient as hf5
from .. import framing

HF5_MODE_ID = 12
CHUNK_SIZE = hf5.max_payload_bytes() - framing.AIR_HEADER_BYTES
CONFIDENCE_THRESHOLD = 0.12


class Hf5Codec:
    tx_sample_rate = hf5.TX_SAMPLE_RATE
    rx_sample_rate = hf5.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hf5Mode", *, include_head=True,
               head_seconds=None) -> np.ndarray:
        del mode, include_head, head_seconds
        return hf5.modulate(bytes(payload))

    def decode(self, audio, mode: "Hf5Mode", *, head_seconds=None, **kwargs) -> dict:
        del mode, head_seconds, kwargs
        return hf5.demodulate(audio)

    def airtime(self, payload_len: int, mode: "Hf5Mode") -> float:
        del payload_len, mode
        return hf5.airtime()


HF5_CODEC = Hf5Codec()


@dataclass(frozen=True)
class Hf5Mode:
    name: str = "hf5"
    mode_id: int = HF5_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    fec_rate: float = 0.5
    codec: Hf5Codec = field(default=HF5_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        return hf5.BAUD

    def encode(self, payload: bytes, *, include_head=True, head_seconds=None):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HF5 = Hf5Mode()
