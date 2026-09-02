"""HR0 viability prototype: oracle-start/CFO, strongly repeated BPSK OFDM.

This is deliberately not a production mode.  It asks the narrower question:
can an approximately eight-second, <=2300 Hz physical frame carry 53 bytes at
-15 dB SNR/3kHz through the standard mid-latitude Watterson cases when frame
boundary and carrier offset are already known?
"""

from __future__ import annotations

import binascii
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import ldpc  # noqa: E402


SAMPLE_RATE = 48_000
NFFT = 1_280
CP_SAMPLES = 128
SYMBOL_SAMPLES = NFFT + CP_SAMPLES
SUBCARRIER_HZ = SAMPLE_RATE / NFFT
CARRIER_BINS = np.arange(8, 61, dtype=np.int32)  # 300 through 2250 Hz
CARRIERS = len(CARRIER_BINS)
OCCUPIED_LOW_HZ = (CARRIER_BINS[0] - 0.5) * SUBCARRIER_HZ
OCCUPIED_HIGH_HZ = (CARRIER_BINS[-1] + 0.5) * SUBCARRIER_HZ
OCCUPIED_HZ = OCCUPIED_HIGH_HZ - OCCUPIED_LOW_HZ

# P-DD repeated 91 times, followed by a closing P.  Every data symbol is
# bracketed by channel estimates and the 2.67 ms CP exceeds the worst 2 ms
# differential delay in the three requested mid-latitude presets.
TOTAL_SYMBOLS = 274
PILOT_INDICES = np.arange(0, TOTAL_SYMBOLS, 3, dtype=np.int32)
DATA_INDICES = np.setdiff1d(np.arange(TOTAL_SYMBOLS), PILOT_INDICES)
FRAME_SAMPLES = TOTAL_SYMBOLS * SYMBOL_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
DATA_CELLS = len(DATA_INDICES) * CARRIERS

MAX_PAYLOAD_BYTES = 53
PACKET_BYTES = 1 + MAX_PAYLOAD_BYTES + 4
PACKET_BITS = PACKET_BYTES * 8
INFO_BITS_PER_BLOCK = PACKET_BITS // 2
FILLER_BITS_PER_BLOCK = ldpc.K - INFO_BITS_PER_BLOCK
TX_BITS_PER_BLOCK = INFO_BITS_PER_BLOCK + (ldpc.N - ldpc.K)
TX_CODE_BITS = 2 * TX_BITS_PER_BLOCK
FILLER_LLR = 50.0
TX_RMS = 0.13


def _bits_from_bytes(value: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(value, dtype=np.uint8))


def _bytes_from_bits(bits: np.ndarray) -> bytes:
    return np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes()


def build_packet(payload: bytes) -> bytes:
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"HR0 carries at most {MAX_PAYLOAD_BYTES} payload bytes")
    body = bytes((len(payload),)) + payload.ljust(MAX_PAYLOAD_BYTES, b"\0")
    return body + binascii.crc32(body).to_bytes(4, "big")


def parse_packet(packet: bytes) -> bytes | None:
    if len(packet) != PACKET_BYTES:
        return None
    body, received_crc = packet[:-4], packet[-4:]
    if binascii.crc32(body).to_bytes(4, "big") != received_crc:
        return None
    length = body[0]
    return None if length > MAX_PAYLOAD_BYTES else body[1:1 + length]


def encode_code_bits(payload: bytes) -> np.ndarray:
    packet_bits = _bits_from_bytes(build_packet(payload))
    transmitted = []
    for block_info in np.split(packet_bits, 2):
        information = np.zeros(ldpc.K, dtype=np.uint8)
        information[:INFO_BITS_PER_BLOCK] = block_info
        codeword = ldpc.encode(information)
        # Shortening: known-zero systematic filler is not sent.  All parity is.
        transmitted.append(np.concatenate((
            codeword[:INFO_BITS_PER_BLOCK], codeword[ldpc.K:])))
    return np.concatenate(transmitted)


def _cell_bit_indices() -> np.ndarray:
    """Map cells to bits with uniform 8/9x time/frequency diversity."""
    indices = np.empty(DATA_CELLS, dtype=np.int32)
    stride = 337  # coprime to 1112
    offset = 173
    for cell in range(DATA_CELLS):
        repetition, position = divmod(cell, TX_CODE_BITS)
        indices[cell] = (stride * position + repetition * offset) % TX_CODE_BITS
    return indices


CELL_BIT_INDICES = _cell_bit_indices()


def _pilot_values() -> np.ndarray:
    rng = np.random.default_rng(0x485230)
    return (1.0 - 2.0 * rng.integers(
        0, 2, (len(PILOT_INDICES), CARRIERS))).astype(np.complex128)


PILOTS = _pilot_values()


