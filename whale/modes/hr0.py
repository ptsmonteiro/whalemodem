"""HR0 fixed-length, non-coherent 128-FSK robust HF frame.

The deliberately long orthogonal symbols buy processing gain while 128 tones
recover seven coded bits per symbol.  The result stays inside the 2.3 kHz HF
contract, uses soft K=9 convolutional coding, and keeps a maximum-size frame
below eight seconds including the common HF lead.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import hilbert

from .. import dsp, rx_audio
from ..dsp import mfsk

SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
SYMBOL_SAMPLES = 2_688
RX_SYMBOL_SAMPLES = SYMBOL_SAMPLES // rx_audio.DECIMATION
FIRST_BIN = 7
TONE_COUNT = 128
BANK = mfsk.ToneBank(SAMPLE_RATE, SYMBOL_SAMPLES, FIRST_BIN, TONE_COUNT)
RX_BANK = mfsk.ToneBank(RX_SAMPLE_RATE, RX_SYMBOL_SAMPLES, FIRST_BIN, TONE_COUNT)
BITS_PER_SYMBOL = BANK.bits_per_symbol

SYNC_SYMBOLS = 16
PAYLOAD_SYMBOLS = 112
PAYLOAD_BITS = PAYLOAD_SYMBOLS * BITS_PER_SYMBOL
TOTAL_SYMBOLS = SYNC_SYMBOLS + PAYLOAD_SYMBOLS
TAIL_SAMPLES = 960
RX_TAIL_SAMPLES = TAIL_SAMPLES // rx_audio.DECIMATION
TX_AMPLITUDE = 0.13 * np.sqrt(2.0)
ACQUISITION_THRESHOLD = 0.035
MAX_CFO_HZ = 50.0

SYNC_PATTERN = np.repeat(
    BANK.symbols_from_bits(dsp.bits.pn_bits((SYNC_SYMBOLS // 2) *
                                           BITS_PER_SYMBOL, 0x1D35B)), 2)
CODEC = dsp.PacketCodec(
    payload_bits=PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS, 313),
    whitener_seed=0x17A6D,
    code=dsp.K9,
)
MAX_PAYLOAD_BYTES = CODEC.max_payload_bytes


def modulate(payload: bytes) -> np.ndarray:
    tones = np.concatenate((SYNC_PATTERN, BANK.symbols_from_bits(CODEC.encode(payload))))
    body = mfsk.modulate(BANK, tones, TX_AMPLITUDE)
    return np.concatenate((body, np.zeros(TAIL_SAMPLES))).astype(np.float32)


def _base_result() -> dict:
    return {"synced": False, "payload": None, "confidence": 0.0,
            "start_index": None, "cfo_hz": 0.0, "raw_payload_bits": None}


def demodulate(audio: np.ndarray) -> dict:
    result = _base_result()
    try:
        samples = np.asarray(audio, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        result["failure"] = "invalid audio"
        return result
    if not np.all(np.isfinite(samples)) or len(samples) < SYNC_SYMBOLS * RX_SYMBOL_SAMPLES:
        result["failure"] = "invalid or short capture"
        return result
    # HR0's 17.9 Hz tone spacing alone only gives an unambiguous residual
    # estimate of +-8.9 Hz. Search the few whole-bin hypotheses needed by
    # the common HF +-46 Hz contract, then estimate the residual normally.
    analytic = hilbert(samples)
    index = np.arange(len(samples))
    hypotheses = np.arange(-MAX_CFO_HZ, MAX_CFO_HZ + BANK.spacing_hz,
                            BANK.spacing_hz)
    best = None
    for coarse_hz in hypotheses:
        corrected = np.real(analytic * np.exp(-2j * np.pi * coarse_hz * index /
                                              RX_SAMPLE_RATE))
        candidate_scores, candidate_step = mfsk.correlate(
            RX_BANK, corrected, SYNC_PATTERN)
        if len(candidate_scores):
            at = int(np.argmax(candidate_scores))
            candidate = (float(candidate_scores[at]), at, candidate_step,
                         float(coarse_hz), corrected)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None:
        result["failure"] = "preamble not found"
        return result
    confidence, coarse, step, coarse_hz, working = best
    start = coarse * step
    if confidence >= ACQUISITION_THRESHOLD:
        start = mfsk.refine(RX_BANK, working, SYNC_PATTERN, start, radius=step)
        confidence = max(confidence, mfsk.pattern_score(RX_BANK, working,
                                                        SYNC_PATTERN, start))
    result.update(confidence=confidence, start_index=start)
    if confidence < ACQUISITION_THRESHOLD:
        result["failure"] = "preamble not found"
        return result
    result["sync_end_index"] = start + SYNC_SYMBOLS * RX_SYMBOL_SAMPLES
    residual = mfsk.offset_hz(RX_BANK, working, start, SYNC_PATTERN)
    result["cfo_hz"] = coarse_hz + residual
    values = mfsk.analyze(RX_BANK, working, result["sync_end_index"],
                          PAYLOAD_SYMBOLS, residual)
    if values is None:
        result["failure"] = "frame truncated"
        return result
    result["end_index"] = min(len(samples), start + TOTAL_SYMBOLS *
                              RX_SYMBOL_SAMPLES + RX_TAIL_SAMPLES)
    magnitudes = np.abs(values)
    hard = RX_BANK.bits_from_symbols(np.argmax(magnitudes, axis=1))
    payload, meta = CODEC.decode_soft(mfsk.soft_bits(RX_BANK, magnitudes))
    result.update(meta)
    result.update(payload=payload, synced=True, raw_payload_bits=hard,
                  tone_magnitudes=magnitudes)
    return result


def frame_seconds(lead_samples: int) -> float:
    return (lead_samples + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES) / SAMPLE_RATE


assert BANK.bandwidth_hz <= 2_300.0
assert MAX_PAYLOAD_BYTES == 42
