"""HF3: sparse-pilot coherent 16-QAM OFDM, targeting Speed Ladder Level 3.

See `experiments/hf3/DESIGN.md` for the geometry/constellation/pilot/frame
decisions and the record of how they were reached. This module is the raw
waveform (geometry, mapping, FEC, framing, acquisition/timing/frequency
recovery), built the same way HC0/HC1/VF3/VF6/HF2 all are: geometry and
wiring on top of the shared `whale/dsp/` kernels (`whale.dsp.ofdm` for
symbol build/analyze, `whale.dsp.acquire` for header detection,
`whale.dsp.freq`/`whale.dsp.timing` for frequency and clock recovery,
`whale.dsp.equalize.fit_header` for the header channel fit, and
`whale.dsp.framing.PacketCodec`/`whale.dsp.fec.K7` for length/CRC/FEC).

HF3 targets Level 3 ("fast data": benign/static at +8 dB waveform SNR and
above, quiet Watterson fading at +10 dB and above -- both friendlier than
HF2's Level 2 envelope). It is designed independently of HC0/HC1/HF2/HR0:
own carrier geometry, own pilot layout (a single sparse comb, no
frequency-diversity carrier grouping -- the friendlier channel does not
need it), and its own constellation, built from a generic Gray-coded M-PAM
table (any bits-per-axis, not a hand-derived fixed formula) rather than
copying HF2's 16-QAM mapping code -- it settled on the same modulation
order as HF2 empirically, not by design fiat: see the dated note below
and in DESIGN.md for why a denser 64-QAM candidate was tried and dropped.

Frame shape:

    [SYNC_SYMBOLS identical][TRAINING_SYMBOLS varying][PAYLOAD_SYMBOLS]

exactly like HF2's, because that shape is how the shared acquisition/
timing/frequency kernels expect to be fed, not because the frame sizes or
carrier layout are copied -- see DESIGN.md.

Each payload OFDM symbol carries `N_CARRIERS` carriers split into
`N_PILOTS` fixed BPSK comb pilots and `N_DATA_CARRIERS` 16-QAM (Gray-coded,
4 bits/carrier) data carriers. The receiver tracks the channel fresh every
payload symbol from the comb pilots (each pilot's own complex gain,
smoothed across a short window of nearby symbols, then interpolated in
polar form -- magnitude and unwrapped phase separately -- across the
carrier axis), and weights each carrier's soft bits by its own
instantaneous pilot-interpolated gain, so a momentary fade discounts
itself rather than corrupting the whole frame. See DESIGN.md for the
measured record behind each of those choices -- several did not survive
contact with a real (not genie) Monte Carlo screen.
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
CORE_SAMPLES = 1_024
#: 128 samples = 2.67 ms -- more than 5x the 0.5 ms quiet-Watterson delay
#: spread SPEED_LADDERS.md's Level 3 envelope requires (and far more than
#: benign/static's <=0.1 ms). A smaller 64-sample guard was tried first;
#: it measured a real correctness problem, not just lower margin: under
#: Watterson fading, `whale.dsp.timing.estimate`'s cyclic-prefix search
#: occasionally locked onto a spurious symbol-clock drift (one measured
#: trial fit ~300 ppm of clock error against a channel with no sample-clock
#: stage at all), corrupting carrier extraction for the whole frame. The
#: smaller search margin a 64-sample guard leaves the timing fit is the
#: likely cause; doubling the guard to 128 samples measurably fixed it (a
#: controlled 40-trial comparison at the same seeds went from 2-5 mid-frame
#: failures to 0 at quiet Watterson +14 dB) -- see DESIGN.md's dated note.
GUARD_SAMPLES = 128
SYMBOL_SAMPLES = GUARD_SAMPLES + CORE_SAMPLES
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
RX_CORE_SAMPLES = CORE_SAMPLES // rx_audio.DECIMATION
RX_GUARD_SAMPLES = GUARD_SAMPLES // rx_audio.DECIMATION
RX_SYMBOL_SAMPLES = RX_GUARD_SAMPLES + RX_CORE_SAMPLES

#: 36 carriers at 46.875 Hz spacing, 421.875-2,062.5 Hz -- a fresh band and
#: spacing, chosen independently of HC1/HF2's identical-looking 93.75 Hz
#: grid, that leaves ~240 Hz of nominal headroom under the project's
#: 2,300 Hz occupied-bandwidth ceiling (see DESIGN.md for the occupied-
#: bandwidth measurement that confirms the margin holds after windowing).
CARRIER_BINS = np.arange(9, 45, dtype=np.int32)
CARRIER_SPACING_HZ = SAMPLE_RATE / CORE_SAMPLES
CARRIER_HZ = CARRIER_BINS.astype(np.float64) * CARRIER_SPACING_HZ
N_CARRIERS = len(CARRIER_BINS)

#: A single sparse comb, every 4th carrier (local indices 0, 4, 8, ..., 32
#: -> bins 9, 13, 17, ..., 41), 27 data carriers. Several pilot counts (4,
#: 6, 9, 12) were measured against real (not genie) Watterson trials, not
#: assumed from a channel-flatness argument: see DESIGN.md's dated notes.
#: 9 pilots is the balance point between tracking accuracy and the
#: bandwidth-limited throughput ceiling that a denser comb eats into.
PILOT_LOCAL_INDEX = np.arange(0, N_CARRIERS, 4, dtype=np.int64)

#: Symbols to average a pilot's own gain estimate over before interpolating
#: across the carrier axis (see `_pilot_channel_estimate`). Quiet Watterson
#: fading's 0.1 Hz Doppler spread is a ~10 s coherence time -- three orders
#: of magnitude longer than the few symbols this window spans -- so this
#: smooths AWGN-driven pilot-estimation noise essentially for free, without
#: the throughput cost of adding more pilot carriers. Measured empirically
#: (see DESIGN.md's dated note): this window measurably improved the quiet
#: Watterson +10 dB decode rate at a fixed pilot/frame configuration, though
#: on its own it did not fully close the gap -- the guard-interval fix
#: below did most of the remaining work.
PILOT_TIME_SMOOTHING_SYMBOLS = 11
DATA_LOCAL_INDEX = np.array(
    [i for i in range(N_CARRIERS) if i not in set(PILOT_LOCAL_INDEX.tolist())],
    dtype=np.int64)
N_PILOTS = len(PILOT_LOCAL_INDEX)
N_DATA_CARRIERS = len(DATA_LOCAL_INDEX)
PILOT_BINS = CARRIER_BINS[PILOT_LOCAL_INDEX]
DATA_BINS = CARRIER_BINS[DATA_LOCAL_INDEX]

BITS_PER_AXIS = 2  # 4-level Gray PAM per axis -> 16-QAM, 4 bits/carrier
BITS_PER_DATA_CARRIER = 2 * BITS_PER_AXIS

RAW_BITS_PER_SYMBOL = N_DATA_CARRIERS * BITS_PER_DATA_CARRIER

SYNC_SYMBOLS = 3
TRAINING_SYMBOLS = 3
HEADER_SYMBOLS = SYNC_SYMBOLS + TRAINING_SYMBOLS
#: 120 payload symbols (~2.9 s): large enough to clear the 2,000 bit/s
#: useful-throughput floor at this carrier/pilot/code combination's
#: bandwidth-limited ceiling, after the guard-interval fix above removed
#: the timing-fit failure mode that made shorter, more conservative frames
#: look falsely necessary. See DESIGN.md's iteration notes -- several frame
#: sizes from 50 to 150 symbols were measured, several of them before the
#: guard fix, which is part of why frame length alone did not look like a
#: reliable FER lever at the time.
PAYLOAD_SYMBOLS = 120
TOTAL_SYMBOLS = HEADER_SYMBOLS + PAYLOAD_SYMBOLS
PAYLOAD_BITS = PAYLOAD_SYMBOLS * RAW_BITS_PER_SYMBOL

TX_RMS = 0.15
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
MIN_PRESENT_CARRIERS = 32
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
    dsp.bits.pn_bits(2 * N_CARRIERS, 0x03A17))
TRAINING_VALUES = dsp.bits.qpsk_from_bits(
    dsp.bits.pn_bits(TRAINING_SYMBOLS * 2 * N_CARRIERS, 0x1C6B5)
).reshape(TRAINING_SYMBOLS, N_CARRIERS)
HEADER_VALUES = np.vstack((
    np.tile(SYNC_VALUES, (SYNC_SYMBOLS, 1)), TRAINING_VALUES))

#: Fixed-value BPSK comb pilots, constant across every payload symbol.
_pilot_bits = dsp.bits.pn_bits(N_PILOTS, 0x00A6D)
PILOT_VALUES = (1.0 - 2.0 * _pilot_bits.astype(np.float64)).astype(np.complex128)

#: 9,600 = 2^7 * 3 * 5^2; 1013 is prime and shares no factor with it.
INTERLEAVER_STRIDE = 1_013

#: K9 (constraint length 9), not K7: measured directly against the required
#: envelope's marginal quiet-Watterson +10 dB point during design (see
#: DESIGN.md's dated note), K9's extra coding gain over K7 was enough to
#: close the remaining FER gap without touching frame geometry or
#: throughput, where reducing pilot/data-carrier ratio or adding physical
#: frequency diversity both cost more throughput than they were worth at
#: this carrier count.
CODEC = dsp.PacketCodec(
    payload_bits=PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS, INTERLEAVER_STRIDE),
    whitener_seed=0x00E27,
    code=dsp.K9,
)

FEC_INPUT_BITS = CODEC.information_bits
FEC_TAIL_BITS = CODEC.code.tail_bits
PACKET_BYTES = CODEC.packet_bytes
UNUSED_INFO_BITS = CODEC.unused_information_bits
MAX_PAYLOAD_BYTES = CODEC.max_payload_bytes

encode_payload_bits = CODEC.encode
decode_payload_soft = CODEC.decode_soft


# -- generic Gray-coded M-PAM / M-QAM mapping --------------------------------

def _gray_pam_table(bits_per_axis: int) -> tuple[np.ndarray, np.ndarray]:
    """Unit-average-complex-power Gray PAM levels and their bit patterns.

    Returns `(levels, bits_at)`: `levels[g]` is the axis value at Gray
    position `g`, `bits_at[g]` are the `bits_per_axis` bits (MSB first)
    whose natural-binary integer Gray-maps to `g`. Consecutive Gray
    positions differ by exactly one bit, the standard reflected-binary
    construction; normalization matches HF2's 4-PAM convention
    (`level / sqrt(2 * mean(level**2))`) generalized to any level count.
    """
    levels_count = 1 << bits_per_axis
    raw = (np.arange(levels_count, dtype=np.float64) * 2.0
           - (levels_count - 1))
    scale = np.sqrt(2.0 * np.mean(raw ** 2))
    levels = raw / scale
    bits_at = np.zeros((levels_count, bits_per_axis), dtype=np.uint8)
    for i in range(levels_count):
        gray = i ^ (i >> 1)
        bits_at[gray] = [(i >> b) & 1
                         for b in reversed(range(bits_per_axis))]
    return levels, bits_at


_PAM_LEVELS, _PAM_BITS_AT = _gray_pam_table(BITS_PER_AXIS)
_PAM_POSITION_OF_BITS = {
    tuple(int(b) for b in _PAM_BITS_AT[g]): g for g in range(len(_PAM_LEVELS))
}


def _pam_from_bits(axis_bits: np.ndarray) -> np.ndarray:
    """`(..., bits_per_axis)` bits -> Gray-coded PAM levels."""
    axis_bits = np.asarray(axis_bits, dtype=np.uint8)
    flat = axis_bits.reshape(-1, BITS_PER_AXIS)
    positions = np.fromiter(
        (_PAM_POSITION_OF_BITS[tuple(int(b) for b in row)] for row in flat),
        dtype=np.int64, count=len(flat))
    return _PAM_LEVELS[positions].reshape(axis_bits.shape[:-1])


def qam_from_bits(bits: np.ndarray) -> np.ndarray:
    """`(6 * n,)` bits, carrier-major `[I bits..., Q bits...]` -> `(n,)`."""
    bits = np.asarray(bits, dtype=np.uint8).reshape(-1, BITS_PER_DATA_CARRIER)
    i_level = _pam_from_bits(bits[:, :BITS_PER_AXIS])
    q_level = _pam_from_bits(bits[:, BITS_PER_AXIS:])
    return i_level + 1j * q_level


def _axis_soft_bits(axis_values: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """Max-log per-bit LLRs for one Gray-PAM axis; positive means bit zero.

    Generic over `BITS_PER_AXIS`: for each bit position, the LLR is the
    squared-distance gap between the closest level with that bit set to
    one and the closest level with it set to zero, so it needs no
    hand-derived formula and stays correct if the constellation order
    changes.
    """
    axis_values = np.asarray(axis_values, dtype=np.float64)
    diff = axis_values[..., None] - _PAM_LEVELS[None, :]
    dist2 = diff * diff
    bits_at = _PAM_BITS_AT
    out = np.empty(axis_values.shape + (BITS_PER_AXIS,), dtype=np.float64)
    for k in range(BITS_PER_AXIS):
        is_one = bits_at[:, k] == 1
        min_one = np.min(np.where(is_one, dist2, np.inf), axis=-1)
        min_zero = np.min(np.where(~is_one, dist2, np.inf), axis=-1)
        out[..., k] = min_one - min_zero
    return out * weight[..., None]


def qam_soft_bits(values: np.ndarray, weight: np.ndarray | float = 1.0
                    ) -> np.ndarray:
    """Per-bit LLRs for Gray M-QAM, carrier-major `[I bits..., Q bits...]`."""
    values = np.asarray(values, dtype=np.complex128)
    weight = np.broadcast_to(np.asarray(weight, dtype=np.float64),
                             values.shape)
    i_soft = _axis_soft_bits(values.real, weight)
    q_soft = _axis_soft_bits(values.imag, weight)
    return np.concatenate((i_soft, q_soft), axis=-1).reshape(-1)


# -- modulation ---------------------------------------------------------------

def build_symbol(values: np.ndarray) -> np.ndarray:
    return _ofdm.build_symbol(GEOMETRY, values)


def symbol_carriers(symbol_audio: np.ndarray,
                    offset: int = FFT_OFFSET) -> np.ndarray:
    return _ofdm.symbol_carriers(GEOMETRY, symbol_audio, offset)


def frame_constellation(payload: bytes) -> np.ndarray:
    coded = encode_payload_bits(payload)
    data_values = qam_from_bits(coded).reshape(
        PAYLOAD_SYMBOLS, N_DATA_CARRIERS)
    payload_grid = np.empty((PAYLOAD_SYMBOLS, N_CARRIERS), dtype=np.complex128)
    payload_grid[:, PILOT_LOCAL_INDEX] = PILOT_VALUES[None, :]
    payload_grid[:, DATA_LOCAL_INDEX] = data_values
    return np.vstack((HEADER_VALUES, payload_grid))


def modulate(payload: bytes, *,
            head_seconds: float = DEFAULT_HEAD_SECONDS) -> np.ndarray:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"payload is {len(payload)} bytes; the maximum is "
            f"{MAX_PAYLOAD_BYTES}")
    values = frame_constellation(payload)
    symbols = np.concatenate([build_symbol(row) for row in values])
    lead = hf_lead.modulate(hf_lead.HF3_LABEL, head_seconds)
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


def _smooth_pilot_gain(pilot_gain: np.ndarray) -> np.ndarray:
    """Centered moving-average each pilot's gain across nearby symbols.

    `pilot_gain` is `(symbols, N_PILOTS)`. Averaging the raw complex gain
    directly (not magnitude/phase) is correct here because the window is
    short enough that the channel itself barely rotates within it (see
    `PILOT_TIME_SMOOTHING_SYMBOLS`'s docstring) -- what averages out is the
    AWGN on each symbol's independent pilot read, not real channel motion.
    """
    window = PILOT_TIME_SMOOTHING_SYMBOLS
    if window <= 1:
        return pilot_gain
    symbols = pilot_gain.shape[0]
    kernel = np.ones(window) / window
    smoothed = np.empty_like(pilot_gain)
    for p in range(pilot_gain.shape[1]):
        padded = np.pad(pilot_gain[:, p], (window // 2, window // 2),
                        mode="edge")
        smoothed[:, p] = np.convolve(padded, kernel, mode="valid")[:symbols]
    return smoothed


def _pilot_channel_estimate(payload_raw: np.ndarray,
                            offset: np.ndarray) -> np.ndarray:
    """Fresh per-symbol, carrier-interpolated complex gain from comb pilots.

    Same general technique as HF2's payload tracking (per-symbol pilot
    read, not a ratio against a stale header fit -- see that module's
    docstring for why a static header-time gain does not survive a
    moving channel), simplified for HF3's sparser 4-pilot comb: plain
    piecewise-linear interpolation across the carrier axis, per symbol,
    real/imag independently.
    """
    payload_raw = np.asarray(payload_raw, dtype=np.complex128)
    pilot_observed = payload_raw[:, PILOT_LOCAL_INDEX] - offset[None, PILOT_LOCAL_INDEX]
    pilot_gain = pilot_observed / PILOT_VALUES[None, :]
    pilot_gain = _smooth_pilot_gain(pilot_gain)

    # Polar interpolation (magnitude and unwrapped phase separately), not
    # linear real/imaginary interpolation: measured against a genie
    # reference during design (see DESIGN.md's dated note), interpolating
    # I/Q directly chord-cuts across any real phase excursion between two
    # pilots -- shrinking the reconstructed magnitude and biasing the phase
    # toward the chord's midpoint rather than the arc the channel actually
    # traces. Even the mild phase slope from a plain in-band filter
    # response was enough to show this bias; polar interpolation removed
    # it (a straight-line fit in the naturally slowly-varying magnitude
    # and phase domains, not the wrapped complex plane).
    x = PILOT_BINS.astype(np.float64)
    magnitude = np.abs(pilot_gain)
    phase = np.unwrap(np.angle(pilot_gain), axis=1)
    symbols = payload_raw.shape[0]
    correction = np.empty((symbols, N_DATA_CARRIERS), dtype=np.complex128)
    for s in range(symbols):
        mag_s = np.interp(DATA_BINS, x, magnitude[s])
        phase_s = np.interp(DATA_BINS, x, phase[s])
        correction[s] = mag_s * np.exp(1j * phase_s)
    return correction


def _base_result() -> dict:
    return {
        "synced": False, "payload": None, "confidence": 0.0,
        "start_index": None, "cfo_hz": 0.0, "clock_offset_ppm": 0.0,
        "carrier_snr_db": np.full(N_CARRIERS, -np.inf),
    }


def demodulate(audio: np.ndarray, *,
              head_seconds: float = DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
    """Decode one HF3 frame out of `audio` (at `RX_SAMPLE_RATE`).

    Returns at least `{"synced", "payload", "start_index"}` plus
    diagnostics (`confidence`, `carrier_snr_db`, `cfo_hz`, ...), matching
    the shape `whale/modes/hf3_mode.py`'s codec reads off this function.
    Never raises: any unusable/hostile input returns `payload: None`.
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

    try:
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

        # Per-(symbol, carrier) soft-bit reliability from the same fresh
        # pilot-interpolated gain used to equalize: a carrier caught in a
        # momentary fade has small |gain| there, so its LLR counts for
        # less at exactly that symbol rather than for the whole frame.
        power = np.abs(pilot_gain) ** 2
        median_power = np.maximum(np.median(power, axis=1, keepdims=True), 1e-30)
        data_weight = np.clip(power / median_power, 0.05, 2.0)
        soft_bits = qam_soft_bits(data_equalized, weight=data_weight)

        payload, meta = CODEC.decode_soft(soft_bits)
        result.update(meta)
        result.update(payload=payload, synced=True, channel=channel.gain,
                      interference=channel.offset, soft_payload_bits=soft_bits)
        return result
    except Exception as exc:
        result["failure"] = f"unusable input: {type(exc).__name__}: {exc}"
        result["synced"] = False
        result["payload"] = None
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
    return (f"hf3: {N_DATA_CARRIERS}x16-QAM + {N_PILOTS} pilot carriers "
            f"{CARRIER_HZ[0]:.2f}-{CARRIER_HZ[-1]:.2f} Hz, {TOTAL_SYMBOLS} "
            f"symbols, {MAX_PAYLOAD_BYTES} B + CRC32 in {frame_seconds():.3f} s, "
            f"offset tolerance +-{COARSE_OFFSET_LIMIT_HZ:.1f} Hz")


def _check_constants() -> None:
    assert CORE_SAMPLES == 1_024 and GUARD_SAMPLES == 128
    assert SYMBOL_SAMPLES == 1_152
    assert CARRIER_SPACING_HZ == 46.875
    assert N_CARRIERS == 36 and N_PILOTS == 9 and N_DATA_CARRIERS == 27
    assert CARRIER_HZ[0] == 421.875 and CARRIER_HZ[-1] == 2_062.5
    assert list(PILOT_BINS) == [9, 13, 17, 21, 25, 29, 33, 37, 41]
    assert TOTAL_SYMBOLS == 126 and PAYLOAD_SYMBOLS == 120
    assert RAW_BITS_PER_SYMBOL == 108 and PAYLOAD_BITS == 12_960
    assert FEC_INPUT_BITS == 6_480
    assert PACKET_BYTES == 809 and UNUSED_INFO_BITS == 0
    assert MAX_PAYLOAD_BYTES == 803
    assert CODEC.interleaver.is_valid()
    assert len(_PAM_LEVELS) == 4
    for g in range(4):
        assert _PAM_POSITION_OF_BITS[tuple(int(b) for b in _PAM_BITS_AT[g])] == g


_check_constants()
