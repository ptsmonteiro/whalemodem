"""VF2: a fixed 214-symbol, 29-carrier coherent-QPSK OFDM frame.

This module is intentionally self-contained.  It implements the waveform and
receiver described in experiments/vf2/README.md and imports no modem DSP from
the rest of the repository.

The first five header symbols carry the same fixed QPSK vector.  Repetition at
a 1152-sample lag gives acquisition that is insensitive to the audio channel;
the remaining ten vary coherently so the receiver can distinguish channel
gain from stationary narrowband interference.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass

import numpy as np
from scipy.signal import hilbert


# Waveform geometry ---------------------------------------------------------

SAMPLE_RATE = 48_000
CORE_SAMPLES = 512
GUARD_SAMPLES = 128
SYMBOL_SAMPLES = GUARD_SAMPLES + 2 * CORE_SAMPLES

CARRIER_BINS = np.arange(5, 34, dtype=np.int32)
CARRIER_SPACING_HZ = SAMPLE_RATE / CORE_SAMPLES
CARRIER_HZ = CARRIER_BINS.astype(np.float64) * CARRIER_SPACING_HZ
N_CARRIERS = len(CARRIER_BINS)

HEADER_SYMBOLS = 15
SYNC_SYMBOLS = 5
PAYLOAD_SYMBOLS = 199
TOTAL_SYMBOLS = HEADER_SYMBOLS + PAYLOAD_SYMBOLS
BITS_PER_SYMBOL = 2 * N_CARRIERS
PAYLOAD_BITS = PAYLOAD_SYMBOLS * BITS_PER_SYMBOL

LEAD_IN_SAMPLES = 2_160                 # 45 ms
TAIL_SAMPLES = 912                      # 19 ms: 5.181 s -> 5.200 s
FRAME_SAMPLES = LEAD_IN_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

# A terminated rate-1/2, K=7 convolutional code fills all 11,542 modulation
# bits exactly.  Six zero input bits return its 64-state trellis to state zero.
FEC_INPUT_BITS = PAYLOAD_BITS // 2
FEC_TAIL_BITS = 6
PACKET_BITS = FEC_INPUT_BITS - FEC_TAIL_BITS
PACKET_BYTES = PACKET_BITS // 8
UNUSED_INFO_BITS = PACKET_BITS % 8
LENGTH_BYTES = 2
CRC_BYTES = 4
MAX_PAYLOAD_BYTES = PACKET_BYTES - LENGTH_BYTES - CRC_BYTES

# Every OFDM symbol has this RMS before the lead-in ramp.  Since the absolute
# maximum of 29 unit carriers is bounded, 0.13 also guarantees |sample| < 1
# without clipping or changing the specified constant-modulus carriers.
TX_RMS = 0.13
_UNSCALED_RMS = np.sqrt(2.0 * N_CARRIERS) / CORE_SAMPLES
_TIME_SCALE = TX_RMS / _UNSCALED_RMS

FFT_OFFSET = 64
ACQUISITION_THRESHOLD = 0.72
MIN_PRESENT_CARRIERS = 20
PHASE_LOOP_ALPHA = 0.08
PHASE_LOOP_BETA = 0.0005


@dataclass(frozen=True)
class FrameInfo:
    sample_rate: int = SAMPLE_RATE
    carrier_count: int = N_CARRIERS
    header_symbols: int = HEADER_SYMBOLS
    payload_symbols: int = PAYLOAD_SYMBOLS
    frame_samples: int = FRAME_SAMPLES
    max_payload_bytes: int = MAX_PAYLOAD_BYTES

    @property
    def frame_seconds(self) -> float:
        return self.frame_samples / self.sample_rate


INFO = FrameInfo()


# Bit and constellation helpers --------------------------------------------

def _lfsr_bits(count: int, seed: int) -> np.ndarray:
    """Deterministic order-17 PN sequence, returned as uint8 bits."""
    state = seed & 0x1FFFF
    if state == 0:
        raise ValueError("LFSR seed must be non-zero")
    out = np.empty(count, dtype=np.uint8)
    for i in range(count):
        out[i] = state & 1
        feedback = ((state >> 0) ^ (state >> 3)) & 1
        state = (state >> 1) | (feedback << 16)
    return out


def qpsk_from_bits(bits: np.ndarray) -> np.ndarray:
    """Map pairs [real-sign bit, imag-sign bit] to unit-energy QPSK."""
    bits = np.asarray(bits, dtype=np.uint8)
    if bits.size % 2:
        raise ValueError("QPSK needs an even number of bits")
    pairs = bits.reshape(-1, 2)
    return ((1.0 - 2.0 * pairs[:, 0])
            + 1j * (1.0 - 2.0 * pairs[:, 1])) / np.sqrt(2.0)


def bits_from_qpsk(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    # Keep the final axis carrier-major: [Re0, Im0, Re1, Im1, ...].
    # column_stack would group all real bits before all imaginary bits when
    # `values` is a 2-D symbol/carrier grid.
    return np.stack((values.real < 0.0, values.imag < 0.0), axis=-1).astype(
        np.uint8).reshape(-1)


def slice_qpsk(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    real = np.where(values.real >= 0.0, 1.0, -1.0)
    imag = np.where(values.imag >= 0.0, 1.0, -1.0)
    return (real + 1j * imag) / np.sqrt(2.0)


_SYNC_BITS = _lfsr_bits(BITS_PER_SYMBOL, 0x12D4B)
SYNC_VALUES = qpsk_from_bits(_SYNC_BITS)
_TRAINING_BITS = _lfsr_bits(
    (HEADER_SYMBOLS - SYNC_SYMBOLS) * BITS_PER_SYMBOL, 0x0D7A1)
HEADER_VALUES = np.vstack((
    np.tile(SYNC_VALUES, (SYNC_SYMBOLS, 1)),
    qpsk_from_bits(_TRAINING_BITS).reshape(
        HEADER_SYMBOLS - SYNC_SYMBOLS, N_CARRIERS),
))
_WHITENER = _lfsr_bits(PACKET_BYTES * 8, 0x1ACE1)

# Grid position -> convolutional-code bit.  The multiplier is coprime to
# 11,542, making this a full permutation and spreading every persistent bad
# carrier across the trellis instead of damaging the same bit neighborhood.
_INTERLEAVER = (np.arange(PAYLOAD_BITS, dtype=np.int64) * 4051) % PAYLOAD_BITS

_CONV_POLYNOMIALS = (0o171, 0o133)
_CONV_STATES = 64


def _parity(value: int) -> int:
    return value.bit_count() & 1


def convolutional_encode(input_bits: np.ndarray) -> np.ndarray:
    input_bits = np.asarray(input_bits, dtype=np.uint8).reshape(-1)
    output = np.empty(2 * len(input_bits), dtype=np.uint8)
    state = 0
    for i, bit_value in enumerate(input_bits):
        bit = int(bit_value)
        register = ((state << 1) | bit) & 0x7F
        output[2 * i] = _parity(register & _CONV_POLYNOMIALS[0])
        output[2 * i + 1] = _parity(register & _CONV_POLYNOMIALS[1])
        state = register & 0x3F
    return output


def convolutional_decode(coded_bits: np.ndarray) -> np.ndarray:
    """Hard-decision Viterbi decoder for the terminated VF2 trellis."""
    coded_bits = np.asarray(coded_bits, dtype=np.uint8).reshape(-1)
    if len(coded_bits) % 2:
        raise ValueError("rate-1/2 code requires an even coded-bit count")
    steps = len(coded_bits) // 2
    infinity = np.int32(1_000_000_000)
    metrics = np.full(_CONV_STATES, infinity, dtype=np.int32)
    metrics[0] = 0
    previous = np.empty((steps, _CONV_STATES), dtype=np.uint8)
    inputs = np.empty((steps, _CONV_STATES), dtype=np.uint8)

    transitions = []
    for state in range(_CONV_STATES):
        for bit in (0, 1):
            register = ((state << 1) | bit) & 0x7F
            next_state = register & 0x3F
            pair = (_parity(register & _CONV_POLYNOMIALS[0]),
                    _parity(register & _CONV_POLYNOMIALS[1]))
            transitions.append((state, bit, next_state, pair))

    for t in range(steps):
        received0, received1 = map(int, coded_bits[2 * t:2 * t + 2])
        new_metrics = np.full(_CONV_STATES, infinity, dtype=np.int32)
        for state, bit, next_state, pair in transitions:
            metric = metrics[state] + (pair[0] != received0) + (pair[1] != received1)
            if metric < new_metrics[next_state]:
                new_metrics[next_state] = metric
                previous[t, next_state] = state
                inputs[t, next_state] = bit
        metrics = new_metrics

    decoded = np.empty(steps, dtype=np.uint8)
    state = 0  # the final six zero inputs terminate here
    for t in range(steps - 1, -1, -1):
        decoded[t] = inputs[t, state]
        state = int(previous[t, state])
    return decoded


def encode_payload_bits(payload: bytes) -> np.ndarray:
    """Build the exact 11,542-bit payload field.

    Before coding, the layout is ``uint16 length | bytes | CRC32 | zero pad``.
    CRC32 covers the user bytes only.  Whitening, termination, rate-1/2
    convolutional coding and interleaving then fill the exact modulation grid.
    """
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"payload is {len(payload)} bytes; VF2 maximum is {MAX_PAYLOAD_BYTES}")
    packet = bytearray(PACKET_BYTES)
    packet[0:2] = len(payload).to_bytes(2, "big")
    packet[2:2 + len(payload)] = payload
    crc_at = 2 + len(payload)
    packet[crc_at:crc_at + 4] = (binascii.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "big")
    information = np.zeros(FEC_INPUT_BITS, dtype=np.uint8)
    packet_bits = np.unpackbits(np.frombuffer(packet, dtype=np.uint8)) ^ _WHITENER
    information[:PACKET_BYTES * 8] = packet_bits
    coded = convolutional_encode(information)
    if len(coded) != PAYLOAD_BITS:
        raise AssertionError("convolutional code did not fill the VF2 grid")
    return coded[_INTERLEAVER]


def decode_payload_bits(bits: np.ndarray) -> tuple[bytes | None, dict]:
    bits = np.asarray(bits, dtype=np.uint8).reshape(-1)
    if len(bits) != PAYLOAD_BITS:
        raise ValueError(f"expected {PAYLOAD_BITS} bits, got {len(bits)}")
    coded = np.empty(PAYLOAD_BITS, dtype=np.uint8)
    coded[_INTERLEAVER] = bits
    information = convolutional_decode(coded)
    tail_ok = not np.any(information[-FEC_TAIL_BITS:])
    plain = information[:PACKET_BYTES * 8] ^ _WHITENER
    packet = np.packbits(plain).tobytes()
    length = int.from_bytes(packet[:2], "big")
    meta = {"decoded_length": length, "crc_ok": False, "fec_tail_ok": tail_ok}
    if length > MAX_PAYLOAD_BYTES:
        meta["failure"] = "invalid length"
        return None, meta
    payload = packet[2:2 + length]
    crc_at = 2 + length
    received_crc = int.from_bytes(packet[crc_at:crc_at + 4], "big")
    wanted_crc = binascii.crc32(payload) & 0xFFFFFFFF
    meta["received_crc32"] = received_crc
    meta["computed_crc32"] = wanted_crc
    meta["crc_ok"] = received_crc == wanted_crc
    if not meta["crc_ok"]:
        meta["failure"] = "CRC mismatch"
        return None, meta
    return payload, meta


# Modulator ----------------------------------------------------------------

def build_symbol(values: np.ndarray) -> np.ndarray:
    """Build ``[last 128 core][core][core]`` from 29 QPSK carriers."""
    values = np.asarray(values, dtype=np.complex128).reshape(-1)
    if len(values) != N_CARRIERS:
        raise ValueError(f"expected {N_CARRIERS} carriers, got {len(values)}")
    spectrum = np.zeros(CORE_SAMPLES, dtype=np.complex128)
    spectrum[CARRIER_BINS] = values
    spectrum[-CARRIER_BINS] = np.conj(values)
    core = np.fft.ifft(spectrum).real * _TIME_SCALE
    return np.concatenate((core[-GUARD_SAMPLES:], core, core))


def frame_constellation(payload: bytes) -> np.ndarray:
    payload_bits = encode_payload_bits(payload)
    payload_values = qpsk_from_bits(payload_bits).reshape(PAYLOAD_SYMBOLS, N_CARRIERS)
    return np.vstack((HEADER_VALUES, payload_values))


def modulate(payload: bytes) -> np.ndarray:
    """Return one complete VF2 frame as exactly 249,600 float32 samples."""
    values = frame_constellation(payload)
    symbols = np.concatenate([build_symbol(row) for row in values])

    # An audio-bearing lead-in opens the FM squelch while remaining outside
    # the 214-symbol frame.  It is a continuous copy of the header's core.
    header_symbol = build_symbol(SYNC_VALUES)
    core = header_symbol[GUARD_SAMPLES:GUARD_SAMPLES + CORE_SAMPLES]
    lead = np.resize(core, LEAD_IN_SAMPLES).copy()
    ramp = min(240, LEAD_IN_SAMPLES)  # 5 ms click-free rise
    lead[:ramp] *= np.linspace(0.0, 1.0, ramp, endpoint=True)

    audio = np.concatenate((lead, symbols, np.zeros(TAIL_SAMPLES)))
    if len(audio) != FRAME_SAMPLES:
        raise AssertionError(f"internal frame length error: {len(audio)}")
    if np.max(np.abs(audio)) >= 1.0:
        raise AssertionError("VF2 drive bound exceeded")
    return audio.astype(np.float32)


# Receiver -----------------------------------------------------------------

def symbol_carriers(symbol_audio: np.ndarray, offset: int = FFT_OFFSET,
                    combine: bool = True) -> np.ndarray:
    """Recover carriers from an exactly aligned symbol (test/diagnostic API)."""
    audio = np.asarray(symbol_audio)
    if len(audio) < SYMBOL_SAMPLES:
        raise ValueError("a complete 1152-sample symbol is required")
    if not 0 <= offset <= SYMBOL_SAMPLES - CORE_SAMPLES:
        raise ValueError("FFT offset must be in [0, 640]")
    if combine and offset > SYMBOL_SAMPLES - 2 * CORE_SAMPLES:
        raise ValueError("two-core combining requires FFT offset in [0, 128]")
    first = np.fft.fft(audio[offset:offset + CORE_SAMPLES])[CARRIER_BINS]
    core_index = (offset - GUARD_SAMPLES) % CORE_SAMPLES
    undo_shift = np.exp(-2j * np.pi * CARRIER_BINS * core_index / CORE_SAMPLES)
    if not combine:
        return first * undo_shift / _TIME_SCALE
    second = np.fft.fft(
        audio[offset + CORE_SAMPLES:offset + 2 * CORE_SAMPLES])[CARRIER_BINS]
    return (first + second) * undo_shift / (2.0 * _TIME_SCALE)


def _rolling_sum(values: np.ndarray, width: int) -> np.ndarray:
    prefix = np.concatenate((np.zeros(1, dtype=values.dtype), np.cumsum(values)))
    return prefix[width:] - prefix[:-width]


def _header_candidate_snr(analytic: np.ndarray, start: int) -> float:
    """Known-header fit quality used to reject periodic pre-keying audio."""
    first = np.empty((HEADER_SYMBOLS, N_CARRIERS), dtype=np.complex128)
    second = np.empty_like(first)
    for i in range(HEADER_SYMBOLS):
        at = start + i * SYMBOL_SAMPLES + FFT_OFFSET
        if at < 0 or at + 2 * CORE_SAMPLES > len(analytic):
            return -np.inf
        first[i] = np.fft.fft(analytic[at:at + CORE_SAMPLES])[CARRIER_BINS]
        second[i] = np.fft.fft(
            analytic[at + CORE_SAMPLES:at + 2 * CORE_SAMPLES])[CARRIER_BINS]
    products = second * np.conj(first)
    phase = np.angle(np.sum(
        products / np.maximum(np.abs(products), 1e-30), axis=0))
    observed = 0.5 * (first + second * np.exp(-1j * phase)[None, :])
    snr = np.empty(N_CARRIERS)
    for k in range(N_CARRIERS):
        design = np.column_stack((HEADER_VALUES[:, k], np.ones(HEADER_SYMBOLS)))
        channel, interference = np.linalg.lstsq(
            design, observed[:, k], rcond=None)[0]
        residual = observed[:, k] - (
            channel * HEADER_VALUES[:, k] + interference)
        noise = np.mean(np.abs(residual) ** 2)
        snr[k] = 10.0 * np.log10(max(abs(channel) ** 2, 1e-30)
                                  / max(noise, 1e-30))
    return float(np.median(snr))


def _acquire(analytic: np.ndarray) -> tuple[int | None, float]:
    """Find symbol zero from the 15-symbol repeated coherent header."""
    lag = SYMBOL_SAMPLES
    span = (SYNC_SYMBOLS - 1) * SYMBOL_SAMPLES
    if len(analytic) < span + lag:
        return None, 0.0
    left = analytic[:-lag]
    right = analytic[lag:]
    cross = _rolling_sum(right * np.conj(left), span)
    e_left = _rolling_sum(np.abs(left) ** 2, span).real
    e_right = _rolling_sum(np.abs(right) ** 2, span).real
    denom = np.sqrt(np.maximum(e_left * e_right, 1e-30))
    scores = np.abs(cross) / denom

    # Ignore numerically impressive correlations made entirely from silence.
    energy = 0.5 * (e_left + e_right)
    if not np.any(energy > 0.0):
        return None, 0.0
    rms = np.sqrt(energy / span)

    # Idle receiver audio can be almost perfectly periodic (a USB spur or a
    # quiet tone), and therefore score closer to 1.0 than the RF frame.  Form
    # contiguous repeat-correlation proposals above a modest energy floor,
    # then let the *varying known header* choose between them.  A real frame
    # fits H*known_qpsk + stationary_interference; an idle tone does not.
    mask = ((scores >= 0.70)
            & (rms >= np.max(rms) * 0.03))
    proposal_samples = np.flatnonzero(mask)
    if not len(proposal_samples):
        index = int(np.argmax(scores))
        return index, float(np.clip(scores[index], 0.0, 1.0))

    groups = []
    group_start = previous = int(proposal_samples[0])
    for sample in proposal_samples[1:]:
        sample = int(sample)
        if sample != previous + 1:
            groups.append((group_start, previous))
            group_start = sample
        previous = sample
    groups.append((group_start, previous))

    best = None
    for low, high in groups:
        candidate = low + int(np.argmax(scores[low:high + 1]))
        header_snr = _header_candidate_snr(analytic, candidate)
        rank = (header_snr, float(scores[candidate]))
        if best is None or rank > best[0]:
            best = (rank, candidate)
    index = best[1]
    return index, float(np.clip(scores[index], 0.0, 1.0))


def _estimate_timing(analytic: np.ndarray, start: int) -> tuple[float, float, float]:
    """Fit received symbol length from the full 640-sample repeat window.

    For a candidate boundary ``b``, samples ``b:b+640`` and
    ``b+512:b+1152`` are identical in an undistorted symbol.  A wrong boundary
    makes part of that comparison cross into an independently modulated
    neighbor.  Individual maxima are noisy on air, so only their fitted line
    is used: intercept samples at symbol zero, slope samples per symbol.
    """
    indices = np.arange(SYNC_SYMBOLS, TOTAL_SYMBOLS, dtype=np.int32)
    shifts = np.empty(len(indices), dtype=np.float64)
    scores = np.empty(len(indices), dtype=np.float64)
    for out_index, symbol_index in enumerate(indices):
        predicted = start + symbol_index * SYMBOL_SAMPLES
        best_score, best_shift = -1.0, 0
        for shift in range(-32, 33):
            at = predicted + shift
            if at < 0 or at + SYMBOL_SAMPLES > len(analytic):
                continue
            left = analytic[at:at + 640]
            right = analytic[at + CORE_SAMPLES:at + SYMBOL_SAMPLES]
            denominator = np.sqrt(
                np.vdot(left, left).real * np.vdot(right, right).real)
            score = float(abs(np.vdot(left, right)) / max(denominator, 1e-30))
            if score > best_score:
                best_score, best_shift = score, shift
        shifts[out_index] = best_shift
        scores[out_index] = max(best_score, 0.0)

    slope, intercept = np.polyfit(indices, shifts, 1)
    return float(intercept), float(slope), float(np.median(scores))


def _fft_banks(analytic: np.ndarray, start: int,
               timing_intercept: float = 0.0,
               timing_slope: float = 0.0) -> tuple[np.ndarray, np.ndarray] | None:
    first = np.empty((TOTAL_SYMBOLS, N_CARRIERS), dtype=np.complex128)
    second = np.empty_like(first)
    for i in range(TOTAL_SYMBOLS):
        timing_shift = int(round(timing_intercept + timing_slope * i))
        at = start + i * SYMBOL_SAMPLES + timing_shift + FFT_OFFSET
        if at < 0 or at + 2 * CORE_SAMPLES > len(analytic):
            return None
        first[i] = np.fft.fft(analytic[at:at + CORE_SAMPLES])[CARRIER_BINS]
        second[i] = np.fft.fft(
            analytic[at + CORE_SAMPLES:at + 2 * CORE_SAMPLES])[CARRIER_BINS]
    return first, second


def _fit_header_drift(first: np.ndarray) -> np.ndarray:
    products = first[1:SYNC_SYMBOLS] * np.conj(first[:SYNC_SYMBOLS - 1])
    unit = products / np.maximum(np.abs(products), 1e-30)
    observed = np.unwrap(np.angle(np.sum(unit, axis=0)))
    weights = np.sqrt(np.mean(np.abs(first[:SYNC_SYMBOLS]) ** 2, axis=0))
    weights = np.maximum(weights, np.max(weights) * 1e-3)
    slope, intercept = np.polyfit(CARRIER_BINS, observed, 1, w=weights)
    return intercept + slope * CARRIER_BINS


def _base_result() -> dict:
    return {
        "synced": False,
        "payload": None,
        "confidence": 0.0,
        "start_index": None,
        "cfo_hz": 0.0,
        "clock_offset_ppm": 0.0,
        "carrier_snr_db": np.full(N_CARRIERS, -np.inf),
        "symbol_evm_db": np.full(TOTAL_SYMBOLS, np.inf),
        "phase_track": np.zeros(TOTAL_SYMBOLS),
        "raw_payload_bits": None,
    }


def demodulate(audio: np.ndarray) -> dict:
    """Search audio for one VF2 frame and return payload plus diagnostics."""
    result = _base_result()
    samples = np.asarray(audio, dtype=np.float64).reshape(-1)
    if len(samples) < HEADER_SYMBOLS * SYMBOL_SAMPLES:
        result["failure"] = "capture shorter than header"
        return result
    analytic = hilbert(samples)
    start, confidence = _acquire(analytic)
    result["confidence"] = confidence
    result["start_index"] = start
    if start is None or confidence < ACQUISITION_THRESHOLD:
        result["failure"] = "header not found"
        return result

    timing_intercept, timing_slope, timing_confidence = _estimate_timing(
        analytic, start)
    result["timing_drift_samples"] = timing_slope * (TOTAL_SYMBOLS - 1)
    result["timing_confidence"] = timing_confidence
    banks = _fft_banks(analytic, start, timing_intercept, timing_slope)
    if banks is None:
        result["failure"] = "frame truncated"
        return result
    first, second = banks
    # Estimate the phase accumulated between repeated cores from every symbol,
    # not from the settling header alone, then combine for the requested 3 dB.
    products = second * np.conj(first)
    unit_products = products / np.maximum(np.abs(products), 1e-30)
    separation_phase = np.unwrap(np.angle(np.sum(unit_products, axis=0)))
    carriers = 0.5 * (first + second * np.exp(-1j * separation_phase)[None, :])

    # A varying coherent training sequence lets this two-parameter fit
    # separate multiplicative channel H from a stationary narrowband term I:
    # received = H * known_qpsk + I.  The initial repeated sync alone cannot.
    channel = np.empty(N_CARRIERS, dtype=np.complex128)
    interference = np.empty(N_CARRIERS, dtype=np.complex128)
    fitted_header = np.empty((HEADER_SYMBOLS, N_CARRIERS), dtype=np.complex128)
    for k in range(N_CARRIERS):
        design = np.column_stack((HEADER_VALUES[:, k], np.ones(HEADER_SYMBOLS)))
        channel[k], interference[k] = np.linalg.lstsq(
            design, carriers[:HEADER_SYMBOLS, k], rcond=None)[0]
        fitted_header[:, k] = channel[k] * HEADER_VALUES[:, k] + interference[k]

    power = np.abs(channel) ** 2
    present = int(np.count_nonzero(power >= np.max(power) * 10.0 ** (-35.0 / 10.0)))
    result["present_carriers"] = present
    if present < MIN_PRESENT_CARRIERS:
        result["failure"] = f"header has only {present}/{N_CARRIERS} carriers"
        return result

    residual = carriers[:HEADER_SYMBOLS] - fitted_header
    noise = np.mean(np.abs(residual) ** 2, axis=0)
    result["carrier_snr_db"] = 10.0 * np.log10(
        np.maximum(power, 1e-30) / np.maximum(noise, 1e-30))
    equalised = (carriers - interference[None, :]) / channel[None, :]
    corrected = equalised.copy()
    phase_track = np.zeros(TOTAL_SYMBOLS)
    carrier_phase = np.zeros(N_CARRIERS)
    carrier_frequency = np.zeros(N_CARRIERS)
    for i in range(HEADER_SYMBOLS, TOTAL_SYMBOLS):
        rotated = equalised[i] * np.exp(-1j * carrier_phase)
        decisions_now = slice_qpsk(rotated)
        phase_error = np.angle(rotated * np.conj(decisions_now))
        # Correct a conservative fraction in the current symbol and carry a
        # second-order estimate forward.  Each carrier gets its own loop:
        # sound-card drift is frequency-dependent and one common phase loop
        # cannot hold 469 Hz and 3094 Hz for a five-second frame.
        corrected[i] = rotated * np.exp(-1j * PHASE_LOOP_ALPHA * phase_error)
        carrier_frequency += PHASE_LOOP_BETA * phase_error
        carrier_phase += carrier_frequency + PHASE_LOOP_ALPHA * phase_error
        phase_track[i] = float(np.median(carrier_phase))
    result["phase_track"] = phase_track

    expected_header = HEADER_VALUES
    decisions = slice_qpsk(corrected)
    evm = np.empty(TOTAL_SYMBOLS)
    evm[:HEADER_SYMBOLS] = np.sqrt(np.mean(
        np.abs(corrected[:HEADER_SYMBOLS] - expected_header) ** 2, axis=1))
    evm[HEADER_SYMBOLS:] = np.sqrt(np.mean(
        np.abs(corrected[HEADER_SYMBOLS:] - decisions[HEADER_SYMBOLS:]) ** 2, axis=1))
    result["symbol_evm_db"] = 20.0 * np.log10(np.maximum(evm, 1e-15))

    payload_bits = bits_from_qpsk(decisions[HEADER_SYMBOLS:])
    result["raw_payload_bits"] = payload_bits
    payload, meta = decode_payload_bits(payload_bits)
    result.update(meta)
    result["payload"] = payload
    result["synced"] = True
    result["channel"] = channel
    result["interference"] = interference
    result["constellation"] = corrected

    # The two repeated cores directly measure frequency scale and offset over
    # their 512-sample separation without extrapolating header settling.
    slope, intercept = np.polyfit(CARRIER_BINS, separation_phase, 1)
    result["cfo_hz"] = float(intercept * SAMPLE_RATE
                             / (2.0 * np.pi * CORE_SAMPLES))
    result["clock_offset_ppm"] = float(timing_slope / SYMBOL_SAMPLES * 1e6)
    return result


def demodulate_debug(audio: np.ndarray, reference_payload: bytes | None = None) -> dict:
    result = demodulate(audio)
    if reference_payload is None or result.get("raw_payload_bits") is None:
        return result
    expected = encode_payload_bits(reference_payload)
    errors = result["raw_payload_bits"] != expected
    grid = errors.reshape(PAYLOAD_SYMBOLS, N_CARRIERS, 2)
    result["total_bit_errors"] = int(np.count_nonzero(errors))
    result["carrier_bit_errors"] = np.sum(grid, axis=(0, 2)).astype(int)
    symbol_errors = np.zeros(TOTAL_SYMBOLS, dtype=int)
    symbol_errors[HEADER_SYMBOLS:] = np.sum(grid, axis=(1, 2))
    result["symbol_bit_errors"] = symbol_errors
    result["ber"] = float(np.mean(errors))
    return result


def describe() -> str:
    return (f"vf2: {N_CARRIERS}x QPSK carriers {CARRIER_HZ[0]:.2f}-"
            f"{CARRIER_HZ[-1]:.2f} Hz, {TOTAL_SYMBOLS} symbols, "
            f"{MAX_PAYLOAD_BYTES} B + CRC32 in {FRAME_SECONDS:.3f} s")


def _check_constants() -> None:
    assert CORE_SAMPLES == 512
    assert SYMBOL_SAMPLES == 1152
    assert CARRIER_SPACING_HZ == 93.75
    assert N_CARRIERS == 29
    assert CARRIER_HZ[0] == 468.75 and CARRIER_HZ[-1] == 3093.75
    assert TOTAL_SYMBOLS == 214
    assert PAYLOAD_BITS == 11_542
    assert FRAME_SAMPLES == 249_600 and FRAME_SECONDS == 5.2
    assert FEC_INPUT_BITS == 5_771
    assert MAX_PAYLOAD_BYTES == 714


_check_constants()
