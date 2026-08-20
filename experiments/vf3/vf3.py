"""VF3: the 58-carrier, full-1024-core counterpart to VF2.

VF3 keeps VF2's 48 kHz / 24 ms / 214-symbol frame, coherent QPSK header,
interleaved rate-1/2 convolutional coding, CRC32, acquisition validation and
per-carrier phase loops.  It spends the complete 1024-sample modulation
interval on new information instead of repeating a 512-sample core:

    [128 cyclic prefix][1024 OFDM core] = 1152 samples

That halves carrier spacing to 46.875 Hz and fits 58 carriers in essentially
the same audio band.  The deliberate cost is the loss of VF2's repeated-core
combining gain and 640-sample placement window.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass
from pathlib import Path
import sys

import numpy as np
from scipy.signal import hilbert

VF2_DIR = Path(__file__).resolve().parent.parent / "vf2"
if str(VF2_DIR) not in sys.path:
    sys.path.insert(0, str(VF2_DIR))
import vf2 as _base  # noqa: E402 -- VF2 supplies shared bit/FEC primitives


SAMPLE_RATE = 48_000
CORE_SAMPLES = 1_024
GUARD_SAMPLES = 128
SYMBOL_SAMPLES = GUARD_SAMPLES + CORE_SAMPLES

CARRIER_BINS = np.arange(10, 68, dtype=np.int32)
CARRIER_SPACING_HZ = SAMPLE_RATE / CORE_SAMPLES
CARRIER_HZ = CARRIER_BINS.astype(np.float64) * CARRIER_SPACING_HZ
N_CARRIERS = len(CARRIER_BINS)

HEADER_SYMBOLS = 15
SYNC_SYMBOLS = 5
PAYLOAD_SYMBOLS = 199
TOTAL_SYMBOLS = HEADER_SYMBOLS + PAYLOAD_SYMBOLS
BITS_PER_SYMBOL = 2 * N_CARRIERS
PAYLOAD_BITS = PAYLOAD_SYMBOLS * BITS_PER_SYMBOL

LEAD_IN_SAMPLES = 2_160
TAIL_SAMPLES = 912
FRAME_SAMPLES = LEAD_IN_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

FEC_INPUT_BITS = PAYLOAD_BITS // 2
FEC_TAIL_BITS = 6
PACKET_BITS = FEC_INPUT_BITS - FEC_TAIL_BITS
PACKET_BYTES = PACKET_BITS // 8
UNUSED_INFO_BITS = PACKET_BITS % 8
LENGTH_BYTES = 2
CRC_BYTES = 4
MAX_PAYLOAD_BYTES = PACKET_BYTES - LENGTH_BYTES - CRC_BYTES

TX_RMS = 0.13
MAX_SAMPLE = 0.95
_UNSCALED_RMS = np.sqrt(2.0 * N_CARRIERS) / CORE_SAMPLES
_TIME_SCALE = TX_RMS / _UNSCALED_RMS

FFT_OFFSET = GUARD_SAMPLES
ACQUISITION_THRESHOLD = 0.70
MIN_PRESENT_CARRIERS = 40
PHASE_LOOP_ALPHA = 0.08
PHASE_LOOP_BETA = 0.0005

qpsk_from_bits = _base.qpsk_from_bits
bits_from_qpsk = _base.bits_from_qpsk
slice_qpsk = _base.slice_qpsk
convolutional_encode = _base.convolutional_encode
convolutional_decode = _base.convolutional_decode

_DIFFERENTIAL_POINTS = np.array([1.0 + 0j, 0.0 + 1j, 0.0 - 1j, -1.0 + 0j])
_DIFFERENTIAL_LABELS = np.array(
    [[0, 0], [0, 1], [1, 0], [1, 1]], dtype=np.uint8)


def differential_encode(bits: np.ndarray, initial: np.ndarray) -> np.ndarray:
    """Encode bit pairs as QPSK phase increments, independently per carrier."""
    pairs = np.asarray(bits, dtype=np.uint8).reshape(
        PAYLOAD_SYMBOLS, N_CARRIERS, 2)
    indices = 2 * pairs[:, :, 0] + pairs[:, :, 1]
    increments = _DIFFERENTIAL_POINTS[indices]
    output = np.empty_like(increments)
    previous = np.asarray(initial, dtype=np.complex128)
    for i in range(PAYLOAD_SYMBOLS):
        output[i] = previous * increments[i]
        previous = output[i]
    return output


def differential_observations(values: np.ndarray, initial: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.complex128)
    previous = np.vstack((np.asarray(initial)[None, :], values[:-1]))
    differential = values * np.conj(previous)
    return differential / np.maximum(np.abs(differential), 1e-30)


def differential_bits(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    scores = np.stack(
        (values.real, values.imag, -values.imag, -values.real), axis=-1)
    return _DIFFERENTIAL_LABELS[np.argmax(scores, axis=-1)].reshape(-1)


def differential_soft_bits(values: np.ndarray,
                           carrier_weights: np.ndarray) -> np.ndarray:
    """Max-log bit reliabilities; positive means bit zero."""
    values = np.asarray(values)
    scores = np.stack(
        (values.real, values.imag, -values.imag, -values.real), axis=-1)
    llr0 = np.maximum(scores[..., 0], scores[..., 1]) - np.maximum(
        scores[..., 2], scores[..., 3])
    llr1 = np.maximum(scores[..., 0], scores[..., 2]) - np.maximum(
        scores[..., 1], scores[..., 3])
    return (np.stack((llr0, llr1), axis=-1)
            * np.asarray(carrier_weights)[None, :, None]).reshape(-1)


def convolutional_decode_soft(soft_bits: np.ndarray) -> np.ndarray:
    """Soft Viterbi decoder; input sign is the bit hypothesis confidence."""
    soft_bits = np.asarray(soft_bits, dtype=np.float64).reshape(-1)
    if len(soft_bits) % 2:
        raise ValueError("rate-1/2 code requires an even soft-bit count")
    steps = len(soft_bits) // 2
    metrics = np.full(64, np.inf)
    metrics[0] = 0.0
    previous = np.empty((steps, 64), dtype=np.uint8)
    inputs = np.empty((steps, 64), dtype=np.uint8)
    transitions = []
    for state in range(64):
        for bit in (0, 1):
            register = ((state << 1) | bit) & 0x7F
            next_state = register & 0x3F
            pair = ((register & 0o171).bit_count() & 1,
                    (register & 0o133).bit_count() & 1)
            signs = (1 - 2 * pair[0], 1 - 2 * pair[1])
            transitions.append((state, bit, next_state, signs))
    for t in range(steps):
        received0, received1 = soft_bits[2 * t:2 * t + 2]
        new_metrics = np.full(64, np.inf)
        for state, bit, next_state, signs in transitions:
            metric = (metrics[state] - signs[0] * received0
                      - signs[1] * received1)
            if metric < new_metrics[next_state]:
                new_metrics[next_state] = metric
                previous[t, next_state] = state
                inputs[t, next_state] = bit
        metrics = new_metrics
    decoded = np.empty(steps, dtype=np.uint8)
    state = 0
    for t in range(steps - 1, -1, -1):
        decoded[t] = inputs[t, state]
        state = int(previous[t, state])
    return decoded


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

_SYNC_BITS = _base._lfsr_bits(BITS_PER_SYMBOL, 0x0F35B)
SYNC_VALUES = qpsk_from_bits(_SYNC_BITS)
_TRAINING_BITS = _base._lfsr_bits(
    (HEADER_SYMBOLS - SYNC_SYMBOLS) * BITS_PER_SYMBOL, 0x1B4C3)
HEADER_VALUES = np.vstack((
    np.tile(SYNC_VALUES, (SYNC_SYMBOLS, 1)),
    qpsk_from_bits(_TRAINING_BITS).reshape(
        HEADER_SYMBOLS - SYNC_SYMBOLS, N_CARRIERS),
))
_WHITENER = _base._lfsr_bits(PACKET_BYTES * 8, 0x17E35)
_INTERLEAVER = (np.arange(PAYLOAD_BITS, dtype=np.int64) * 8101) % PAYLOAD_BITS


def encode_payload_bits(payload: bytes) -> np.ndarray:
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"payload is {len(payload)} bytes; VF3 maximum is {MAX_PAYLOAD_BYTES}")
    packet = bytearray(PACKET_BYTES)
    packet[0:2] = len(payload).to_bytes(2, "big")
    packet[2:2 + len(payload)] = payload
    crc_at = 2 + len(payload)
    packet[crc_at:crc_at + 4] = (
        binascii.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "big")
    information = np.zeros(FEC_INPUT_BITS, dtype=np.uint8)
    information[:PACKET_BYTES * 8] = (
        np.unpackbits(np.frombuffer(packet, dtype=np.uint8)) ^ _WHITENER)
    coded = convolutional_encode(information)
    return coded[_INTERLEAVER]


def decode_payload_bits(bits: np.ndarray) -> tuple[bytes | None, dict]:
    bits = np.asarray(bits, dtype=np.uint8).reshape(-1)
    if len(bits) != PAYLOAD_BITS:
        raise ValueError(f"expected {PAYLOAD_BITS} bits, got {len(bits)}")
    coded = np.empty(PAYLOAD_BITS, dtype=np.uint8)
    coded[_INTERLEAVER] = bits
    return _decode_information(convolutional_decode(coded))


def decode_payload_soft(soft_bits: np.ndarray) -> tuple[bytes | None, dict]:
    soft_bits = np.asarray(soft_bits, dtype=np.float64).reshape(-1)
    if len(soft_bits) != PAYLOAD_BITS:
        raise ValueError(f"expected {PAYLOAD_BITS} soft bits, got {len(soft_bits)}")
    coded = np.empty(PAYLOAD_BITS, dtype=np.float64)
    coded[_INTERLEAVER] = soft_bits
    return _decode_information(convolutional_decode_soft(coded))


def _decode_information(information: np.ndarray) -> tuple[bytes | None, dict]:
    tail_ok = not np.any(information[-FEC_TAIL_BITS:])
    packet = np.packbits(
        information[:PACKET_BYTES * 8] ^ _WHITENER).tobytes()
    length = int.from_bytes(packet[:2], "big")
    meta = {"decoded_length": length, "crc_ok": False, "fec_tail_ok": tail_ok}
    if length > MAX_PAYLOAD_BYTES:
        meta["failure"] = "invalid length"
        return None, meta
    payload = packet[2:2 + length]
    crc_at = 2 + length
    received_crc = int.from_bytes(packet[crc_at:crc_at + 4], "big")
    computed_crc = binascii.crc32(payload) & 0xFFFFFFFF
    meta.update(received_crc32=received_crc, computed_crc32=computed_crc,
                crc_ok=received_crc == computed_crc)
    if not meta["crc_ok"]:
        meta["failure"] = "CRC mismatch"
        return None, meta
    return payload, meta


def build_symbol(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.complex128).reshape(-1)
    if len(values) != N_CARRIERS:
        raise ValueError(f"expected {N_CARRIERS} carriers, got {len(values)}")
    spectrum = np.zeros(CORE_SAMPLES, dtype=np.complex128)
    spectrum[CARRIER_BINS] = values
    spectrum[-CARRIER_BINS] = np.conj(values)
    core = np.fft.ifft(spectrum).real * _TIME_SCALE
    return np.concatenate((core[-GUARD_SAMPLES:], core))


def symbol_carriers(symbol_audio: np.ndarray, offset: int = FFT_OFFSET) -> np.ndarray:
    audio = np.asarray(symbol_audio)
    if len(audio) < SYMBOL_SAMPLES:
        raise ValueError("a complete 1152-sample symbol is required")
    if not 0 <= offset <= GUARD_SAMPLES:
        raise ValueError("FFT offset must be in [0, 128]")
    spectrum = np.fft.fft(audio[offset:offset + CORE_SAMPLES])[CARRIER_BINS]
    core_index = (offset - GUARD_SAMPLES) % CORE_SAMPLES
    undo_shift = np.exp(-2j * np.pi * CARRIER_BINS * core_index / CORE_SAMPLES)
    return spectrum * undo_shift / _TIME_SCALE


def frame_constellation(payload: bytes) -> np.ndarray:
    payload_values = differential_encode(
        encode_payload_bits(payload), HEADER_VALUES[-1])
    return np.vstack((HEADER_VALUES, payload_values))


def modulate(payload: bytes) -> np.ndarray:
    values = frame_constellation(payload)
    symbols = np.concatenate([build_symbol(row) for row in values])
    sync_core = build_symbol(SYNC_VALUES)[GUARD_SAMPLES:]
    lead = np.resize(sync_core, LEAD_IN_SAMPLES).copy()
    lead[:240] *= np.linspace(0.0, 1.0, 240, endpoint=True)
    audio = np.concatenate((lead, symbols, np.zeros(TAIL_SAMPLES)))
    if len(audio) != FRAME_SAMPLES:
        raise AssertionError(f"internal frame length error: {len(audio)}")
    peak = float(np.max(np.abs(audio)))
    if peak > MAX_SAMPLE:
        audio *= MAX_SAMPLE / peak
    return audio.astype(np.float32)


def _rolling_sum(values: np.ndarray, width: int) -> np.ndarray:
    prefix = np.concatenate((np.zeros(1, dtype=values.dtype), np.cumsum(values)))
    return prefix[width:] - prefix[:-width]


def _header_candidate_snr(analytic: np.ndarray, start: int) -> float:
    observed = np.empty((HEADER_SYMBOLS, N_CARRIERS), dtype=np.complex128)
    for i in range(HEADER_SYMBOLS):
        at = start + i * SYMBOL_SAMPLES + FFT_OFFSET
        if at < 0 or at + CORE_SAMPLES > len(analytic):
            return -np.inf
        observed[i] = np.fft.fft(
            analytic[at:at + CORE_SAMPLES])[CARRIER_BINS]
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
    lag = SYMBOL_SAMPLES
    span = (SYNC_SYMBOLS - 1) * SYMBOL_SAMPLES
    if len(analytic) < span + lag:
        return None, 0.0
    left, right = analytic[:-lag], analytic[lag:]
    cross = _rolling_sum(right * np.conj(left), span)
    e_left = _rolling_sum(np.abs(left) ** 2, span).real
    e_right = _rolling_sum(np.abs(right) ** 2, span).real
    scores = np.abs(cross) / np.sqrt(np.maximum(e_left * e_right, 1e-30))
    rms = np.sqrt(0.5 * (e_left + e_right) / span)
    if not np.any(rms > 0.0):
        return None, 0.0
    mask = (scores >= 0.68) & (rms >= np.max(rms) * 0.03)
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
        rank = (_header_candidate_snr(analytic, candidate), float(scores[candidate]))
        if best is None or rank > best[0]:
            best = (rank, candidate)
    index = best[1]
    return index, float(np.clip(scores[index], 0.0, 1.0))


def _estimate_timing(analytic: np.ndarray, start: int) -> tuple[float, float, float]:
    indices = np.arange(SYNC_SYMBOLS, TOTAL_SYMBOLS, dtype=np.int32)
    shifts = np.empty(len(indices))
    scores = np.empty(len(indices))
    for out_index, symbol_index in enumerate(indices):
        predicted = start + symbol_index * SYMBOL_SAMPLES
        best_score, best_shift = -1.0, 0
        for shift in range(-32, 33):
            at = predicted + shift
            if at < 0 or at + SYMBOL_SAMPLES > len(analytic):
                continue
            prefix = analytic[at:at + GUARD_SAMPLES]
            tail = analytic[at + CORE_SAMPLES:at + SYMBOL_SAMPLES]
            denominator = np.sqrt(
                np.vdot(prefix, prefix).real * np.vdot(tail, tail).real)
            score = float(abs(np.vdot(prefix, tail)) / max(denominator, 1e-30))
            if score > best_score:
                best_score, best_shift = score, shift
        shifts[out_index] = best_shift
        scores[out_index] = max(best_score, 0.0)
    slope, intercept = np.polyfit(indices, shifts, 1)
    return float(intercept), float(slope), float(np.median(scores))


def _fft_bank(analytic: np.ndarray, start: int, intercept: float,
              slope: float) -> np.ndarray | None:
    carriers = np.empty((TOTAL_SYMBOLS, N_CARRIERS), dtype=np.complex128)
    for i in range(TOTAL_SYMBOLS):
        shift = int(round(intercept + slope * i))
        at = start + i * SYMBOL_SAMPLES + shift + FFT_OFFSET
        if at < 0 or at + CORE_SAMPLES > len(analytic):
            return None
        carriers[i] = np.fft.fft(
            analytic[at:at + CORE_SAMPLES])[CARRIER_BINS]
    return carriers


def _base_result() -> dict:
    return {
        "synced": False, "payload": None, "confidence": 0.0,
        "start_index": None, "cfo_hz": 0.0, "clock_offset_ppm": 0.0,
        "carrier_snr_db": np.full(N_CARRIERS, -np.inf),
        "symbol_evm_db": np.full(TOTAL_SYMBOLS, np.inf),
        "phase_track": np.zeros(TOTAL_SYMBOLS), "raw_payload_bits": None,
    }


def demodulate(audio: np.ndarray) -> dict:
    result = _base_result()
    samples = np.asarray(audio, dtype=np.float64).reshape(-1)
    if len(samples) < HEADER_SYMBOLS * SYMBOL_SAMPLES:
        result["failure"] = "capture shorter than header"
        return result
    analytic = hilbert(samples)
    start, confidence = _acquire(analytic)
    result.update(confidence=confidence, start_index=start)
    if start is None or confidence < ACQUISITION_THRESHOLD:
        result["failure"] = "header not found"
        return result
    intercept, slope, timing_confidence = _estimate_timing(analytic, start)
    result["timing_drift_samples"] = slope * (TOTAL_SYMBOLS - 1)
    result["timing_confidence"] = timing_confidence
    carriers = _fft_bank(analytic, start, intercept, slope)
    if carriers is None:
        result["failure"] = "frame truncated"
        return result

    channel = np.empty(N_CARRIERS, dtype=np.complex128)
    interference = np.empty(N_CARRIERS, dtype=np.complex128)
    fitted = np.empty((HEADER_SYMBOLS, N_CARRIERS), dtype=np.complex128)
    for k in range(N_CARRIERS):
        design = np.column_stack((HEADER_VALUES[:, k], np.ones(HEADER_SYMBOLS)))
        channel[k], interference[k] = np.linalg.lstsq(
            design, carriers[:HEADER_SYMBOLS, k], rcond=None)[0]
        fitted[:, k] = channel[k] * HEADER_VALUES[:, k] + interference[k]
    power = np.abs(channel) ** 2
    present = int(np.count_nonzero(power >= np.max(power) * 10 ** (-35.0 / 10.0)))
    result["present_carriers"] = present
    if present < MIN_PRESENT_CARRIERS:
        result["failure"] = f"header has only {present}/{N_CARRIERS} carriers"
        return result
    residual = carriers[:HEADER_SYMBOLS] - fitted
    noise = np.mean(np.abs(residual) ** 2, axis=0)
    result["carrier_snr_db"] = 10.0 * np.log10(
        np.maximum(power, 1e-30) / np.maximum(noise, 1e-30))
    equalised = (carriers - interference[None, :]) / channel[None, :]
    differential = differential_observations(
        equalised[HEADER_SYMBOLS:], equalised[HEADER_SYMBOLS - 1])
    differential_scores = np.stack(
        (differential.real, differential.imag,
         -differential.imag, -differential.real), axis=-1)
    differential_decisions = _DIFFERENTIAL_POINTS[
        np.argmax(differential_scores, axis=-1)]
    corrected = equalised.copy()
    corrected[HEADER_SYMBOLS:] = differential
    result["phase_track"] = np.zeros(TOTAL_SYMBOLS)

    evm = np.empty(TOTAL_SYMBOLS)
    evm[:HEADER_SYMBOLS] = np.sqrt(np.mean(
        np.abs(corrected[:HEADER_SYMBOLS] - HEADER_VALUES) ** 2, axis=1))
    evm[HEADER_SYMBOLS:] = np.sqrt(np.mean(
        np.abs(differential - differential_decisions) ** 2,
        axis=1))
    result["symbol_evm_db"] = 20.0 * np.log10(np.maximum(evm, 1e-15))
    payload_bits = differential_bits(differential)
    result["raw_payload_bits"] = payload_bits
    snr_linear = 10.0 ** (result["carrier_snr_db"] / 10.0)
    snr_linear /= max(float(np.median(snr_linear)), 1e-30)
    carrier_weights = np.clip(snr_linear, 0.5, 2.0)
    soft_bits = differential_soft_bits(differential, carrier_weights)
    payload, meta = decode_payload_soft(soft_bits)
    result.update(meta)
    result.update(payload=payload, synced=True, channel=channel,
                  interference=interference, constellation=corrected,
                  soft_payload_bits=soft_bits)
    result["clock_offset_ppm"] = float(slope / SYMBOL_SAMPLES * 1e6)
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
    return (f"vf3: {N_CARRIERS}x QPSK carriers {CARRIER_HZ[0]:.2f}-"
            f"{CARRIER_HZ[-1]:.2f} Hz, {TOTAL_SYMBOLS} symbols, "
            f"{MAX_PAYLOAD_BYTES} B + CRC32 in {FRAME_SECONDS:.3f} s")


def _check_constants() -> None:
    assert CORE_SAMPLES == 1024 and GUARD_SAMPLES == 128
    assert SYMBOL_SAMPLES == 1152
    assert CARRIER_SPACING_HZ == 46.875
    assert N_CARRIERS == 58
    assert CARRIER_HZ[0] == 468.75 and CARRIER_HZ[-1] == 3140.625
    assert TOTAL_SYMBOLS == 214 and PAYLOAD_BITS == 23_084
    assert FEC_INPUT_BITS == 11_542
    assert FRAME_SAMPLES == 249_600 and FRAME_SECONDS == 5.2
    assert PACKET_BYTES == 1_442 and UNUSED_INFO_BITS == 0
    assert MAX_PAYLOAD_BYTES == 1_436


_check_constants()
