"""HF9: a short, robust QPSK mode on HF8's 49-carrier geometry."""

from __future__ import annotations

from dataclasses import dataclass, field

from whale.phy import ofdm49

from .. import framing, waveform
from .ofdm49_mode import Ofdm49Codec, Ofdm49Mode


#: On-air identifier; mode IDs identify one immutable waveform globally.
HF9_MODE_ID = 17

FFT_SIZE = 240
CP_LEN = 24
BITS_PER_SYMBOL = 2
FEC_RATE = "1/2"
PILOT_INTERVAL = 5
INTERLEAVE = True
NOISE_ESTIMATOR = "repeat"
BAND_LO_HZ = 300.0
BAND_HI_HZ = 2700.0
PACKET_BYTES = 121

ACTIVE_BINS = tuple(ofdm49.bins_in_band(
    FFT_SIZE, BAND_LO_HZ, BAND_HI_HZ))
HF9_PHY = ofdm49.OFDM49Mode(
    fft_size=FFT_SIZE,
    cp_len=CP_LEN,
    active_bins=ACTIVE_BINS,
    bits_per_symbol=BITS_PER_SYMBOL,
    packet_bytes=PACKET_BYTES,
    pilot_interval=PILOT_INTERVAL,
    # Approximately 300 ms at HF9's 12 kHz design rate.
    n_preamble_symbols=14,
    equalizer="gain",
    drive_scale=0.008,
    fec_rate=FEC_RATE,
    interleave=INTERLEAVE,
)

CHUNK_SIZE = HF9_PHY.max_payload_bytes - framing.AIR_HEADER_BYTES
CONFIDENCE_THRESHOLD = 0.12

HF9_CODEC = Ofdm49Codec(
    HF9_PHY, streaming=True,
    decode_kwargs={"noise_estimator": NOISE_ESTIMATOR})


@dataclass(frozen=True)
class Hf9Mode(Ofdm49Mode):
    name: str = "hf9"
    mode_id: int = HF9_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    fec_rate: str | None = FEC_RATE
    codec: Ofdm49Codec = field(default=HF9_CODEC, compare=False, repr=False)

    modulation = "49-carrier QPSK OFDM"
    fec = "QC-LDPC 1/2"


HF9 = Hf9Mode()

LADDER = (waveform.LadderEntry(HF9, "hf", rank=2),)
