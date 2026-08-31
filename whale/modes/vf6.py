"""VF6: top-rung 58-carrier OFDM with square 256-QAM and RS protection.

VF6 keeps VF5's 48 kHz / 24 ms / 214-symbol frame, shortened Reed-Solomon
outer code, CRC32 and acquisition strategy. Each carrier holds eight bits in
normalized Gray square 256-QAM. Ten known
full-band pilot symbols are interspersed through the payload so the receiver
can interpolate the channel phase independently on every carrier. The OFDM
symbol remains:

    [128 cyclic prefix][1024 OFDM core] = 1152 samples

The 189 remaining data symbols provide an 87,696-bit payload grid.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass

import numpy as np
from scipy.signal import hilbert

from ..dsp.bits import pn_bits


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
BITS_PER_SYMBOL = 8 * N_CARRIERS
PILOT_POSITIONS = np.arange(18, PAYLOAD_SYMBOLS, 20, dtype=np.int32)
PILOT_SYMBOLS = len(PILOT_POSITIONS)
DATA_SYMBOL_INDICES = np.setdiff1d(
    np.arange(PAYLOAD_SYMBOLS, dtype=np.int32), PILOT_POSITIONS)
DATA_SYMBOLS = len(DATA_SYMBOL_INDICES)
PAYLOAD_BITS = DATA_SYMBOLS * BITS_PER_SYMBOL

LEAD_IN_SAMPLES = 2_160
TAIL_SAMPLES = 912
FRAME_SAMPLES = LEAD_IN_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

PACKET_BYTES = PAYLOAD_BITS // 8
UNUSED_GRID_BYTES = 40
RS_BLOCKS = 43
RS_CODEWORD_BYTES = 254
RS_PARITY_BYTES = 16
RS_DATA_BYTES = RS_CODEWORD_BYTES - RS_PARITY_BYTES
RS_ENCODED_BYTES = RS_BLOCKS * RS_CODEWORD_BYTES
RS_PACKET_BYTES = RS_BLOCKS * RS_DATA_BYTES
LENGTH_BYTES = 2
CRC_BYTES = 4
MAX_PAYLOAD_BYTES = RS_PACKET_BYTES - LENGTH_BYTES - CRC_BYTES

TX_RMS = 0.13
MAX_SAMPLE = 0.95
_UNSCALED_RMS = np.sqrt(2.0 * N_CARRIERS) / CORE_SAMPLES
_TIME_SCALE = TX_RMS / _UNSCALED_RMS

FFT_OFFSET = GUARD_SAMPLES
ACQUISITION_THRESHOLD = 0.70
MIN_PRESENT_CARRIERS = 40
PHASE_LOOP_ALPHA = 0.08
PHASE_LOOP_BETA = 0.0005

QAM256_SCALE = np.sqrt(170.0)
_GRAY_TO_LEVEL = np.empty(16, dtype=np.float64)
for _binary_index in range(16):
    _GRAY_TO_LEVEL[_binary_index ^ (_binary_index >> 1)] = (
        2 * _binary_index - 15) / QAM256_SCALE
_LEVEL_TO_GRAY = np.array([i ^ (i >> 1) for i in range(16)], dtype=np.uint8)


def qam256_from_bits(bits: np.ndarray) -> np.ndarray:
    """Map octets to normalized Gray-labelled square 256-QAM points."""
    octets = np.packbits(np.asarray(bits, dtype=np.uint8).reshape(-1, 8), axis=1)[:, 0]
    return _GRAY_TO_LEVEL[octets >> 4] + 1j * _GRAY_TO_LEVEL[octets & 15]


def _axis_gray(values: np.ndarray) -> np.ndarray:
    indices = np.clip(np.rint((np.asarray(values) * QAM256_SCALE + 15.0) / 2.0), 0, 15).astype(np.uint8)
    return _LEVEL_TO_GRAY[indices]


def bits_from_qam256(values: np.ndarray) -> np.ndarray:
    """Separable O(N) hard slicer; avoids a 256-point distance matrix."""
    values = np.asarray(values, dtype=np.complex128).reshape(-1)
    labels = (_axis_gray(values.real) << 4) | _axis_gray(values.imag)
    return np.unpackbits(labels[:, None], axis=1).reshape(-1)


def _qam256_decisions(values: np.ndarray) -> np.ndarray:
    bits = bits_from_qam256(values)
    return qam256_from_bits(bits).reshape(np.asarray(values).shape)


# Shortened RS(254, 238) over GF(256), primitive polynomial 0x11d. Forty-three
# codewords are byte-interleaved across the grid so a localized fade is spread.
_GF_EXP = np.zeros(510, dtype=np.uint8)
_GF_LOG = np.zeros(256, dtype=np.int16)
_gf_value = 1
for _gf_index in range(255):
    _GF_EXP[_gf_index] = _gf_value
    _GF_LOG[_gf_value] = _gf_index
    _gf_value <<= 1
    if _gf_value & 0x100:
        _gf_value ^= 0x11D
_GF_EXP[255:] = _GF_EXP[:255]


def _gf_mul(left: int, right: int) -> int:
    if left == 0 or right == 0:
        return 0
    return int(_GF_EXP[int(_GF_LOG[left]) + int(_GF_LOG[right])])


def _gf_div(left: int, right: int) -> int:
    if right == 0:
        raise ZeroDivisionError("GF(256) division by zero")
    if left == 0:
        return 0
    return int(_GF_EXP[(int(_GF_LOG[left]) - int(_GF_LOG[right])) % 255])


def _gf_pow2(power: int) -> int:
    return int(_GF_EXP[power % 255])


def _poly_mul(left: list[int], right: list[int]) -> list[int]:
    product = [0] * (len(left) + len(right) - 1)
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            product[i + j] ^= _gf_mul(a, b)
    return product


def _poly_eval_high(coefficients: bytes | bytearray | list[int], x: int) -> int:
    result = 0
    for coefficient in coefficients:
        result = _gf_mul(result, x) ^ int(coefficient)
    return result


def _rs_generator(parity_bytes: int) -> list[int]:
    generator = [1]
    for i in range(parity_bytes):
        generator = _poly_mul(generator, [1, _gf_pow2(i)])
    return generator


_RS_GENERATOR = _rs_generator(RS_PARITY_BYTES)


def _rs_encode_block(data: bytes) -> bytes:
    if len(data) != RS_DATA_BYTES:
        raise ValueError(f"RS data block must be {RS_DATA_BYTES} bytes")
    work = bytearray(data) + bytearray(RS_PARITY_BYTES)
    for i in range(RS_DATA_BYTES):
        coefficient = work[i]
        if coefficient:
            for j in range(1, len(_RS_GENERATOR)):
                work[i + j] ^= _gf_mul(_RS_GENERATOR[j], coefficient)
    return bytes(data) + bytes(work[-RS_PARITY_BYTES:])


def _error_locator(syndromes: list[int]) -> list[int]:
    """Berlekamp-Massey locator coefficients in ascending degree order."""
    count = len(syndromes)
    locator = [1] + [0] * count
    previous = [1] + [0] * count
    degree, age, discrepancy_scale = 0, 1, 1
    for n in range(count):
        discrepancy = syndromes[n]
        for i in range(1, degree + 1):
            discrepancy ^= _gf_mul(locator[i], syndromes[n - i])
        if discrepancy == 0:
            age += 1
            continue
        saved = locator.copy()
        scale = _gf_div(discrepancy, discrepancy_scale)
        for i in range(count + 1 - age):
            locator[i + age] ^= _gf_mul(scale, previous[i])
        if 2 * degree <= n:
            degree = n + 1 - degree
            previous = saved
            discrepancy_scale = discrepancy
            age = 1
        else:
            age += 1
    if degree * 2 > count:
        raise ValueError("too many RS errors")
    return locator[:degree + 1]


def _solve_gf(matrix: list[list[int]], vector: list[int]) -> list[int]:
    size = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = next((row for row in range(column, size)
                      if augmented[row][column]), None)
        if pivot is None:
            raise ValueError("singular RS error system")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for j in range(column, size + 1):
            augmented[column][j] = _gf_div(augmented[column][j], divisor)
        for row in range(size):
            if row == column or augmented[row][column] == 0:
                continue
            scale = augmented[row][column]
            for j in range(column, size + 1):
                augmented[row][j] ^= _gf_mul(scale, augmented[column][j])
    return [augmented[row][-1] for row in range(size)]


def _rs_decode_block(codeword: bytes) -> tuple[bytes, int]:
    if len(codeword) != RS_CODEWORD_BYTES:
        raise ValueError(f"RS codeword must be {RS_CODEWORD_BYTES} bytes")
    syndromes = [
        _poly_eval_high(codeword, _gf_pow2(i))
        for i in range(RS_PARITY_BYTES)
    ]
    if not any(syndromes):
        return codeword[:RS_DATA_BYTES], 0
    locator = _error_locator(syndromes)
    error_count = len(locator) - 1
    positions = []
    for position in range(RS_CODEWORD_BYTES):
        coefficient_power = RS_CODEWORD_BYTES - 1 - position
        x = _gf_pow2(-coefficient_power)
        value = 0
        for coefficient in reversed(locator):
            value = _gf_mul(value, x) ^ coefficient
        if value == 0:
            positions.append(position)
    if len(positions) != error_count:
        raise ValueError("could not locate all RS errors")
    matrix = []
    for syndrome_index in range(error_count):
        matrix.append([
            _gf_pow2(syndrome_index * (RS_CODEWORD_BYTES - 1 - position))
            for position in positions
        ])
    magnitudes = _solve_gf(matrix, syndromes[:error_count])
    corrected = bytearray(codeword)
    for position, magnitude in zip(positions, magnitudes):
        corrected[position] ^= magnitude
    check = [
        _poly_eval_high(corrected, _gf_pow2(i))
        for i in range(RS_PARITY_BYTES)
    ]
    if any(check):
        raise ValueError("RS correction did not clear syndromes")
    return bytes(corrected[:RS_DATA_BYTES]), error_count


def pilot_phase_correct(payload_values: np.ndarray,
                        initial_values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Interpolate per-carrier phase from the header and ten payload pilots."""
    payload_values = np.asarray(payload_values, dtype=np.complex128)
    initial_values = np.asarray(initial_values, dtype=np.complex128)
    anchor_positions = np.concatenate((np.array([-1]), PILOT_POSITIONS))
    ratios = np.vstack((
        initial_values[None, :] / HEADER_VALUES[-1][None, :],
        payload_values[PILOT_POSITIONS] / PILOT_VALUES,
    ))
    anchor_phases = np.unwrap(np.angle(ratios), axis=0)
    positions = np.arange(PAYLOAD_SYMBOLS)
    phase = np.empty((PAYLOAD_SYMBOLS, N_CARRIERS), dtype=np.float64)
    for carrier in range(N_CARRIERS):
        phase[:, carrier] = np.interp(
            positions, anchor_positions, anchor_phases[:, carrier])
    return payload_values * np.exp(-1j * phase), phase


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

