"""Production WaveformMode adapter for HR0."""

from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np

from .. import framing, waveform
from whale.phy import hr0

HR0_MODE_ID = 10
CHUNK_SIZE = hr0.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES


class Hr0Codec:
    tx_sample_rate = hr0.SAMPLE_RATE
    rx_sample_rate = hr0.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hr0Mode") -> np.ndarray:
        if len(payload) > hr0.MAX_PAYLOAD_BYTES:
            raise ValueError(f"packet is {len(payload)} bytes; {mode.name} carries at most {hr0.MAX_PAYLOAD_BYTES}")
        del mode
        return hr0.modulate(bytes(payload))

    def decode(self, audio, mode: "Hr0Mode", **kwargs):
        del mode, kwargs
        return hr0.demodulate(audio)

    def airtime(self, payload_len: int, mode: "Hr0Mode") -> float:
        del mode
        return hr0.frame_seconds(payload_len)


HR0_CODEC = Hr0Codec()


@dataclass(frozen=True)
class Hr0Mode(waveform.ModeDescription):
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

    def encode(self, payload: bytes):
        return self.codec.encode(payload, self)

    def decode(self, audio, **kwargs):
        # Additive canonical snr_db/freq_offset_hz alias; the mode's
        # own spelling (tone_snr_db/carrier_snr_db/cfo_hz/...) is kept.
        return waveform.canonicalize_result(self.codec.decode(audio, self, **kwargs))

    def airtime(self, payload_len: int):
        return self.codec.airtime(payload_len, self)

    @property
    def band_hz(self) -> tuple[float, float]:
        """Lowest to highest carrier/tone centre, in Hz."""
        return (float(hr0.BANK.tone_hz[0]), float(hr0.BANK.tone_hz[-1]))

    modulation = "32-tone noncoherent FSK"
    fec = "K=9 conv 1/2"


HR0 = Hr0Mode()

#: HR0 is the hf control mode and the lowest (most robust) rung of the
#: ladder; see whale/mode_qualification.py's MANIFEST for its qualification
#: evidence.
LADDER = (waveform.LadderEntry(HR0, "hf", rank=0, control=True),)
