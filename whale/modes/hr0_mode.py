"""Production WaveformMode adapter for HR0."""

from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np

from .. import framing
from . import hr0

HR0_MODE_ID = 10
CHUNK_SIZE = hr0.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES


class Hr0Codec:
    tx_sample_rate = hr0.SAMPLE_RATE
    rx_sample_rate = hr0.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hr0Mode", *, include_head=True,
               head_seconds=None) -> np.ndarray:
        if len(payload) > hr0.MAX_PAYLOAD_BYTES:
            raise ValueError(f"packet is {len(payload)} bytes; {mode.name} carries at most {hr0.MAX_PAYLOAD_BYTES}")
        if not include_head:
            head_seconds = hr0.DEFAULT_HEAD_SECONDS
        return hr0.modulate(bytes(payload), head_seconds=head_seconds)

    def decode(self, audio, mode: "Hr0Mode", *, head_seconds=None, **kwargs):
        del mode, kwargs
        result = hr0.demodulate(audio, head_seconds=head_seconds)
        observed = result.pop("head_blocks_received", None)
        if observed is not None:
            result.update(
                head_blocks_observed=observed,
                head_seconds_received=(
                    observed * hr0.HEAD_BLOCK_SAMPLES / hr0.SAMPLE_RATE))
        return result

    def airtime(self, payload_len: int, mode: "Hr0Mode") -> float:
        del mode
        return hr0.frame_seconds(hr0.lead_in_samples(), payload_len)


HR0_CODEC = Hr0Codec()


@dataclass(frozen=True)
class Hr0Mode:
    name: str = "hr0"
    mode_id: int = HR0_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = hr0.ACQUISITION_THRESHOLD
    codec: Hr0Codec = field(default=HR0_CODEC, compare=False, repr=False)
    tx_sample_rate: int = hr0.SAMPLE_RATE
    rx_sample_rate: int = hr0.RX_SAMPLE_RATE

    @property
    def baud(self):
        return hr0.BANK.symbol_rate

    @property
    def head_match_allowance_seconds(self):
        return hr0.HEAD_BLOCK_SAMPLES / hr0.SAMPLE_RATE

    def encode(self, payload: bytes, *, include_head=True, head_seconds=None):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int):
        return self.codec.airtime(payload_len, self)


HR0 = Hr0Mode()