_SYNC_BITS = pn_bits(BITS_PER_SYMBOL, 0x0F35B)
SYNC_VALUES = qam256_from_bits(_SYNC_BITS)
_TRAINING_BITS = pn_bits(
    (HEADER_SYMBOLS - SYNC_SYMBOLS) * BITS_PER_SYMBOL, 0x1B4C3)
HEADER_VALUES = np.vstack((
    np.tile(SYNC_VALUES, (SYNC_SYMBOLS, 1)),
    qam256_from_bits(_TRAINING_BITS).reshape(
        HEADER_SYMBOLS - SYNC_SYMBOLS, N_CARRIERS),
))
_PILOT_SIGN_BITS = pn_bits(
    PILOT_SYMBOLS * N_CARRIERS * 2, 0x13A6D).reshape(
        PILOT_SYMBOLS, N_CARRIERS, 2)
PILOT_VALUES = (15.0 / QAM256_SCALE) * (
    (1.0 - 2.0 * _PILOT_SIGN_BITS[..., 0].astype(np.float64))
    + 1j * (1.0 - 2.0 * _PILOT_SIGN_BITS[..., 1].astype(np.float64))
)
_WHITENER = pn_bits(PAYLOAD_BITS, 0x17E35)


def encode_payload_bits(payload: bytes) -> np.ndarray:
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"payload is {len(payload)} bytes; VF6 maximum is {MAX_PAYLOAD_BYTES}")
    rs_packet = bytearray(RS_PACKET_BYTES)
    rs_packet[0:2] = len(payload).to_bytes(2, "big")
    rs_packet[2:2 + len(payload)] = payload
    crc_at = 2 + len(payload)
    rs_packet[crc_at:crc_at + 4] = (
        binascii.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "big")
    codewords = b"".join(
        _rs_encode_block(rs_packet[i:i + RS_DATA_BYTES])
        for i in range(0, RS_PACKET_BYTES, RS_DATA_BYTES)
    )
    encoded = np.frombuffer(codewords, dtype=np.uint8).reshape(
        RS_BLOCKS, RS_CODEWORD_BYTES).T.reshape(-1).tobytes()
    packet = bytearray(PACKET_BYTES)
    packet[:RS_ENCODED_BYTES] = encoded
    return np.unpackbits(np.frombuffer(packet, dtype=np.uint8)) ^ _WHITENER


