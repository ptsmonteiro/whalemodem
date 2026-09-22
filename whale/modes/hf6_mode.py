"""HF6: experimental high-throughput 49-carrier OFDM mode.

HF6 packages the 64-QAM OFDM configuration of ``whale.phy.ofdm49`` (the PHY
developed in the retired ``experiments/hf10_ofdm49_v6/``, whose evidence is
in that directory's RESULTS.md) into the negotiable waveform contract.  It is
deliberately experimental: the uncoded 64-QAM operating point exceeds HF8's
peak throughput on paper, but failed the initial radio-path smoke test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from whale.phy import ofdm49 as hf6

from .. import framing, waveform
from .ofdm49_mode import Ofdm49Codec, Ofdm49Mode


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
    # Approximately 300 ms at HF6's 12 kHz design rate.
    n_preamble_symbols=12,
    equalizer="gain",
    drive_scale=1.0,
    fec_rate=FEC_RATE,
)

CHUNK_SIZE = HF6_PHY.max_payload_bytes - framing.AIR_HEADER_BYTES
CONFIDENCE_THRESHOLD = 0.12

HF6_CODEC = Ofdm49Codec(HF6_PHY)


@dataclass(frozen=True)
class Hf6Mode(Ofdm49Mode):
    name: str = "hf6"
    mode_id: int = HF6_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    fec_rate: str | None = FEC_RATE
    codec: Ofdm49Codec = field(default=HF6_CODEC, compare=False, repr=False)

    modulation = "49-carrier 64-QAM OFDM"
    fec = "none"


HF6 = Hf6Mode()

LADDER = (waveform.LadderEntry(HF6, "hf", rank=6),)
