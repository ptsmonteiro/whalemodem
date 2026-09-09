"""HF5: resilient level-3 single-carrier data mode.

HF5 uses the 8PSK/1500-baud single-carrier waveform and fast acquisition, but adds a
terminated K=7 rate-1/2 convolutional code and a block interleaver.  Its
full-capacity frame is sized for just over 2 kbit/s net application rate,
leaving coding margin for moderate Watterson fading.  This is an experimental
mode until the requested 16 dB Watterson envelope has promotion-sized evidence.

The waveform is `whale/phy/sc_resilient.py` (developed in
`experiments/hf15_resilient/`, whose remaining code is the FEC-rate sweep).
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

    @property
    def head_match_allowance_seconds(self) -> float:
        """This PHY carries no outer head, so it never reports an observation
        and `_head_feedback_request` returns before consulting the allowance.
        The value is still dereferenced eagerly at that call site, so it has
        to exist: without it the first frame this mode ever receives raises
        an AttributeError that escapes ModemService's LinkError handler and
        tears the link down mid-transfer."""
        return 0.0

    def encode(self, payload: bytes, *, include_head=True, head_seconds=None):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HF5 = Hf5Mode()