def decode_payload_bits(bits: np.ndarray) -> tuple[bytes | None, dict]:
    bits = np.asarray(bits, dtype=np.uint8).reshape(-1)
    if len(bits) != PAYLOAD_BITS:
        raise ValueError(f"expected {PAYLOAD_BITS} bits, got {len(bits)}")
    packet = np.packbits(bits ^ _WHITENER).tobytes()
    return _decode_packet(packet)


def _decode_packet(packet: bytes) -> tuple[bytes | None, dict]:
    corrected_blocks = []
    block_corrections = []
    corrected_bytes = 0
    try:
        deinterleaved = np.frombuffer(
            packet[:RS_ENCODED_BYTES], dtype=np.uint8).reshape(
                RS_CODEWORD_BYTES, RS_BLOCKS).T
        for codeword in deinterleaved:
            block, corrections = _rs_decode_block(
                codeword.tobytes())
            corrected_blocks.append(block)
            block_corrections.append(corrections)
            corrected_bytes += corrections
    except ValueError as error:
        return None, {
            "decoded_length": None, "crc_ok": False,
            "rs_ok": False,
            "rs_corrected_bytes": corrected_bytes,
            "rs_block_corrections": block_corrections,
            "rs_max_block_corrections": max(block_corrections, default=0),
            "failure": f"RS decode failed: {error}",
        }
    rs_packet = b"".join(corrected_blocks)
    length = int.from_bytes(rs_packet[:2], "big")
    meta = {
        "decoded_length": length, "crc_ok": False,
        "rs_ok": True, "rs_corrected_bytes": corrected_bytes,
        "rs_block_corrections": block_corrections,
        "rs_max_block_corrections": max(block_corrections, default=0),
    }
    if length > MAX_PAYLOAD_BYTES:
        meta["failure"] = "invalid length"
        return None, meta
    payload = rs_packet[2:2 + length]
    crc_at = 2 + length
    received_crc = int.from_bytes(rs_packet[crc_at:crc_at + 4], "big")
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
    payload_values = np.empty(
        (PAYLOAD_SYMBOLS, N_CARRIERS), dtype=np.complex128)
    payload_values[PILOT_POSITIONS] = PILOT_VALUES
    payload_values[DATA_SYMBOL_INDICES] = qam256_from_bits(
        encode_payload_bits(payload)).reshape(DATA_SYMBOLS, N_CARRIERS)
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
    estimated_intercept, estimated_slope, timing_confidence = _estimate_timing(
        analytic, start)
    # The cyclic prefix gives a broad correlation plateau: fitting the integer
    # maximum of every symbol moves the FFT window around inside that plateau.
    # That harmless-looking jitter is enough to damage the outer points of
    # 256-QAM.  Acquisition already anchors the FFT inside the 128-sample CP,
    # and the excellent-channel clock errors VF6 targets move by less than one
    # sample over this frame, so retain that fixed window.  Keep the CP fit as
    # a diagnostic rather than feeding its quantisation noise into the FFT.
    intercept = slope = 0.0
    result["estimated_timing_intercept_samples"] = estimated_intercept
    result["estimated_timing_drift_samples"] = (
        estimated_slope * (TOTAL_SYMBOLS - 1))
    result["timing_drift_samples"] = 0.0
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
    payload_values = equalised[HEADER_SYMBOLS:]
    payload_corrected, carrier_phase_track = pilot_phase_correct(
        payload_values, equalised[HEADER_SYMBOLS - 1])
    data_values = payload_corrected[DATA_SYMBOL_INDICES]
    hard_bits = bits_from_qam256(data_values)
    payload_decisions = np.empty_like(payload_corrected)
    payload_decisions[PILOT_POSITIONS] = PILOT_VALUES
    payload_decisions[DATA_SYMBOL_INDICES] = _qam256_decisions(data_values)
    corrected = equalised.copy()
    corrected[HEADER_SYMBOLS:] = payload_corrected
    result["phase_track"] = np.concatenate((
        np.zeros(HEADER_SYMBOLS), np.median(carrier_phase_track, axis=1)
    ))

    evm = np.empty(TOTAL_SYMBOLS)
    evm[:HEADER_SYMBOLS] = np.sqrt(np.mean(
        np.abs(corrected[:HEADER_SYMBOLS] - HEADER_VALUES) ** 2, axis=1))
    evm[HEADER_SYMBOLS:] = np.sqrt(np.mean(
        np.abs(payload_corrected - payload_decisions) ** 2,
        axis=1))
    result["symbol_evm_db"] = 20.0 * np.log10(np.maximum(evm, 1e-15))
    payload_bits = hard_bits.reshape(-1)
    result["raw_payload_bits"] = payload_bits
    payload, meta = decode_payload_bits(hard_bits)
    result.update(meta)
    result.update(payload=payload, synced=True, channel=channel,
                  interference=interference, constellation=corrected,
                  carrier_phase_track=carrier_phase_track)
    result["clock_offset_ppm"] = float(
        estimated_slope / SYMBOL_SAMPLES * 1e6)
    return result


