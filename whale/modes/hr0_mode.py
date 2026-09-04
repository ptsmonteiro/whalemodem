"""Production WaveformMode adapter for HR0."""

from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np

from .. import framing
from . import hf_lead, hr0

HR0_MODE_ID = 10
CHUNK_SIZE = hr0.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES


class Hr0Codec:
    tx_sample_rate = hr0.SAMPLE_RATE
    rx_sample_rate = hr0.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hr0Mode", *, include_head=True,
               head_seconds=None) -> np.ndarray:
        if len(payload) > hr0.MAX_PAYLOAD_BYTES:
            raise ValueError(f"packet is {len(payload)} bytes; {mode.name} carries at most {hr0.MAX_PAYLOAD_BYTES}")
        lead = hf_lead.modulate(hf_lead.HR0_LABEL, head_seconds) if include_head else np.zeros(0, np.float32)
        return np.concatenate((lead, hr0.modulate(bytes(payload))))

    def decode(self, audio, mode: "Hr0Mode", *, head_seconds=None, **kwargs):
        del mode, kwargs
        result = hr0.demodulate(audio)
        if result.get("payload") is not None and result.get("start_index") is not None:
            observed, score = hf_lead.measure(audio, result["start_index"],
                                              hf_lead.HR0_LABEL, head_seconds)
            result.update(head_blocks_observed=observed,
                          head_seconds_received=hf_lead.seconds_received(observed),
                          head_match=score)
        return result

    def airtime(self, payload_len: int, mode: "Hr0Mode") -> float:
        del payload_len, mode
        return hr0.frame_seconds(hf_lead.MIN_SAMPLES)


HR0_CODEC = Hr0Codec()


@dataclass(frozen=True)
class Hr0Mode:
    name: str = "hr0"
    mode_id: int = HR0_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = hr0.ACQUISITION_THRESHOLD
    lead_label: int = hf_lead.HR0_LABEL
    codec: Hr0Codec = field(default=HR0_CODEC, compare=False, repr=False)
    tx_sample_rate: int = hr0.SAMPLE_RATE
    rx_sample_rate: int = hr0.RX_SAMPLE_RATE

    @property
    def baud(self):
        return hr0.BANK.symbol_rate

    @property
    def head_match_allowance_seconds(self):
        return hf_lead.BLOCK_SAMPLES / hr0.SAMPLE_RATE

    def encode(self, payload: bytes, *, include_head=True, head_seconds=None):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int):
        return self.codec.airtime(payload_len, self)


HR0 = Hr0Mode()
