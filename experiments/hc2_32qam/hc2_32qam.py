"""Oracle-aligned coherent 32QAM proof for a top HF ladder rung.

This experiment answers one deliberately narrow question: can a 49-carrier,
41.667-symbol/s waveform carry more than 7,050 user bits/s through an ideal
audio loopback?  It does not implement acquisition, channel estimation,
frequency/clock tracking, or a negotiable ``WaveformMode``.  Those omissions
are intentional: the receiver is handed the exact frame boundary and assumes
an identity channel.  Consequently this is rate and codec evidence, not a
qualified HF mode.

The payload uses rectangular coherent 32QAM (8 Gray-coded I levels by 4
Gray-coded Q levels) and a punctured K=7 convolutional code.  The repeating
mother-code keep mask ``111001`` retains four of every six rate-1/2 output
bits, producing exact rate 3/4.  Erased observations are restored with zero
reliability before the existing soft Viterbi decoder.
"""

from __future__ import annotations

import binascii

import numpy as np
from scipy import signal

from whale.dsp import bits, fec, ofdm

SAMPLE_RATE = 48_000
CORE_SAMPLES = 1_024
GUARD_SAMPLES = 128
SYMBOL_SAMPLES = CORE_SAMPLES + GUARD_SAMPLES
SYMBOL_RATE = SAMPLE_RATE / SYMBOL_SAMPLES

# 49 bins span 2,250 Hz between carrier centres (bins 11..59 inclusive).
CARRIER_BINS = np.arange(11, 60, dtype=np.int32)
N_CARRIERS = len(CARRIER_BINS)
CARRIER_SPACING_HZ = SAMPLE_RATE / CORE_SAMPLES
CARRIER_HZ = CARRIER_BINS * CARRIER_SPACING_HZ
BITS_PER_CARRIER = 5
CODED_BITS_PER_SYMBOL = N_CARRIERS * BITS_PER_CARRIER

# Two known coherent symbols are included in frame-rate accounting even
# though this oracle milestone does not need to estimate their channel.
TRAINING_SYMBOLS = 2
PAYLOAD_SYMBOLS = 120
TOTAL_SYMBOLS = TRAINING_SYMBOLS + PAYLOAD_SYMBOLS
CODED_BITS = PAYLOAD_SYMBOLS * CODED_BITS_PER_SYMBOL

PUNCTURE_MASK = np.array([1, 1, 1, 0, 0, 1], dtype=bool)
MOTHER_CODE_BITS = CODED_BITS * 6 // 4
INFORMATION_BITS = MOTHER_CODE_BITS // 2
FEC_TAIL_BITS = fec.K7.tail_bits
PACKET_BYTES = (INFORMATION_BITS - FEC_TAIL_BITS) // 8
UNUSED_INFORMATION_BITS = (INFORMATION_BITS - FEC_TAIL_BITS) % 8
LENGTH_BYTES = 2
CRC_BYTES = 4
MAX_PAYLOAD_BYTES = PACKET_BYTES - LENGTH_BYTES - CRC_BYTES

FRAME_SAMPLES = TOTAL_SYMBOLS * SYMBOL_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
RAW_BIT_RATE = CODED_BITS_PER_SYMBOL * SYMBOL_RATE
CODED_INFORMATION_RATE = RAW_BIT_RATE * 3 / 4
SUSTAINED_USER_BIT_RATE = MAX_PAYLOAD_BYTES * 8 / FRAME_SECONDS

GEOMETRY = ofdm.Geometry(
    sample_rate=SAMPLE_RATE, core_samples=CORE_SAMPLES,
    guard_samples=GUARD_SAMPLES, carrier_bins=CARRIER_BINS,
).scaled_to_rms(0.13)

_WHITENER = bits.pn_bits(PACKET_BYTES * 8, 0x12A6D)
_TRAINING = np.tile(
    bits.qpsk_from_bits(bits.pn_bits(2 * N_CARRIERS, 0x0C531)),
    (TRAINING_SYMBOLS, 1),
)


def _gray_to_binary(gray: np.ndarray) -> np.ndarray:
    binary = gray.copy()
    shift = binary >> 1
    while np.any(shift):
        binary ^= shift
        shift >>= 1
    return binary


