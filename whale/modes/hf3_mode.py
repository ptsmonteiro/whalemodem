"""HF3, the HF4 waveform with a rate-3/4 inner convolutional code."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from experiments import hf3_fec34 as hf3

from .. import framing

HF3_MODE_ID = 9
CHUNK_SIZE = hf3.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
CONFIDENCE_THRESHOLD = hf3.ACQUISITION_THRESHOLD


class Hf3Codec:
    tx_sample_rate = hf3.SAMPLE_RATE
    rx_sample_rate = hf3.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hf3Mode", *, include_head=True,
               head_seconds=None) -> np.ndarray:
        del mode, include_head, head_seconds
        if len(payload) > hf3.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; hf3 carries at most "
                f"{hf3.MAX_PAYLOAD_BYTES}")
        return hf3.modulate(payload)

    def decode(self, audio, mode: "Hf3Mode", *, head_seconds=None, **kwargs) -> dict:
        del mode, head_seconds
        return hf3.demodulate(audio, **kwargs)

    def airtime(self, payload_len: int, mode: "Hf3Mode") -> float:
        del payload_len, mode
        return hf3.FRAME_SECONDS


HF3_CODEC = Hf3Codec()


@dataclass(frozen=True)
class Hf3Mode:
    name: str = "hf3"
    mode_id: int = HF3_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    fec_rate: float = hf3.FEC_RATE
    codec: Hf3Codec = field(default=HF3_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        return hf3.SAMPLE_RATE / hf3.SYMBOL_SAMPLES

    def encode(self, payload: bytes, *, include_head=True, head_seconds=None):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HF3 = Hf3Mode()
