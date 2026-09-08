"""HF6: experimental high-throughput 49-carrier OFDM mode.

HF6 packages the 64-QAM OFDM configuration of ``whale.phy.ofdm49`` (the PHY
developed as ``experiments/hf10_ofdm49_v6/ofdm49_v6.py``, whose evidence is
in that directory's RESULTS.md) into the negotiable waveform contract.  It is
deliberately experimental: the uncoded 64-QAM operating point exceeds HF4's
peak throughput on paper, but failed the initial radio-path smoke test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from whale.phy import ofdm49 as hf6

from .. import framing


HF6_MODE_ID = 13
PACKET_BYTES = 350
FFT_SIZE = 240
CP_LEN = 60
BITS_PER_SYMBOL = 6
PILOT_INTERVAL = 20
FEC_RATE = None

ACTIVE_BINS = tuple(hf6.bins_in_band(FFT_SIZE))
HF6_PHY = hf6.OFDM49Mode(
    fft_size=FFT_SIZE,
    cp_len=CP_LEN,
    active_bins=ACTIVE_BINS,
    bits_per_symbol=BITS_PER_SYMBOL,
    packet_bytes=PACKET_BYTES,
    pilot_interval=PILOT_INTERVAL,
    equalizer="gain",
    drive_scale=1.0,
    fec_rate=FEC_RATE,
)

CHUNK_SIZE = HF6_PHY.max_payload_bytes - framing.AIR_HEADER_BYTES
CONFIDENCE_THRESHOLD = 0.12


class Hf6Codec:
    tx_sample_rate = hf6.TX_SAMPLE_RATE
    rx_sample_rate = hf6.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hf6Mode", *, include_head=True,
               head_seconds=None) -> np.ndarray:
        del include_head, head_seconds
        if len(payload) > HF6_PHY.max_payload_bytes:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{HF6_PHY.max_payload_bytes}")
        return HF6_PHY.modulate(bytes(payload))

    def decode(self, audio, mode: "Hf6Mode", *, head_seconds=None, **kwargs) -> dict:
        del mode, head_seconds
        if np.asarray(audio).ndim != 1:
            return {"synced": False, "payload": None}
        return HF6_PHY.demodulate(audio, **kwargs)

    def airtime(self, payload_len: int, mode: "Hf6Mode") -> float:
        del payload_len, mode
        return HF6_PHY.frame_seconds()


HF6_CODEC = Hf6Codec()


@dataclass(frozen=True)
class Hf6Mode:
    name: str = "hf6"
    mode_id: int = HF6_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    fec_rate: str | None = FEC_RATE
    codec: Hf6Codec = field(default=HF6_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        return hf6.DESIGN_RATE / HF6_PHY.symbol_len

    def encode(self, payload: bytes, *, include_head=True, head_seconds=None):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HF6 = Hf6Mode()
