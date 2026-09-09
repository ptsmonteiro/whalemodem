"""HF8: the robust 8PSK sibling of HF7, on HF7's own 49-carrier OFDM plan.

HF8 wires the configuration measured in `experiments/hf19_ofdm49_8psk/` into
the link's `WaveformMode` contract, mirroring `whale/modes/hf7_mode.py`'s
pattern; it does not re-derive or re-run that design or its evidence.

The waveform is `whale/phy/ofdm49.py` (developed in the retired
`experiments/hf10_ofdm49_v6/`) -- unmodified, the same PHY HF6
and HF7 already wrap -- on **exactly HF7's carrier plan and
guard**: 50 Hz subcarrier spacing (`fft_size=240` at the 12 kHz design rate),
a 2 ms guard, 49 carriers filling 300-2700 Hz edge to edge.  Four things
change:

  * **8PSK instead of 32-QAM** (`bits_per_symbol=3` against HF7's 5).  This
    is the whole AWGN-floor story: every 8PSK arm in the screening grid,
    across every guard, code rate and pilot spacing tried, landed on the same
    AWGN boundary, and none of those other axes moved it by a grid step.
  * **Rate-2/3 LDPC instead of rate-3/4**,
  * **a full pilot symbol every 10 data symbols instead of every 20**, and
  * **a 0.616 s frame instead of 4.84 s** -- see `PACKET_BYTES` below, where
    the reasoning is the opposite of HF7's and matters more than the other
    three put together on a fading channel.

What that buys, at 40 trials per point (full tables in that experiment's
RESULTS.md):

  * **AWGN: 90% delivery from 12 dB, against HF7's 20 dB -- 8 dB of floor.**
  * **Quiet Watterson (0.5 ms / 0.1 Hz): qualified from 18 dB.  HF7 never
    reaches 90% on that channel at any SNR tested, to 24 dB, where it manages
    7/40.**  This is the point of the mode: `MODE_QUALIFICATION.md` records
    that HF7 has no measured fading envelope at all and should be expected to
    fail there.  18 dB is the `MODE_QUALIFICATION.md` section 3 boundary,
    measured 2026-09-08 at 300 trials (95.0% delivered, Wilson lower 91.9%,
    so FER upper 8.1% against the 10% limit); 16 dB fails it at 90.0%
    delivered, Wilson lower 86.1%.  Note this is 2 dB worse than the 40-trial
    screen originally claimed -- the screen read the raw 90.0% at 16 dB as a
    pass.  18 dB still clears the +19 dB quiet point `SPEED_LADDERS.md` sets
    for its Level-3 "fast data" rung, at 3,298 bit/s against that rung's
    2,000 bit/s floor.
  * **Moderate Watterson (1.0 ms / 0.5 Hz) is NOT in the envelope**, though
    it is no longer dead: 50% delivery from 16 dB and 68% at 24 dB, against
    HF7's 0/40 everywhere.  Disturbed is not in the envelope either.  The
    ladder, not this mode, is responsible for those channels.

Three negative results from the same experiment are worth stating here,
because each looks like an obvious improvement and each is wrong on this PHY:

  * **Comb pilots make it worse.**  Every `pilot_comb_stride` arm lost on
    every channel *and* gave up rate; the no-comb arm won outright.  The
    `comb_tracking="legacy"` path is worse still -- it fails on AWGN alone --
    because it divides by the comb reference without the phase schedule and
    taper the transmitter applies a second time to those bins.
  * **A longer guard does not help.**  3 ms and 4 ms guards were screened
    against HF7's 2 ms and neither improved any boundary; the frequency
    selectivity that limits this waveform under fading is not an
    intersymbol-interference problem the guard can absorb.
  * **A lower code rate does not reach moderate fading either.**  Rate 1/2
    costs a further 24% of the rate against 2/3 and left every fading
    boundary exactly where it was.

**On the radios, in both directions, the floor claim holds and then some.**
Interleaved transmit-drive ladders against HF7 on 2026-09-07:

  * IC-7300 -> IC-705: HF8's 90%-delivery breakpoint is **12 dB of transmit
    audio below HF7's** (9/10 at -12 dB where HF7 was 0/10), against the 8 dB
    simulated; at equal drive HF8's raw BER was 0.0000 to HF7's 0.011-0.018.
  * IC-705 -> IC-7300, the weaker path: **HF7 does not deliver a single frame
    at any drive level**, while HF8 delivers 10/10 with 3 dB to spare.  HF7
    reported 3.4 dB *more* SNR than HF8 at every level and still failed --
    32-QAM at 5.8% raw BER where 8PSK sat at 0.8%.  That is a real path
    between two real radios on which the installed default top rung does not
    work and this mode does.

See `logs/mode_qualification/hf-ssb/hf19/20260907T194237Z-drive-sweep/` and
`.../20260907T195827Z-ba-drive-sweep/`.

**The fading claims, which are the reason this mode exists, remain
simulation only.**  Both benches are benign audio-coupled paths with no
ionosphere in them, and the drive multiplier is not a calibrated SNR
reference.  `docs/SIMULATION_RADIO_DIFFERENCES.md` collects this project's
repeated finding that simulated-channel results are code checks rather than
radio predictions -- including, on this very PHY family, a 16-QAM
configuration that passed simulation and was 1/5 on the air.

HF8 was installed as a **DEFAULT** rung on 2026-09-07 by owner decision, on
the hardware evidence above, and sits between HC1W and HF7 in the rate-ordered
ladder.  Default is availability, not qualification: the operating-envelope
gates -- above all any hardware evidence under fading -- are open.  See
`MODE_QUALIFICATION.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from whale.phy import ofdm49 as hf8

from .. import framing
from . import hf_lead


HF8_MODE_ID = 15

FFT_SIZE = 240              # 50.0 Hz subcarrier spacing at the 12 kHz design rate
CP_LEN = 24                 # 2 ms guard, HF7's; 3 ms and 4 ms bought nothing
BITS_PER_SYMBOL = 3         # 8PSK -- the 8 dB of floor over HF7's 32-QAM
FEC_RATE = "2/3"
PILOT_INTERVAL = 10         # HF7 pilots every 20; half that is the fading half
INTERLEAVE = True
NOISE_ESTIMATOR = "repeat"

# 300-2700 Hz: 49 carriers, the full HF channel, HF7's plan unchanged.
BAND_LO_HZ = 300.0
BAND_HI_HZ = 2700.0

# 270 B is 5 whole rate-2/3 LDPC codewords (k=432 information bits) with no
# padding at all, and puts the frame at 0.616 s.
#
# The frame length is not an overhead decision here, the way HF7's 4,738 B is.
# It is the single largest lever on this mode's fading envelope, measured:
# holding the waveform fixed and varying only the frame, quiet-Watterson 90%
# delivery moves 24 dB -> 20 -> 16 as the frame shortens 2.090 s -> 1.144 ->
# 0.616, and at 4.862 s it is never reached at all.  Long frames straddle deep
# fades; no amount of coding inside one recovers a frame that spent a whole
# codeword in a null.  HF7's reasoning -- lengthen the frame until the fixed
# ~155 ms per-keying cost amortizes to ~3% -- is right for a mode whose job is
# peak rate on a good path and wrong for this one, which spends that
# throughput on margin deliberately.  The cost is real and should be quoted:
# the same ~155 ms is ~20% of this frame on air, so ~3,298 bit/s of waveform
# throughput is ~2,614 bit/s of keyed-air throughput.  `airtime()` reports
# waveform duration, per the `WaveformMode` contract and SPEED_LADDERS.md.
PACKET_BYTES = 2460

ACTIVE_BINS = tuple(hf8.bins_in_band(FFT_SIZE, BAND_LO_HZ, BAND_HI_HZ))
HF8_PHY = hf8.OFDM49Mode(
    fft_size=FFT_SIZE,
    cp_len=CP_LEN,
    active_bins=ACTIVE_BINS,
    bits_per_symbol=BITS_PER_SYMBOL,
    packet_bytes=PACKET_BYTES,
    pilot_interval=PILOT_INTERVAL,
    equalizer="gain",
    # HF7's calibration, for HF7's bench and audio gain structure. 8PSK's
    # crest factor is 0.8 dB above HF7's 8.9 dB, so this is if anything
    # slightly conservative. Like HF7's, it does not generalize to another
    # station's audio path; the right fix is drive from measured EVM.
    drive_scale=0.008,
    fec_rate=FEC_RATE,
    interleave=INTERLEAVE,
)

CHUNK_SIZE = HF8_PHY.max_payload_bytes - framing.AIR_HEADER_BYTES
CONFIDENCE_THRESHOLD = 0.12


class Hf8Codec:
    tx_sample_rate = hf8.TX_SAMPLE_RATE
    rx_sample_rate = hf8.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hf8Mode", *, include_head=True,
               head_seconds=None) -> np.ndarray:
        if len(payload) > HF8_PHY.max_payload_bytes:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{HF8_PHY.max_payload_bytes}")
        # Keep the minimum common lead when include_head=False; disabling the
        # negotiated extension must not create a headless shipped waveform.
        if not include_head or head_seconds is None:
            head_seconds = hf_lead.MIN_SECONDS
        lead = hf_lead.modulate(hf_lead.HF8_LABEL, head_seconds)
        return np.concatenate((lead, HF8_PHY.modulate(bytes(payload))))

    def decode(self, audio, mode: "Hf8Mode", *, head_seconds=None, **kwargs) -> dict:
        del mode
        if np.asarray(audio).ndim != 1:
            return {"synced": False, "payload": None}
        # The soft-decision path needs the "repeat" noise estimator, for the
        # reason HF7 records: the legacy estimator fits the preamble against a
        # gain derived from that same preamble, so its residual is biased low
        # and the LLRs handed to the LDPC decoder are mis-scaled.
        kwargs.setdefault("noise_estimator", NOISE_ESTIMATOR)
        # Strip the latest matching lead boundary before OFDM acquisition so
        # the MFSK head cannot win the OFDM preamble search. Erased or clipped
        # leads still use the body-only acquisition fallback.
        captured = np.asarray(audio)
        lead_candidates = hf_lead.measured_candidates(
            captured, hf_lead.HF8_LABEL, head_seconds)
        result = None
        body_start = None
        for candidate, body_offset in lead_candidates:
            attempt = HF8_PHY.demodulate(captured[body_offset:], **kwargs)
            local_start = attempt.get("start_sample")
            if local_start is not None:
                body_start = body_offset + local_start
                attempt["start_sample"] = body_start
            result = attempt
            if result.get("payload") is not None and body_start is not None:
                break
        if result is None or result.get("payload") is None:
            result = HF8_PHY.demodulate(captured, **kwargs)
            body_start = result.get("start_sample")
        if result.get("payload") is not None and body_start is not None:
            result["start_index"] = body_start
            observed, score = hf_lead.measure(
                captured, body_start, hf_lead.HF8_LABEL, head_seconds)
            result.update(
                head_blocks_observed=observed,
                head_seconds_received=hf_lead.seconds_received(observed),
                head_match=score)
        return result

    def airtime(self, payload_len: int, mode: "Hf8Mode") -> float:
        del payload_len, mode
        return hf_lead.MIN_SECONDS + HF8_PHY.frame_seconds()


HF8_CODEC = Hf8Codec()


@dataclass(frozen=True)
class Hf8Mode:
    name: str = "hf8"
    mode_id: int = HF8_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    lead_label: int = hf_lead.HF8_LABEL
    fec_rate: str | None = FEC_RATE
    codec: Hf8Codec = field(default=HF8_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        return hf8.DESIGN_RATE / HF8_PHY.symbol_len

    @property
    def head_match_allowance_seconds(self) -> float:
        """One common HF lead block, the measurement resolution."""
        return hf_lead.BLOCK_SAMPLES / self.tx_sample_rate

    def encode(self, payload: bytes, *, include_head=True, head_seconds=None):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HF8 = Hf8Mode()