def qam32_from_bits(data: np.ndarray) -> np.ndarray:
    """Map five-bit groups to unit-average-energy rectangular 32QAM."""
    groups = np.asarray(data, dtype=np.uint8).reshape(-1, 5)
    i_gray = (groups[:, 0] << 2) | (groups[:, 1] << 1) | groups[:, 2]
    q_gray = (groups[:, 3] << 1) | groups[:, 4]
    i_level = 2 * _gray_to_binary(i_gray).astype(np.int16) - 7
    q_level = 2 * _gray_to_binary(q_gray).astype(np.int16) - 3
    return (i_level.astype(float) + 1j * q_level.astype(float)) / np.sqrt(26)


def bits_from_qam32(values: np.ndarray) -> np.ndarray:
    """Hard-slice rectangular 32QAM back to its Gray labels."""
    values = np.asarray(values).reshape(-1) * np.sqrt(26)
    i_index = np.clip(np.rint((values.real + 7) / 2), 0, 7).astype(np.uint8)
    q_index = np.clip(np.rint((values.imag + 3) / 2), 0, 3).astype(np.uint8)
    i_gray = i_index ^ (i_index >> 1)
    q_gray = q_index ^ (q_index >> 1)
    return np.column_stack((i_gray >> 2, i_gray >> 1, i_gray,
                            q_gray >> 1, q_gray)).astype(np.uint8).reshape(-1) & 1


def _encode_packet(payload: bytes) -> np.ndarray:
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload is {len(payload)} bytes; maximum is {MAX_PAYLOAD_BYTES}")
    packet = bytearray(PACKET_BYTES)
    packet[:2] = len(payload).to_bytes(2, "big")
    packet[2:2 + len(payload)] = payload
    crc_at = 2 + len(payload)
    packet[crc_at:crc_at + 4] = (binascii.crc32(payload) & 0xffffffff).to_bytes(4, "big")
    information = np.zeros(INFORMATION_BITS, dtype=np.uint8)
    information[:PACKET_BYTES * 8] = np.unpackbits(np.frombuffer(packet, dtype=np.uint8)) ^ _WHITENER
    mother = fec.K7.encode(information)
    return mother[np.resize(PUNCTURE_MASK, len(mother))]


def _decode_packet(coded: np.ndarray) -> bytes | None:
    coded = np.asarray(coded, dtype=np.uint8).reshape(-1)
    if len(coded) != CODED_BITS:
        raise ValueError(f"expected {CODED_BITS} coded bits")
    keep = np.resize(PUNCTURE_MASK, MOTHER_CODE_BITS)
    soft = np.zeros(MOTHER_CODE_BITS, dtype=float)
    soft[keep] = np.where(coded == 0, 1.0, -1.0)
    information = fec.K7.decode_soft(soft)
    packet = np.packbits(information[:PACKET_BYTES * 8] ^ _WHITENER).tobytes()
    length = int.from_bytes(packet[:2], "big")
    if length > MAX_PAYLOAD_BYTES:
        return None
    payload = packet[2:2 + length]
    crc_at = 2 + length
    received = int.from_bytes(packet[crc_at:crc_at + 4], "big")
    return payload if received == (binascii.crc32(payload) & 0xffffffff) else None


def modulate(payload: bytes) -> np.ndarray:
    coded = _encode_packet(payload)
    constellation = qam32_from_bits(coded).reshape(PAYLOAD_SYMBOLS, N_CARRIERS)
    grid = np.vstack((_TRAINING, constellation))
    return np.concatenate([ofdm.build_symbol(GEOMETRY, row) for row in grid]).astype(np.float32)


def demodulate_oracle(audio: np.ndarray) -> bytes | None:
    """Decode an exactly aligned, identity-channel frame."""
    samples = np.asarray(audio, dtype=float).reshape(-1)
    if len(samples) != FRAME_SAMPLES:
        raise ValueError(f"oracle receiver requires exactly {FRAME_SAMPLES} samples")
    carriers = np.vstack([
        ofdm.symbol_carriers(GEOMETRY, samples[i * SYMBOL_SAMPLES:(i + 1) * SYMBOL_SAMPLES])
        for i in range(TOTAL_SYMBOLS)
    ])
    return _decode_packet(bits_from_qam32(carriers[TRAINING_SYMBOLS:]))