def modulate(payload: bytes) -> np.ndarray:
    code_bits = encode_code_bits(payload)
    cells = 1.0 - 2.0 * code_bits[CELL_BIT_INDICES]
    data_values = cells.reshape(len(DATA_INDICES), CARRIERS)
    grid = np.zeros((TOTAL_SYMBOLS, NFFT // 2 + 1), dtype=np.complex128)
    grid[PILOT_INDICES[:, None], CARRIER_BINS] = PILOTS
    grid[DATA_INDICES[:, None], CARRIER_BINS] = data_values
    useful = np.fft.irfft(grid, n=NFFT, axis=1)
    symbols = np.concatenate((useful[:, -CP_SAMPLES:], useful), axis=1)
    audio = symbols.reshape(-1)
    audio *= TX_RMS / np.sqrt(np.mean(audio * audio))
    return audio.astype(np.float32)


def _project_channel(raw: np.ndarray, taps: int = 8) -> np.ndarray:
    """Project a noisy contiguous-carrier pilot estimate onto short delays."""
    impulse = np.fft.ifft(raw)
    impulse[taps:] = 0.0
    return np.fft.fft(impulse)


def _channel_estimates(received_grid: np.ndarray) -> np.ndarray:
    raw = received_grid[PILOT_INDICES[:, None], CARRIER_BINS] / PILOTS
    projected = np.asarray([_project_channel(row) for row in raw])
    estimates = np.zeros((len(DATA_INDICES), CARRIERS), dtype=np.complex128)
    pilot_lookup = {int(symbol): index for index, symbol in enumerate(PILOT_INDICES)}
    for out_index, symbol in enumerate(DATA_INDICES):
        before = symbol - symbol % 3
        after = before + 3
        left, right = pilot_lookup[before], pilot_lookup[after]
        fraction = (symbol - before) / 3.0
        estimates[out_index] = ((1.0 - fraction) * projected[left]
                                + fraction * projected[right])
    return estimates


def _block_llr(transmitted_llr: np.ndarray) -> np.ndarray:
    full = np.empty(ldpc.N, dtype=float)
    full[:INFO_BITS_PER_BLOCK] = transmitted_llr[:INFO_BITS_PER_BLOCK]
    full[INFO_BITS_PER_BLOCK:ldpc.K] = FILLER_LLR
    full[ldpc.K:] = transmitted_llr[INFO_BITS_PER_BLOCK:]
    return full


def demodulate(audio: np.ndarray) -> dict:
    """Decode at sample zero and zero CFO: the stated oracle assumptions."""
    samples = np.asarray(audio, dtype=float).reshape(-1)
    if len(samples) < FRAME_SAMPLES:
        return {"payload": None, "crc_ok": False, "failure": "truncated"}
    symbols = samples[:FRAME_SAMPLES].reshape(TOTAL_SYMBOLS, SYMBOL_SAMPLES)
    useful = symbols[:, CP_SAMPLES:]
    received = np.fft.rfft(useful, n=NFFT, axis=1)
    data = received[DATA_INDICES[:, None], CARRIER_BINS]
    channel = _channel_estimates(received)
    # MRC over every repeated/interleaved BPSK cell.  Scale is immaterial to
    # normalized min-sum except relative to the known filler confidence.
    soft_cells = np.real(np.conj(channel) * data).reshape(-1)
    llr = np.zeros(TX_CODE_BITS, dtype=float)
    np.add.at(llr, CELL_BIT_INDICES, soft_cells)
    scale = np.median(np.abs(llr))
    if scale > 0:
        llr *= 4.0 / scale
    decoded_halves, iterations, checks = [], [], []
    for block_values in np.split(llr, 2):
        information, count, ok = ldpc.decode(_block_llr(block_values))
        decoded_halves.append(information[:INFO_BITS_PER_BLOCK])
        iterations.append(count)
        checks.append(ok)
    packet = _bytes_from_bits(np.concatenate(decoded_halves))
    payload = parse_packet(packet)
    return {
        "payload": payload,
        "crc_ok": payload is not None,
        "ldpc_ok": checks,
        "ldpc_iterations": iterations,
        "packet": packet,
    }


def describe() -> dict:
    repetitions = np.bincount(CELL_BIT_INDICES, minlength=TX_CODE_BITS)
    return {
        "sample_rate": SAMPLE_RATE,
        "nfft": NFFT,
        "cp_samples": CP_SAMPLES,
        "cp_ms": 1e3 * CP_SAMPLES / SAMPLE_RATE,
        "subcarrier_spacing_hz": SUBCARRIER_HZ,
        "carriers": CARRIERS,
        "carrier_edges_hz": [float(CARRIER_BINS[0] * SUBCARRIER_HZ),
                             float(CARRIER_BINS[-1] * SUBCARRIER_HZ)],
        "occupied_edges_hz": [OCCUPIED_LOW_HZ, OCCUPIED_HIGH_HZ],
        "occupied_hz": OCCUPIED_HZ,
        "total_symbols": TOTAL_SYMBOLS,
        "pilot_symbols": len(PILOT_INDICES),
        "data_symbols": len(DATA_INDICES),
        "frame_seconds": FRAME_SECONDS,
        "max_payload_bytes": MAX_PAYLOAD_BYTES,
        "packet_bytes": PACKET_BYTES,
        "ldpc_blocks": 2,
        "ldpc_n": ldpc.N,
        "ldpc_k": ldpc.K,
        "shortened_information_bits_per_block": INFO_BITS_PER_BLOCK,
        "omitted_filler_bits_per_block": FILLER_BITS_PER_BLOCK,
        "transmitted_code_bits": TX_CODE_BITS,
        "data_cells": DATA_CELLS,
        "repetition_min": int(repetitions.min()),
        "repetition_max": int(repetitions.max()),
        "payload_bit_rate": MAX_PAYLOAD_BYTES * 8 / FRAME_SECONDS,
    }


assert PACKET_BITS % 2 == 0
assert FRAME_SECONDS < 8.1
assert CP_SAMPLES / SAMPLE_RATE >= 0.002
assert OCCUPIED_HIGH_HZ <= 2300
assert DATA_CELLS >= TX_CODE_BITS
