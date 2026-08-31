"""Negotiable adapter for experimental top-rung VF6."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import resample_poly

from .. import framing, rx_audio
from . import vf6

VF6_MODE_ID = 6
CHUNK_SIZE = vf6.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
CONFIDENCE_THRESHOLD = vf6.ACQUISITION_THRESHOLD


class Vf6Codec:
    tx_sample_rate = vf6.SAMPLE_RATE
    rx_sample_rate = rx_audio.DECODE_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Vf6Mode", *, include_head=True,
               head_seconds=0.045) -> np.ndarray:
        del include_head, head_seconds
        if len(payload) > vf6.MAX_PAYLOAD_BYTES:
            raise ValueError(f"packet is {len(payload)} bytes; {mode.name} carries at most {vf6.MAX_PAYLOAD_BYTES}")
        return vf6.modulate(bytes(payload))

    def decode(self, audio, mode: "Vf6Mode", **kwargs) -> dict:
        del mode, kwargs
        samples = np.asarray(audio, dtype=np.float64).reshape(-1)
        # The shared receive front end is 12 kHz. VF6 retains the qualified
        # VF5 48 kHz geometry; polyphase reconstruction keeps that boundary
        # explicit until a native 12 kHz FFT-bank port is benchmarked.
        result = vf6.demodulate(resample_poly(samples, 4, 1))
        start = result.get("start_index")
        if start is not None:
            result["sync_end_index"] = int(round(
                (start + vf6.HEADER_SYMBOLS * vf6.SYMBOL_SAMPLES) / 4))
            frame_after_sync = (vf6.TOTAL_SYMBOLS * vf6.SYMBOL_SAMPLES
                                + vf6.TAIL_SAMPLES)
            if result.get("payload") is not None:
                result["end_index"] = int(round((start + frame_after_sync) / 4))
        return result

    def airtime(self, payload_len: int, mode: "Vf6Mode") -> float:
        del payload_len, mode
        return vf6.FRAME_SECONDS


VF6_CODEC = Vf6Codec()


@dataclass(frozen=True)
class Vf6Mode:
    name: str = "vf6"
    mode_id: int = VF6_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    codec: Vf6Codec = field(default=VF6_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self): return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self): return self.codec.rx_sample_rate

    @property
    def baud(self): return vf6.SAMPLE_RATE / vf6.SYMBOL_SAMPLES

    @property
    def head_match_allowance_seconds(self): return vf6.CORE_SAMPLES / vf6.SAMPLE_RATE

    def encode(self, payload: bytes, **kwargs): return self.codec.encode(payload, self, **kwargs)

    def decode(self, audio, **kwargs): return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int): return self.codec.airtime(payload_len, self)


VF6 = Vf6Mode()
