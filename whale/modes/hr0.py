"""HR0 short/full 32-FSK control waveform, promoted from MARGIN32.

The 2026-09-06 revision trades the previous 128-FSK margin for short ACK
latency: 1.812 s for 12 bytes and 3.860 s for 42 bytes with the common lead.
This is wire-incompatible with the previous mode-10 body. Both peers must
upgrade; the frozen old waveform lives only in the comparison experiment.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import hilbert

from .. import dsp, rx_audio
from ..dsp import mfsk

SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
SYMBOL_SAMPLES = 1_024
RX_SYMBOL_SAMPLES = SYMBOL_SAMPLES // rx_audio.DECIMATION
FIRST_BIN = 12
TONE_COUNT = 32
BANK = mfsk.ToneBank(SAMPLE_RATE, SYMBOL_SAMPLES, FIRST_BIN, TONE_COUNT)
RX_BANK = mfsk.ToneBank(RX_SAMPLE_RATE, RX_SYMBOL_SAMPLES, FIRST_BIN, TONE_COUNT)
BITS_PER_SYMBOL = BANK.bits_per_symbol

SYNC_SYMBOLS = 16
PAYLOAD_SYMBOLS = 158
PAYLOAD_BITS = PAYLOAD_SYMBOLS * BITS_PER_SYMBOL
TOTAL_SYMBOLS = SYNC_SYMBOLS + PAYLOAD_SYMBOLS
TAIL_SAMPLES = 960
RX_TAIL_SAMPLES = TAIL_SAMPLES // rx_audio.DECIMATION
TX_AMPLITUDE = 0.13 * np.sqrt(2.0)
ACQUISITION_THRESHOLD = 0.10

SYNC_PATTERN = np.repeat(
    BANK.symbols_from_bits(dsp.bits.pn_bits((SYNC_SYMBOLS // 2) *
                                           BITS_PER_SYMBOL, 0x1D35B)), 2)
CODEC = dsp.PacketCodec(
    payload_bits=PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS, 301),
    whitener_seed=0x17A98,
    code=dsp.K9,
)
MAX_PAYLOAD_BYTES = CODEC.max_payload_bytes

# A checked air header plus the two-byte DATA_ACK remainder fits in 12
# bytes. Both sizes use the same symbol energy and code; short controls
# avoid padding to the full DATA capacity. Both lengths share acquisition; CRC32
# selects between at most two bounded body decodes (short first).
SHORT_PAYLOAD_SYMBOLS = 62
SHORT_CODEC = dsp.PacketCodec(
    payload_bits=SHORT_PAYLOAD_SYMBOLS * BITS_PER_SYMBOL,
    interleaver=dsp.interleave.multiplicative(
        SHORT_PAYLOAD_SYMBOLS * BITS_PER_SYMBOL, 119),
    whitener_seed=0x17A7A,
    code=dsp.K9,
)
SHORT_MAX_PAYLOAD_BYTES = SHORT_CODEC.max_payload_bytes


def payload_symbols(payload_len: int) -> int:
    if not 0 <= payload_len <= MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload length must be between 0 and {MAX_PAYLOAD_BYTES}")
    return (SHORT_PAYLOAD_SYMBOLS if payload_len <= SHORT_MAX_PAYLOAD_BYTES
            else PAYLOAD_SYMBOLS)


def modulate(payload: bytes) -> np.ndarray:
    symbols = payload_symbols(len(payload))
    codec = SHORT_CODEC if symbols == SHORT_PAYLOAD_SYMBOLS else CODEC
    tones = np.concatenate((SYNC_PATTERN, BANK.symbols_from_bits(codec.encode(payload))))
    body = mfsk.modulate(BANK, tones, TX_AMPLITUDE)
    return np.concatenate((body, np.zeros(TAIL_SAMPLES))).astype(np.float32)


def _base_result() -> dict:
    return {"synced": False, "payload": None, "confidence": 0.0,
            "start_index": None, "cfo_hz": 0.0, "raw_payload_bits": None}


def demodulate(audio: np.ndarray) -> dict:
    result = _base_result()
    try:
        samples = np.asarray(audio, dtype=np.float64)
    except (TypeError, ValueError):
        result["failure"] = "invalid audio"
        return result
    if (samples.ndim != 1 or not np.all(np.isfinite(samples))
            or len(samples) < SYNC_SYMBOLS * RX_SYMBOL_SAMPLES):
        result["failure"] = "invalid or short capture"
        return result
    # Search whole-tone offsets before estimating residual CFO, matching
    # the experimental receiver and covering the common +/-46 Hz contract.
    analytic = hilbert(samples)
    index = np.arange(len(samples))
    hypotheses = (-BANK.spacing_hz, 0.0, BANK.spacing_hz)
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
    result.update(confidence=confidence, start_index=start)
    if confidence < ACQUISITION_THRESHOLD:
        result["failure"] = "preamble not found"
        return result
    result["sync_end_index"] = start + SYNC_SYMBOLS * RX_SYMBOL_SAMPLES
    residual = mfsk.offset_hz(RX_BANK, working, start, SYNC_PATTERN)
    result["cfo_hz"] = coarse_hz + residual
    for symbols, codec in ((SHORT_PAYLOAD_SYMBOLS, SHORT_CODEC),
                           (PAYLOAD_SYMBOLS, CODEC)):
        values = mfsk.analyze(RX_BANK, working, result["sync_end_index"],
                              symbols, residual)
        if values is None:
            # A failed short hypothesis may be the prefix of a full frame.
            # Do not publish an end_index that would make streaming RX
            # consume it before the full body has arrived.
            result["failure"] = "frame truncated"
            return result
        magnitudes = np.abs(values)
        payload, meta = codec.decode_soft(mfsk.soft_bits(RX_BANK, magnitudes))
        if payload is None and symbols == SHORT_PAYLOAD_SYMBOLS:
            continue
        result["end_index"] = min(len(samples), start + (SYNC_SYMBOLS + symbols) *
                                  RX_SYMBOL_SAMPLES + RX_TAIL_SAMPLES)
        hard = RX_BANK.bits_from_symbols(np.argmax(magnitudes, axis=1))
        result.update(meta)
        result.update(payload=payload, synced=True, raw_payload_bits=hard,
                      tone_magnitudes=magnitudes, payload_symbols=symbols)
        return result


def frame_seconds(lead_samples: int, payload_len: int = MAX_PAYLOAD_BYTES) -> float:
    symbols = SYNC_SYMBOLS + payload_symbols(payload_len)
    return (lead_samples + symbols * SYMBOL_SAMPLES + TAIL_SAMPLES) / SAMPLE_RATE


assert BANK.bandwidth_hz <= 2_300.0
assert MAX_PAYLOAD_BYTES == 42
assert SHORT_MAX_PAYLOAD_BYTES == 12
