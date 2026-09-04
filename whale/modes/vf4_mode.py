"""Negotiable adapter for experimental Level-3 VHF FM mode VF4."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import resample_poly

from .. import framing, rx_audio
from . import vf4

#: On-air identifier. Mode IDs are global across both channel policies
#: (`mode_qualification.validate_manifest` forbids reuse): 0-2 are the CPFSK
#: profiles, 3/6 are VF3/VF6 (vhf-fm), 4/5/7 are HC1/HC0/HF2 (hf-ssb). 8 is
#: the next free global ID.
VF4_MODE_ID = 8
CHUNK_SIZE = vf4.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
CONFIDENCE_THRESHOLD = vf4.ACQUISITION_THRESHOLD


class Vf4Codec:
    tx_sample_rate = vf4.SAMPLE_RATE
    rx_sample_rate = rx_audio.DECODE_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Vf4Mode", *, include_head=True,
               head_seconds=0.045) -> np.ndarray:
        del include_head, head_seconds
        if len(payload) > vf4.MAX_PAYLOAD_BYTES:
            raise ValueError(f"packet is {len(payload)} bytes; {mode.name} carries at most {vf4.MAX_PAYLOAD_BYTES}")
        return vf4.modulate(bytes(payload))

    def decode(self, audio, mode: "Vf4Mode", **kwargs) -> dict:
        del mode, kwargs
        samples = np.asarray(audio, dtype=np.float64).reshape(-1)
        # The shared receive front end is 12 kHz; VF4 keeps VF6's native
        # 48 kHz geometry and reconstructs up via the same polyphase path.
        result = vf4.demodulate(resample_poly(samples, 4, 1))
        start = result.get("start_index")
        if start is not None:
            result["sync_end_index"] = int(round(
                (start + vf4.HEADER_SYMBOLS * vf4.SYMBOL_SAMPLES) / 4))
            frame_after_sync = (vf4.TOTAL_SYMBOLS * vf4.SYMBOL_SAMPLES
                                + vf4.TAIL_SAMPLES)
            if result.get("payload") is not None:
                result["end_index"] = int(round((start + frame_after_sync) / 4))
        return result

    def airtime(self, payload_len: int, mode: "Vf4Mode") -> float:
        del payload_len, mode
        return vf4.FRAME_SECONDS


VF4_CODEC = Vf4Codec()


@dataclass(frozen=True)
class Vf4Mode:
    name: str = "vf4"
    mode_id: int = VF4_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    codec: Vf4Codec = field(default=VF4_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self): return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self): return self.codec.rx_sample_rate

    @property
    def baud(self): return vf4.SAMPLE_RATE / vf4.SYMBOL_SAMPLES

    @property
    def head_match_allowance_seconds(self): return vf4.CORE_SAMPLES / vf4.SAMPLE_RATE

    def encode(self, payload: bytes, **kwargs): return self.codec.encode(payload, self, **kwargs)

    def decode(self, audio, **kwargs): return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int): return self.codec.airtime(payload_len, self)


VF4 = Vf4Mode()
