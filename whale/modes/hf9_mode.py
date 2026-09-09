"""HF9: a short, robust QPSK mode on HF8's 49-carrier geometry."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from whale.phy import ofdm49

from .. import framing


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
    equalizer="gain",
    drive_scale=0.008,
    fec_rate=FEC_RATE,
    interleave=INTERLEAVE,
)

CHUNK_SIZE = HF9_PHY.max_payload_bytes - framing.AIR_HEADER_BYTES
CONFIDENCE_THRESHOLD = 0.12


class Hf9Codec:
    tx_sample_rate = ofdm49.TX_SAMPLE_RATE
    rx_sample_rate = ofdm49.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hf9Mode", *, include_head=True,
               head_seconds=None) -> np.ndarray:
        del include_head, head_seconds
        if len(payload) > HF9_PHY.max_payload_bytes:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{HF9_PHY.max_payload_bytes}")
        return HF9_PHY.modulate(bytes(payload))

    def decode(self, audio, mode: "Hf9Mode", *, head_seconds=None,
               **kwargs) -> dict:
        del mode, head_seconds
        if np.asarray(audio).ndim != 1:
            return {"synced": False, "payload": None}
        kwargs.setdefault("noise_estimator", NOISE_ESTIMATOR)
        return HF9_PHY.demodulate(audio, **kwargs)

    def airtime(self, payload_len: int, mode: "Hf9Mode") -> float:
        del payload_len, mode
        return HF9_PHY.frame_seconds()


HF9_CODEC = Hf9Codec()


@dataclass(frozen=True)
class Hf9Mode:
    name: str = "hf9"
    mode_id: int = HF9_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    fec_rate: str | None = FEC_RATE
    codec: Hf9Codec = field(default=HF9_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        return ofdm49.DESIGN_RATE / HF9_PHY.symbol_len

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


HF9 = Hf9Mode()