def demodulate_debug(audio: np.ndarray, reference_payload: bytes | None = None) -> dict:
    result = demodulate(audio)
    if reference_payload is None or result.get("raw_payload_bits") is None:
        return result
    expected = encode_payload_bits(reference_payload)
    errors = result["raw_payload_bits"] != expected
    grid = errors.reshape(DATA_SYMBOLS, N_CARRIERS, 8)
    result["total_bit_errors"] = int(np.count_nonzero(errors))
    result["carrier_bit_errors"] = np.sum(grid, axis=(0, 2)).astype(int)
    symbol_errors = np.zeros(TOTAL_SYMBOLS, dtype=int)
    symbol_errors[HEADER_SYMBOLS + DATA_SYMBOL_INDICES] = np.sum(
        grid, axis=(1, 2))
    result["symbol_bit_errors"] = symbol_errors
    result["ber"] = float(np.mean(errors))
    return result


def describe() -> str:
    return (f"vf6: {N_CARRIERS}x square 256-QAM carriers {CARRIER_HZ[0]:.2f}-"
            f"{CARRIER_HZ[-1]:.2f} Hz, {TOTAL_SYMBOLS} symbols, "
            f"{MAX_PAYLOAD_BYTES} B + CRC32 in {FRAME_SECONDS:.3f} s")


def _check_constants() -> None:
    assert CORE_SAMPLES == 1024 and GUARD_SAMPLES == 128
    assert SYMBOL_SAMPLES == 1152
    assert CARRIER_SPACING_HZ == 46.875
    assert N_CARRIERS == 58
    assert CARRIER_HZ[0] == 468.75 and CARRIER_HZ[-1] == 3140.625
    assert TOTAL_SYMBOLS == 214 and PAYLOAD_BITS == 87_696
    assert PILOT_SYMBOLS == 10 and DATA_SYMBOLS == 189
    assert FRAME_SAMPLES == 249_600 and FRAME_SECONDS == 5.2
    assert PACKET_BYTES == 10_962 and UNUSED_GRID_BYTES == 40
    assert RS_ENCODED_BYTES == 10_922 and RS_PACKET_BYTES == 10_234
    assert MAX_PAYLOAD_BYTES == 10_228


_check_constants()