def _analytic_carriers(analytic: np.ndarray, start: int) -> np.ndarray:
    rows = []
    for index in range(TOTAL_SYMBOLS):
        at = start + index * SYMBOL_SAMPLES + GUARD_SAMPLES
        core = analytic[at:at + CORE_SAMPLES]
        if len(core) != CORE_SAMPLES:
            raise ValueError("capture ends before the complete HC2 frame")
        rows.append(np.fft.fft(core)[CARRIER_BINS] / (2 * GEOMETRY.time_scale))
    return np.vstack(rows)


def demodulate(audio: np.ndarray, *, max_frequency_offset_hz: float = 20.0,
               acquisition_step_hz: float = 1.0,
               return_diagnostics: bool = False):
    """Acquire/equalize one frame in a clean or benign real-audio capture."""
    samples = np.asarray(audio, dtype=float).reshape(-1)
    if len(samples) < FRAME_SAMPLES:
        return (None, {}) if return_diagnostics else None
    if max_frequency_offset_hz < 0 or acquisition_step_hz <= 0:
        raise ValueError("invalid acquisition search bounds")
    analytic = signal.hilbert(samples)
    template = signal.hilbert(ofdm.build_symbol(GEOMETRY, _TRAINING[0]))
    sample_index = np.arange(len(samples), dtype=float)
    best_metric, best_start, best_coarse = -1.0, 0, 0.0
    frequencies = np.arange(-max_frequency_offset_hz,
                            max_frequency_offset_hz + acquisition_step_hz / 2,
                            acquisition_step_hz)
    template_energy = np.vdot(template, template).real
    for frequency in frequencies:
        corrected = analytic * np.exp(-2j * np.pi * frequency * sample_index / SAMPLE_RATE)
        correlation = signal.correlate(corrected, template, mode="valid", method="fft")
        energy = signal.convolve(np.abs(corrected) ** 2, np.ones(len(template)),
                                 mode="valid", method="fft")
        metric = np.abs(correlation) ** 2 / np.maximum(energy * template_energy, 1e-30)
        # A complete frame must remain after the candidate.  The two training
        # symbols are intentionally identical, so select the earliest member
        # of the near-equal correlation peak rather than acquiring training 2.
        metric = metric[:len(samples) - FRAME_SAMPLES + 1]
        peak = float(np.max(metric))
        at = int(np.flatnonzero(metric >= peak * 0.995)[0])
        if peak > best_metric:
            best_metric, best_start, best_coarse = peak, at, float(frequency)
    coarse = analytic * np.exp(-2j * np.pi * best_coarse * sample_index / SAMPLE_RATE)
    training = _analytic_carriers(coarse, best_start)[:TRAINING_SYMBOLS]
    residual = np.angle(np.sum(training[1] * np.conj(training[0]))) / (
        2 * np.pi * SYMBOL_SAMPLES / SAMPLE_RATE)
    frequency = best_coarse + residual
    corrected = analytic * np.exp(-2j * np.pi * frequency * sample_index / SAMPLE_RATE)
    grid = _analytic_carriers(corrected, best_start)
    channel = np.mean(grid[:TRAINING_SYMBOLS] / _TRAINING, axis=0)
    result = None
    if np.all(np.abs(channel) >= 1e-8):
        equalized = grid[TRAINING_SYMBOLS:] / channel
        tracked = np.empty_like(equalized)
        phase = 0.0
        for index, row in enumerate(equalized):
            provisional = row * np.exp(-1j * phase)
            decisions = qam32_from_bits(bits_from_qam32(provisional))
            phase += np.angle(np.sum(provisional * np.conj(decisions)))
            tracked[index] = row * np.exp(-1j * phase)
        result = _decode_packet(bits_from_qam32(tracked))
    diagnostics = {"start_sample": best_start, "frequency_offset_hz": frequency,
                   "acquisition_metric": best_metric}
    return (result, diagnostics) if return_diagnostics else result


def rate_accounting() -> dict[str, float | int]:
    return {
        "raw_bit_rate": RAW_BIT_RATE,
        "fec_rate": 3 / 4,
        "coded_information_rate": CODED_INFORMATION_RATE,
        "training_symbols": TRAINING_SYMBOLS,
        "payload_symbols": PAYLOAD_SYMBOLS,
        "frame_seconds": FRAME_SECONDS,
        "max_payload_bytes": MAX_PAYLOAD_BYTES,
        "sustained_user_bit_rate": SUSTAINED_USER_BIT_RATE,
    }
