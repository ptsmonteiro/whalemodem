"""HF2: pilot-assisted coherent 16-QAM OFDM, targeting Speed Ladder Level 2.

See `experiments/hf2/PLAN.md` and `DESIGN.md` for the experiment this
waveform belongs to and why each choice below was made; this module is
stage 2 of that plan -- the raw waveform (geometry, mapping, FEC, framing,
acquisition/timing/frequency recovery) -- not the `WaveformMode` promotion,
which is a later stage.

Like HC1 and VF3, this is geometry and wiring on top of the shared
`whale/dsp/` kernels: OFDM symbol build/analyze (`whale.dsp.ofdm`), the
rate-1/2 K=7 convolutional code and CRC32/length framing
(`whale.dsp.fec`, `whale.dsp.framing`), acquisition
(`whale.dsp.acquire`), frequency and timing recovery (`whale.dsp.freq`,
`whale.dsp.timing`), and header equalization (`whale.dsp.equalize`).  Every
geometry number, the pilot layout and the 16-QAM mapping are HF2's own,
picked independently of HC0/HC1/VF6/HR0 per DESIGN.md.

Frame shape:

    [SYNC_SYMBOLS identical][TRAINING_SYMBOLS varying][PAYLOAD_SYMBOLS]

`SYNC_SYMBOLS` are what acquisition's self-correlation locks onto (identical
QPSK, all 19 carriers).  `TRAINING_SYMBOLS` are a second, varying, still
fully-known QPSK block: `whale.dsp.equalize.fit_header` uses it for the
initial per-carrier gain/offset/SNR and `whale.dsp.freq.fine_offset_hz` uses
it for the fine carrier-frequency estimate, the same two jobs HC1's header
does. Together they are `HEADER_SYMBOLS`.

Each of the `PAYLOAD_SYMBOLS` OFDM symbols carries 19 carriers split into 8
fixed-value BPSK comb pilots (bins 7, 10, 12, 15, 17, 20, 22, 25 -- roughly
evenly spread across the 19-carrier band) and 11 16-QAM (Gray-coded, 4
bits/carrier) data carriers. This is denser than the original 4-pilot/
15-data-carrier layout; see DESIGN.md's stage-4 dated note for why (a
Watterson channel-tracking bug found on the stage-3 screen, fixed in three
parts: fresh per-symbol pilot-based channel estimation rather than a ratio
against a stale header fit, a widened pilot comb, and per-symbol per-carrier
soft-bit reliability weighting -- see `_pilot_channel_estimate` and
`demodulate`'s docstrings/comments for the mechanism).  `whale.dsp.equalize`
does not ship a ready-made frequency-axis interpolator (`pilot_phase` tracks
a *time*-axis phase from periodic full-pilot symbols, which is not this
design's comb-pilot layout, and `fit_header`'s single static per-carrier
fit is used only for the header's own SNR/present-carrier diagnostics, not
for equalizing the payload), so per-symbol pilot tracking is HF2-local glue.

Coding is `whale.dsp.framing.PacketCodec` unchanged: length field, CRC32,
whitening, rate-1/2 K=7 soft-Viterbi and a multiplicative bit interleaver,
all inside the payload grid (`whale.framing`'s PN-sync format is bypassed,
the same choice VF3/HC0/HC1 each made independently).

Frame size: DESIGN.md's starting point of 40 payload symbols does not
divide into a whole number of packet bytes at any pilot/data-carrier split
tried. A rate-1/2 K=7 grid with 11 data carriers (44 raw bits/symbol) needs
`payload_symbols * 22 - 6` divisible by 8, i.e. `payload_symbols % 4 == 1`;
45 is the nearest value satisfying that at/above the 40-symbol starting
point, and is used here.  See DESIGN.md's "Implementation note" and stage-4
dated note for the record of this and the pilot-count deviations.

The shared HF lead-in (`whale.modes.hf_lead`, label `HF2_LABEL`) is
prepended by `modulate` and measured by `demodulate`, the same calling
convention `whale/modes/hc1_mode.py` uses around `whale/modes/hc1.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import hilbert

from whale import dsp, rx_audio
from whale.dsp import (acquire as _acquire_kernel, equalize as _eq,
                       freq as _freq, ofdm as _ofdm, timing as _timing)
from whale.modes import hf_lead

# -- geometry ---------------------------------------------------------------

SAMPLE_RATE = 48_000
CORE_SAMPLES = 512
GUARD_SAMPLES = 128
SYMBOL_SAMPLES = GUARD_SAMPLES + CORE_SAMPLES
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
RX_CORE_SAMPLES = CORE_SAMPLES // rx_audio.DECIMATION
RX_GUARD_SAMPLES = GUARD_SAMPLES // rx_audio.DECIMATION
RX_SYMBOL_SAMPLES = RX_GUARD_SAMPLES + RX_CORE_SAMPLES

#: 19 carriers at 93.75 Hz spacing, 656.25-2343.75 Hz.  See DESIGN.md for why
#: this band, arrived at independently of HC1's identical-looking one.
CARRIER_BINS = np.arange(7, 26, dtype=np.int32)
CARRIER_SPACING_HZ = SAMPLE_RATE / CORE_SAMPLES
CARRIER_HZ = CARRIER_BINS.astype(np.float64) * CARRIER_SPACING_HZ
N_CARRIERS = len(CARRIER_BINS)

#: 8 comb pilots spread evenly across the 19-carrier band (local indices
#: 0, 3, 5, 8, 10, 13, 15, 18 -> bins 7, 10, 12, 15, 17, 20, 22, 25), 11
#: data carriers. Widened twice from the original 4-pilot/15-data-carrier
#: layout -- see DESIGN.md's dated implementation note: moderate
#: Watterson's 1 ms delay spread produces sharp local nulls (confirmed by
#: instrumentation -- see `_pilot_channel_estimate`'s docstring) that a
#: 6-bin comb still missed. ~2.4-bin spacing keeps the equalizer's
#: interpolation gap small enough that soft-bit reliability weighting
#: (also added, see `demodulate`) can do the rest.
PILOT_LOCAL_INDEX = np.array([0, 3, 5, 8, 10, 13, 15, 18], dtype=np.int64)
DATA_LOCAL_INDEX = np.array(
    [i for i in range(N_CARRIERS) if i not in set(PILOT_LOCAL_INDEX.tolist())],
    dtype=np.int64)
N_PILOTS = len(PILOT_LOCAL_INDEX)
N_DATA_CARRIERS = len(DATA_LOCAL_INDEX)
PILOT_BINS = CARRIER_BINS[PILOT_LOCAL_INDEX]
DATA_BINS = CARRIER_BINS[DATA_LOCAL_INDEX]

BITS_PER_DATA_CARRIER = 4  # 16-QAM, Gray-coded, 2 bits/axis

#: Frequency-diversity carrier groups (stage-4b fix, see DESIGN.md's
#: second dated note). Instrumentation confirmed the multiplicative
#: interleaver already spreads one dead carrier's coded bits perfectly
#: evenly across the whole codeword (every 44th trellis position, out of
#: 1980) -- clustering was NOT the problem. The problem is that a
#: persistent Watterson notch erases ~9-18% of ALL codeword bits for the
#: entire frame, evenly spread or not, which the rate-1/2 code's erasure
#: capacity does not fully absorb. The fix is physical frequency
#: diversity: each of the 11 data carriers is assigned to one of 5
#: *logical* carriers, each backed by 2-3 physical carriers spread far
#: apart in frequency (low half paired against high half). The same
#: 16-QAM value is transmitted on every physical carrier in a group; the
#: receiver sums the per-carrier soft-bit LLRs (already weighted by each
#: physical carrier's own fresh per-symbol pilot-based reliability, see
#: `demodulate`) before Viterbi decoding, so a notch on one physical
#: carrier of a group is masked by its far-apart partner(s) unless both
#: fade at once -- much less likely than either alone under a spatially
#: local notch. This trades raw bits/symbol (44 -> 20) for that
#: robustness; `PAYLOAD_SYMBOLS` grows so the same 1980-bit coded frame
#: (same `MAX_PAYLOAD_BYTES`) still fits, at higher airtime cost.
#: Local data-carrier indices (into `DATA_LOCAL_INDEX`, ordered by bin):
#: low half [1,2,4,6,7] (bins 8,9,11,13,14), high half [17,16,14,12,11,9]
#: (bins 24,23,21,19,18,16, descending so pairing is low[i]<->high[i]).
#: Group 0 additionally carries the leftover high-half carrier (bin 16)
#: as a third copy, using all 11 physical data carriers with none idle.
DATA_GROUPS = (
    (1, 17, 9),   # bins 8, 24, 16 -- group 0, triple diversity
    (2, 16),      # bins 9, 23
    (4, 14),      # bins 11, 21
    (6, 12),      # bins 13, 19
    (7, 11),      # bins 14, 18
)
N_LOGICAL_CARRIERS = len(DATA_GROUPS)
#: local-data-carrier-index -> logical group index, for RX combining.
GROUP_OF_DATA_LOCAL = {local: g for g, group in enumerate(DATA_GROUPS)
                       for local in group}
assert sorted(GROUP_OF_DATA_LOCAL) == sorted(DATA_LOCAL_INDEX.tolist())

RAW_BITS_PER_SYMBOL = N_LOGICAL_CARRIERS * BITS_PER_DATA_CARRIER

SYNC_SYMBOLS = 4
TRAINING_SYMBOLS = 6
HEADER_SYMBOLS = SYNC_SYMBOLS + TRAINING_SYMBOLS
#: 1980 coded bits (990 information bits, same packet size as the stage-4a
#: fix) at 20 raw bits/symbol (5 logical carriers x 4 bits) needs exactly
#: 99 payload symbols; kept as an explicit constant, not derived, so a
#: future change to the coded-bit count is forced to re-derive this.
PAYLOAD_SYMBOLS = 99
TOTAL_SYMBOLS = HEADER_SYMBOLS + PAYLOAD_SYMBOLS
PAYLOAD_BITS = PAYLOAD_SYMBOLS * RAW_BITS_PER_SYMBOL

TX_RMS = 0.13
MAX_SAMPLE = 0.95

GEOMETRY = _ofdm.Geometry(
    sample_rate=SAMPLE_RATE, core_samples=CORE_SAMPLES,
    guard_samples=GUARD_SAMPLES, carrier_bins=CARRIER_BINS,
).scaled_to_rms(TX_RMS)
RX_GEOMETRY = _ofdm.Geometry(
    sample_rate=RX_SAMPLE_RATE, core_samples=RX_CORE_SAMPLES,
    guard_samples=RX_GUARD_SAMPLES, carrier_bins=CARRIER_BINS)

TAIL_SAMPLES = 960
RX_TAIL_SAMPLES = TAIL_SAMPLES // rx_audio.DECIMATION
FFT_OFFSET = GUARD_SAMPLES
RX_FFT_OFFSET = RX_GUARD_SAMPLES

ACQUISITION_THRESHOLD = 0.70
MIN_PRESENT_CARRIERS = 17
CARRIER_FLOOR_DB = 35.0

COARSE_OFFSET_LIMIT_HZ = SAMPLE_RATE / (2.0 * CORE_SAMPLES)
FINE_OFFSET_LIMIT_HZ = SAMPLE_RATE / (2.0 * SYMBOL_SAMPLES)


def lead_in_samples(head_seconds: float | None = None) -> int:
    """Leading `hf_lead` samples for a requested head duration."""
    return hf_lead.lead_samples(head_seconds)


DEFAULT_HEAD_SECONDS = hf_lead.MIN_SECONDS


def frame_samples(head_seconds: float = DEFAULT_HEAD_SECONDS) -> int:
    return (lead_in_samples(head_seconds)
            + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES)


def frame_seconds(head_seconds: float = DEFAULT_HEAD_SECONDS) -> float:
    return frame_samples(head_seconds) / SAMPLE_RATE


# -- reference constellations and the payload codec --------------------------

SYNC_VALUES = dsp.bits.qpsk_from_bits(
    dsp.bits.pn_bits(2 * N_CARRIERS, 0x0F2B1))
TRAINING_VALUES = dsp.bits.qpsk_from_bits(
    dsp.bits.pn_bits(TRAINING_SYMBOLS * 2 * N_CARRIERS, 0x134A9)
).reshape(TRAINING_SYMBOLS, N_CARRIERS)
HEADER_VALUES = np.vstack((
    np.tile(SYNC_VALUES, (SYNC_SYMBOLS, 1)), TRAINING_VALUES))

#: Fixed-value BPSK comb pilots, constant across every payload symbol (real
#: valued, unit magnitude -- matching 16-QAM's unit average carrier energy so
#: the frame's per-carrier power is roughly even).
_pilot_bits = dsp.bits.pn_bits(N_PILOTS, 0x02D7B)
PILOT_VALUES = (1.0 - 2.0 * _pilot_bits.astype(np.float64)).astype(np.complex128)

#: Coprime with PAYLOAD_BITS (2,460 = 2^2 * 3 * 5 * 41); 937 is prime and
#: shares no factor with it, so `multiplicative` accepts it as a permutation.
INTERLEAVER_STRIDE = 937

CODEC = dsp.PacketCodec(
    payload_bits=PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS, INTERLEAVER_STRIDE),
    whitener_seed=0x02B41,
    code=dsp.K7,
)

FEC_INPUT_BITS = CODEC.information_bits
FEC_TAIL_BITS = CODEC.code.tail_bits
PACKET_BYTES = CODEC.packet_bytes
UNUSED_INFO_BITS = CODEC.unused_information_bits
MAX_PAYLOAD_BYTES = CODEC.max_payload_bytes

encode_payload_bits = CODEC.encode
decode_payload_soft = CODEC.decode_soft


# -- 16-QAM Gray mapping ------------------------------------------------------

#: Gray-coded 4-PAM levels, unit average energy: bits (b0, b1) -> level,
#: index by 2*b0 + b1 through the lookup below.
_PAM4_LEVELS = np.array([-3.0, -1.0, 1.0, 3.0]) / np.sqrt(10.0)
#: (b0, b1) -> level index.  Adjacent levels differ by one bit each step:
#: -3(00) -1(01) 1(11) 3(10).
_PAM4_INDEX = {(0, 0): 0, (0, 1): 1, (1, 1): 2, (1, 0): 3}


def _pam4_from_bits(pair_bits: np.ndarray) -> np.ndarray:
    """`(..., 2)` bit pairs -> Gray-coded 4-PAM levels."""
    b0 = pair_bits[..., 0].astype(np.int64)
    b1 = pair_bits[..., 1].astype(np.int64)
    index = np.where(b0 == 0, np.where(b1 == 0, 0, 1), np.where(b1 == 1, 2, 3))
    return _PAM4_LEVELS[index]


def qam16_from_bits(bits: np.ndarray) -> np.ndarray:
    """`(4 * n,)` bits, carrier-major `[Ib0, Ib1, Qb0, Qb1]` -> `(n,)` 16-QAM."""
    bits = np.asarray(bits, dtype=np.uint8).reshape(-1, 4)
    i_level = _pam4_from_bits(bits[:, 0:2])
    q_level = _pam4_from_bits(bits[:, 2:4])
    return i_level + 1j * q_level


def qam16_soft_bits(values: np.ndarray, weight: np.ndarray | float = 1.0
                    ) -> np.ndarray:
    """Approximate per-bit LLRs for Gray 16-QAM; positive means bit zero.

    `b0` (the outer/inner sign) is read straight off the axis sample; `b1`
    (inner vs. outer pair) off how far the sample sits from the two-level
    boundary at |level| = 2/sqrt(10).  Both are the standard first-order
    Gray-PAM soft metrics, undone from HF2's unit-energy normalization.
    """
    values = np.asarray(values, dtype=np.complex128)
    scaled = values * np.sqrt(10.0)
    real, imag = scaled.real, scaled.imag
    weight = np.asarray(weight, dtype=np.float64)
    i_b0 = -real * weight
    i_b1 = (np.abs(real) - 2.0) * weight
    q_b0 = -imag * weight
    q_b1 = (np.abs(imag) - 2.0) * weight
    return np.stack((i_b0, i_b1, q_b0, q_b1), axis=-1).reshape(-1)


# -- modulation ---------------------------------------------------------------

def build_symbol(values: np.ndarray) -> np.ndarray:
    return _ofdm.build_symbol(GEOMETRY, values)


def symbol_carriers(symbol_audio: np.ndarray,
                    offset: int = FFT_OFFSET) -> np.ndarray:
    return _ofdm.symbol_carriers(GEOMETRY, symbol_audio, offset)


def frame_constellation(payload: bytes) -> np.ndarray:
    coded = encode_payload_bits(payload)
    logical_values = qam16_from_bits(coded).reshape(
        PAYLOAD_SYMBOLS, N_LOGICAL_CARRIERS)
    payload_grid = np.empty((PAYLOAD_SYMBOLS, N_CARRIERS), dtype=np.complex128)
    payload_grid[:, PILOT_LOCAL_INDEX] = PILOT_VALUES[None, :]
    # Frequency diversity: every physical carrier in a `DATA_GROUPS` group
    # carries the same logical carrier's 16-QAM value -- see the constant's
    # docstring above.
    for g, group in enumerate(DATA_GROUPS):
        for local in group:
            payload_grid[:, local] = logical_values[:, g]
    return np.vstack((HEADER_VALUES, payload_grid))


def modulate(payload: bytes, *,
            head_seconds: float = DEFAULT_HEAD_SECONDS) -> np.ndarray:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"payload is {len(payload)} bytes; the maximum is "
            f"{MAX_PAYLOAD_BYTES}")
    values = frame_constellation(payload)
    symbols = np.concatenate([build_symbol(row) for row in values])
    lead = hf_lead.modulate(hf_lead.HF2_LABEL, head_seconds)
    audio = np.concatenate((lead, symbols, np.zeros(TAIL_SAMPLES)))
    if len(audio) != frame_samples(head_seconds):
        raise AssertionError(f"internal frame length error: {len(audio)}")
    peak = float(np.max(np.abs(audio)))
    if peak > MAX_SAMPLE:
        audio *= MAX_SAMPLE / peak
    return audio.astype(np.float32)


# -- demodulation --------------------------------------------------------------

_TIMING_SYMBOLS = np.arange(SYNC_SYMBOLS, TOTAL_SYMBOLS, dtype=np.int32)
_HEADER_CP_SYMBOLS = np.arange(HEADER_SYMBOLS, dtype=np.int32)


def _coarse_offset(analytic: np.ndarray, start: int) -> float:
    return _freq.coarse_offset_hz(RX_GEOMETRY, analytic, start,
                                  _HEADER_CP_SYMBOLS)


def _remove_residual_offset(carriers: np.ndarray, offset_hz: float,
                            start: int, shifts: np.ndarray) -> np.ndarray:
    indices = np.arange(len(carriers))
    window_start = (start + indices * RX_SYMBOL_SAMPLES + shifts + RX_FFT_OFFSET)
    phase = np.exp(-2j * np.pi * offset_hz * window_start / RX_SAMPLE_RATE)
    return carriers * phase[:, None]


def _header_candidate_snr(analytic: np.ndarray, start: int) -> float:
    span = HEADER_SYMBOLS * RX_SYMBOL_SAMPLES
    if start < 0 or start + span > len(analytic):
        return -np.inf
    offset = _coarse_offset(analytic, start)
    header = _freq.derotate(analytic[start:start + span], offset, RX_SAMPLE_RATE)
    observed = _ofdm.carrier_bank(RX_GEOMETRY, header, 0, HEADER_SYMBOLS,
                                  offset=RX_FFT_OFFSET)
    if observed is None:
        return -np.inf
    return _eq.header_snr(observed, HEADER_VALUES)


def _acquire(analytic: np.ndarray) -> tuple[int | None, float]:
    return _acquire_kernel.acquire(
        RX_GEOMETRY, analytic, sync_symbols=SYNC_SYMBOLS,
        rank=lambda start: _header_candidate_snr(analytic, start))


def _pilot_channel_estimate(payload_raw: np.ndarray,
                            offset: np.ndarray) -> np.ndarray:
    """Fresh per-symbol, carrier-interpolated complex gain from comb pilots.

    See the module docstring for the original design (a per-symbol
    correction *on top of* the header's per-carrier gain, i.e. dividing the
    already-header-equalized payload by the pilots' observed/expected
    ratio). Two dated rounds of diagnosis on the Watterson screen (see
    DESIGN.md) found that design fundamentally broken, not just tunable:

    1. A first attempt weighted/dropped pilots by header-fit SNR. That
       still failed, because a header-time fade at a *data* carrier's
       nearest pilot is exactly the situation where that pilot carries the
       most locally relevant information -- discounting or dropping it to
       "protect" the fit removes the one nearby reading that would have
       tracked the local notch, and interpolating across the resulting gap
       from distant, unrelated pilots is worse, not better.
    2. The deeper problem: `fit_header` gives every carrier ONE static
       gain from a 10-symbol (~130 ms) block at the very start of a
       43-symbol (~700 ms) frame. Under Watterson fading a carrier's true
       gain can move several-fold across that span (confirmed by
       instrumentation: a carrier's header-equalized magnitude drifting
       from ~2x to ~9x over one frame while its neighboring pilot's ratio
       climbed from ~3x to ~18x in lockstep). Computing a payload
       correction as a ratio *relative to* that stale header gain chains
       two different times' channel estimates multiplicatively and
       amplifies the mismatch instead of removing it.

    The fix: do not touch the header's per-carrier gain for the payload
    path at all (it is still used for acquisition ranking, present-carrier
    counting and the SNR-based soft-bit weights below, all of which only
    need a coarse read on the header block). Instead, for every payload
    symbol independently, read each pilot's *own* complex gain fresh
    (`(raw - header offset) / known pilot value`) and interpolate that
    across the carrier axis to the data bins -- a single division at the
    time that matters, not a ratio of two.

    Interpolation across the carrier axis is plain piecewise-linear
    (`np.interp`, real/imag independently) between consecutive pilots, per
    symbol. An earlier attempt weighted this fit by each pilot's own
    instantaneous magnitude (as a per-symbol reliability proxy) on the
    theory that a pilot in a momentary fade should count less; measured
    against a noiseless (40 dB) genie reference it made the residual worse,
    not better (0.40-0.60 RMS vs 0.27 unweighted) -- magnitude-based
    weighting biases the fit towards the *strong* pilots' positions rather
    than suppressing noise, which is the wrong asymmetry when carriers
    genuinely differ in gain by design (this is fading, not a noise
    estimate). Plain interpolation, unweighted, measured best.

    What is left after this (confirmed instrumentally, see the dated
    DESIGN.md note): a genuine, irreducible-by-interpolation residual from
    sharp local nulls in the Watterson frequency response -- near a null
    the channel's phase can turn on a single carrier bin, which no
    piecewise-linear or even much denser pilot spacing fully captures
    (tested up to a 10-pilot/9-data comb: residual dropped but did not
    vanish). That residual is handled downstream, not here: `demodulate`
    weights each (symbol, carrier)'s soft bits by this same per-symbol
    pilot-interpolated gain, so the FEC discounts exactly the carriers
    passing through a fade at that instant instead of trusting a
    equalized value that a null has made unreliable.
    """
    payload_raw = np.asarray(payload_raw, dtype=np.complex128)
    pilot_observed = payload_raw[:, PILOT_LOCAL_INDEX] - offset[None, PILOT_LOCAL_INDEX]
    pilot_gain = pilot_observed / PILOT_VALUES[None, :]

    x = PILOT_BINS.astype(np.float64)
    symbols = payload_raw.shape[0]
    correction = np.empty((symbols, N_DATA_CARRIERS), dtype=np.complex128)
    for s in range(symbols):
        correction[s] = (
            np.interp(DATA_BINS, x, pilot_gain[s].real)
            + 1j * np.interp(DATA_BINS, x, pilot_gain[s].imag))
    return correction


def _base_result() -> dict:
    return {
        "synced": False, "payload": None, "confidence": 0.0,
        "start_index": None, "cfo_hz": 0.0, "clock_offset_ppm": 0.0,
        "carrier_snr_db": np.full(N_CARRIERS, -np.inf),
    }


def demodulate(audio: np.ndarray, *,
              head_seconds: float = DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
    """Decode one HF2 frame out of `audio` (at `RX_SAMPLE_RATE`).

    Returns at least `{"synced", "payload", "start_index"}` plus diagnostics
    (`confidence`, `carrier_snr_db`, `cfo_hz`, ...), matching the shape
    `whale/modes/hc1_mode.py`'s codec reads off `hc1.demodulate`.
    """
    del kwargs
    result = _base_result()
    try:
        samples = np.asarray(audio, dtype=np.float64).reshape(-1)
    except Exception:
        result["failure"] = "unusable input"
        return result
    if samples.ndim != 1 or not np.all(np.isfinite(samples)):
        result["failure"] = "unusable input"
        return result
    if len(samples) < HEADER_SYMBOLS * RX_SYMBOL_SAMPLES:
        result["failure"] = "capture shorter than header"
        return result

    analytic = hilbert(samples)
    start, confidence = _acquire(analytic)
    result.update(confidence=confidence, start_index=start)
    if start is None or confidence < ACQUISITION_THRESHOLD:
        result["failure"] = "header not found"
        return result
    result["sync_end_index"] = start + HEADER_SYMBOLS * RX_SYMBOL_SAMPLES

    coarse_hz = _coarse_offset(analytic, start)
    corrected = _freq.derotate(analytic, coarse_hz, RX_SAMPLE_RATE)
    result["coarse_cfo_hz"] = coarse_hz

    fit = _timing.estimate(RX_GEOMETRY, corrected, start, _TIMING_SYMBOLS)
    result["timing_drift_samples"] = fit.drift_samples(TOTAL_SYMBOLS)
    result["timing_confidence"] = fit.confidence

    carriers = _ofdm.carrier_bank(RX_GEOMETRY, corrected, start, TOTAL_SYMBOLS,
                                  fit.intercept, fit.slope, RX_FFT_OFFSET)
    if carriers is None:
        result["failure"] = "frame truncated"
        return result
    result["end_index"] = min(
        len(samples),
        start + TOTAL_SYMBOLS * RX_SYMBOL_SAMPLES + RX_TAIL_SAMPLES)

    shifts = np.array([fit.shift_at(i) for i in range(TOTAL_SYMBOLS)])
    fine_hz = _freq.fine_offset_hz(RX_GEOMETRY, carriers[:HEADER_SYMBOLS],
                                   HEADER_VALUES)
    carriers = _remove_residual_offset(carriers, fine_hz, start, shifts)
    result["fine_cfo_hz"] = fine_hz
    result["cfo_hz"] = coarse_hz + fine_hz
    result["clock_offset_ppm"] = fit.clock_offset_ppm(RX_GEOMETRY)

    channel = _eq.fit_header(carriers[:HEADER_SYMBOLS], HEADER_VALUES)
    present = channel.present_carriers(CARRIER_FLOOR_DB)
    result["present_carriers"] = present
    if present < MIN_PRESENT_CARRIERS:
        result["failure"] = f"header has only {present}/{N_CARRIERS} carriers"
        return result
    result["carrier_snr_db"] = channel.snr_db

    payload_raw = carriers[HEADER_SYMBOLS:]
    pilot_gain = _pilot_channel_estimate(payload_raw, channel.offset)
    safe_gain = np.where(np.abs(pilot_gain) > 1e-6, pilot_gain, 1.0)
    data_equalized = (
        (payload_raw[:, DATA_LOCAL_INDEX] - channel.offset[None, DATA_LOCAL_INDEX])
        / safe_gain)

    # Per-(symbol, carrier) soft-bit reliability from the same fresh,
    # per-symbol pilot-interpolated gain used to equalize -- not a single
    # static per-carrier weight off the header block. A carrier passing
    # through a local Watterson null has small |gain| at that instant;
    # equalizing divides by it, which recovers the right *mean* but scales
    # up that symbol-carrier's noise/residual too, so its LLR should count
    # for less right there, not for the whole frame. See
    # `_pilot_channel_estimate`'s docstring and the dated DESIGN.md note:
    # this replaced a static header-SNR weight that could not tell a
    # carrier catching a mid-frame fade from one that never did.
    power = np.abs(pilot_gain) ** 2
    median_power = np.maximum(np.median(power, axis=1, keepdims=True), 1e-30)
    data_weight = np.clip(power / median_power, 0.05, 2.0)
    per_carrier_soft = qam16_soft_bits(
        data_equalized, weight=data_weight).reshape(
            PAYLOAD_SYMBOLS, N_DATA_CARRIERS, BITS_PER_DATA_CARRIER)

    # Frequency-diversity combining: sum each group's physical carriers'
    # already-reliability-weighted LLRs into one logical carrier's LLR
    # before decoding (see `DATA_GROUPS`'s docstring). A carrier passing
    # through a notch contributes a small, near-zero LLR there (its weight
    # is small); the sum is dominated by whichever group member is not
    # simultaneously faded, which a spatially local Watterson notch makes
    # unlikely for carriers spread across the band.
    logical_soft = np.zeros(
        (PAYLOAD_SYMBOLS, N_LOGICAL_CARRIERS, BITS_PER_DATA_CARRIER))
    for position, local in enumerate(DATA_LOCAL_INDEX):
        logical_soft[:, GROUP_OF_DATA_LOCAL[int(local)], :] += (
            per_carrier_soft[:, position, :])
    soft_bits = logical_soft.reshape(-1)

    payload, meta = CODEC.decode_soft(soft_bits)
    result.update(meta)
    result.update(payload=payload, synced=True, channel=channel.gain,
                  interference=channel.offset, soft_payload_bits=soft_bits)
    return result


@dataclass(frozen=True)
class FrameInfo:
    sample_rate: int = SAMPLE_RATE
    carrier_count: int = N_CARRIERS
    header_symbols: int = HEADER_SYMBOLS
    payload_symbols: int = PAYLOAD_SYMBOLS
    max_payload_bytes: int = MAX_PAYLOAD_BYTES

    @property
    def frame_seconds(self) -> float:
        return frame_seconds()


INFO = FrameInfo()


def describe() -> str:
    return (f"hf2: {N_DATA_CARRIERS}x16-QAM + {N_PILOTS} pilot carriers "
            f"{CARRIER_HZ[0]:.2f}-{CARRIER_HZ[-1]:.2f} Hz, {TOTAL_SYMBOLS} "
            f"symbols, {MAX_PAYLOAD_BYTES} B + CRC32 in {frame_seconds():.3f} s, "
            f"offset tolerance +-{COARSE_OFFSET_LIMIT_HZ:.1f} Hz")


def _check_constants() -> None:
    assert CORE_SAMPLES == 512 and GUARD_SAMPLES == 128
    assert SYMBOL_SAMPLES == 640
    assert CARRIER_SPACING_HZ == 93.75
    assert N_CARRIERS == 19 and N_PILOTS == 8 and N_DATA_CARRIERS == 11
    assert CARRIER_HZ[0] == 656.25 and CARRIER_HZ[-1] == 2343.75
    assert list(PILOT_BINS) == [7, 10, 12, 15, 17, 20, 22, 25]
    assert N_LOGICAL_CARRIERS == 5
    assert TOTAL_SYMBOLS == 109 and PAYLOAD_SYMBOLS == 99
    assert RAW_BITS_PER_SYMBOL == 20 and PAYLOAD_BITS == 1_980
    assert FEC_INPUT_BITS == 990
    # No stranded bits: the coded grid divides exactly into whole packet
    # bytes plus the trellis tail.  This is unchanged from the stage-4a
    # geometry -- frequency-diversity grouping (DATA_GROUPS) changes only
    # how the same 1,980 coded bits are placed on the air, not the codec.
    assert PACKET_BYTES == 123 and UNUSED_INFO_BITS == 0
    assert MAX_PAYLOAD_BYTES == 117
    assert CODEC.interleaver.is_valid()


_check_constants()
