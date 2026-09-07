"""HF7: 45-carrier OFDM maximum-speed HF data mode.

HF7 wires the configuration measured in `experiments/hf18_ofdm49_vara/`
into the link's `WaveformMode` contract, mirroring `whale/modes/hf6_mode.py`'s
pattern; it does not re-derive or re-run that design or its evidence.

The waveform is `experiments/hf10_ofdm49_v6/ofdm49_v6.py` -- unmodified, the
same PHY HF6 already wraps -- at 50 Hz subcarrier spacing (`fft_size=240` at
the 12 kHz design rate) with a 2 ms guard, 32-QAM, and rate-3/4 LDPC over an
interleaved frame.  That geometry is VARA HF's top-speed arrangement, and a
100-trial-per-arm interleaved comparison against HF6's 25 Hz-spaced,
97-carrier arrangement found their frame delivery statistically
indistinguishable (96/100 vs 94/100, Fisher p = 0.75) while this one carries
more bits per second of air time.

Two numbers from that experiment shape this module and are worth stating
where they will be read:

  * **Occupied bandwidth is the binding constraint, not throughput.** The
    49-carrier version that experiment measured spans 300-2700 Hz and has a
    99%-power occupied bandwidth of 2,444 Hz, over the 2,300 Hz ceiling
    `SPEED_LADDERS.md` places on every HF rung.  HF7 therefore drops the four
    lowest subcarriers (300-450 Hz), which the same experiment's per-bin SNR
    census identified as the four *weakest* of the 49 (13.7-18.1 dB against a
    ~19.5 dB median), landing at 45 carriers over 500-2700 Hz and a measured
    2,253 Hz occupied bandwidth.  The trim costs ~8% of rate and buys
    compliance plus a slightly cleaner carrier set.

  * **Air time, not frame time.** This bench spends a fixed ~155 ms per
    keying on PTT ramp and tail, which `frame_seconds()` excludes.  The frame
    below is sized so that overhead is amortized to ~3% rather than the ~5.5%
    it costs at HF6's frame size; `airtime()` still reports the waveform
    duration, per the `WaveformMode` contract and `SPEED_LADDERS.md`'s
    definition of the throughput denominator.

Default availability is a product decision, not a qualification claim: HF7's
Level-4 operating-envelope evidence has not been run under
`MODE_QUALIFICATION.md`'s rules.  See that document and
`whale/mode_qualification.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from experiments.hf10_ofdm49_v6 import ofdm49_v6 as hf7

from .. import framing


HF7_MODE_ID = 14

FFT_SIZE = 240              # 50.0 Hz subcarrier spacing at the 12 kHz design rate
CP_LEN = 24                 # 2 ms guard; 0 ms costs 6.7 dB, 5 ms costs 14% of rate
BITS_PER_SYMBOL = 5         # 32-QAM
FEC_RATE = "3/4"
PILOT_INTERVAL = 20
INTERLEAVE = True
NOISE_ESTIMATOR = "repeat"

# 500-2700 Hz: 45 carriers, 2,253 Hz measured 99%-power occupied bandwidth,
# 47 Hz inside the 2,300 Hz ceiling. See the module docstring for why the
# trim comes off the low end.
BAND_LO_HZ = 500.0
BAND_HI_HZ = 2700.0

# 4,374 B packs exactly 72 rate-3/4 LDPC codewords (k=486 information bits),
# wasting no coded bits, and puts the frame at ~4.86 s -- the size at which
# the bench's fixed per-keying overhead falls to ~3% of air time.
PACKET_BYTES = 4374

ACTIVE_BINS = tuple(hf7.bins_in_band(FFT_SIZE, BAND_LO_HZ, BAND_HI_HZ))
HF7_PHY = hf7.OFDM49Mode(
    fft_size=FFT_SIZE,
    cp_len=CP_LEN,
    active_bins=ACTIVE_BINS,
    bits_per_symbol=BITS_PER_SYMBOL,
    packet_bytes=PACKET_BYTES,
    pilot_interval=PILOT_INTERVAL,
    equalizer="gain",
    # Calibrated against this bench's audio gain structure and this waveform's
    # 8.9 dB crest factor; the harness default of 1.0 runs ~7 dB overdriven
    # and costs two constellation steps of EVM.
    drive_scale=0.008,
    fec_rate=FEC_RATE,
    interleave=INTERLEAVE,
)

CHUNK_SIZE = HF7_PHY.max_payload_bytes - framing.AIR_HEADER_BYTES
CONFIDENCE_THRESHOLD = 0.12


class Hf7Codec:
    tx_sample_rate = hf7.TX_SAMPLE_RATE
    rx_sample_rate = hf7.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hf7Mode", *, include_head=True,
               head_seconds=None) -> np.ndarray:
        del include_head, head_seconds
        if len(payload) > HF7_PHY.max_payload_bytes:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{HF7_PHY.max_payload_bytes}")
        return HF7_PHY.modulate(bytes(payload))

    def decode(self, audio, mode: "Hf7Mode", *, head_seconds=None, **kwargs) -> dict:
        del mode, head_seconds
        if np.asarray(audio).ndim != 1:
            return {"synced": False, "payload": None}
        # The soft-decision path needs the "repeat" noise estimator: the legacy
        # estimator fits the preamble against a gain derived from that same
        # preamble, so its residual is biased low and the LLRs handed to the
        # LDPC decoder are mis-scaled. Raw BER is identical either way.
        kwargs.setdefault("noise_estimator", NOISE_ESTIMATOR)
        return HF7_PHY.demodulate(audio, **kwargs)

    def airtime(self, payload_len: int, mode: "Hf7Mode") -> float:
        del payload_len, mode
        return HF7_PHY.frame_seconds()


HF7_CODEC = Hf7Codec()


@dataclass(frozen=True)
class Hf7Mode:
    name: str = "hf7"
    mode_id: int = HF7_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    fec_rate: str | None = FEC_RATE
    codec: Hf7Codec = field(default=HF7_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        return hf7.DESIGN_RATE / HF7_PHY.symbol_len

    def encode(self, payload: bytes, *, include_head=True, head_seconds=None):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HF7 = Hf7Mode()
