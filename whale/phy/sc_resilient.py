"""HF4-derived resilient single-carrier PHY.

Developed as `experiments/hf15_resilient/sc_resilient.py` and moved here
unmodified when it became shipped product code; the code-rate evidence
behind it is in `experiments/hf15_resilient/`.

The RF waveform is deliberately kept compatible with HF4's acquisition and
pilot-tracking path.  The payload carried inside that waveform is protected
by a terminated K=7 rate-1/2 convolutional code and a block interleaver.  A
small outer HF4 packet wrapper is retained so the mature HF4 demodulator can
still provide its synchronizer, equalizer, and raw hard bits.
"""

from __future__ import annotations

import numpy as np

from whale.dsp import bits as _bits
from whale.dsp import fec as _fec
from whale.dsp import interleave as _interleave
from whale.phy import sc_fast


TX_SAMPLE_RATE = sc_fast.TX_SAMPLE_RATE
RX_SAMPLE_RATE = sc_fast.RX_SAMPLE_RATE
DESIGN_RATE = sc_fast.DESIGN_RATE

# Inner packet: length + link payload + CRC.  The outer HF4 wrapper needs
# 2*N+2 coded bytes, plus its own six-byte length/CRC wrapper.  2991 is the
# smallest packet size divisible by 3 that carries that complete wrapper.
INNER_PACKET_BYTES = 1491
INNER_MAX_PAYLOAD_BYTES = INNER_PACKET_BYTES - sc_fast.sc.LENGTH_BYTES - sc_fast.sc.CRC_BYTES
OUTER_PACKET_BYTES = 2991
CODED_BYTES = 2 * INNER_PACKET_BYTES + 2
assert CODED_BYTES == 2984
assert CODED_BYTES + sc_fast.sc.LENGTH_BYTES + sc_fast.sc.CRC_BYTES <= OUTER_PACKET_BYTES

BAUD = 1500.0
BITS_PER_SYMBOL = 3
PILOT_INTERVAL = 150
PHY = sc_fast.SingleCarrierMode(
    baud=BAUD, bits_per_symbol=BITS_PER_SYMBOL,
    packet_bytes=OUTER_PACKET_BYTES, pilot_interval=PILOT_INTERVAL)
CODE = _fec.K7
# 8 rows keeps adjacent coded bits apart while dividing the fixed coded
# length exactly (23,872 bits = 8 x 2,984).
INTERLEAVER = _interleave.block(8, (CODED_BYTES * 8) // 8)


def _inner_packet(payload: bytes) -> np.ndarray:
    packet = sc_fast.sc._pack_packet(payload, INNER_PACKET_BYTES)
    raw = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
    terminated = np.concatenate((raw, np.zeros(CODE.tail_bits, dtype=np.uint8)))
    coded = CODE.encode(terminated)
    # K=7 termination makes the coded length four bits short of a byte.
    coded = np.concatenate((coded, np.zeros(4, dtype=np.uint8)))
    return INTERLEAVER.spread(coded)


def _outer_bytes(payload: bytes) -> bytes:
    coded = _inner_packet(payload)
    return np.packbits(coded).tobytes()


def modulate(payload: bytes) -> np.ndarray:
    if len(payload) > INNER_MAX_PAYLOAD_BYTES:
        raise ValueError("payload too large for hf15")
    return PHY.modulate(_outer_bytes(bytes(payload)))


def _decode_inner(raw_bits: np.ndarray, raw_soft_bits: np.ndarray | None = None) -> tuple[bytes | None, dict]:
    start = sc_fast.sc.LENGTH_BYTES * 8
    stop = start + CODED_BYTES * 8
    if len(raw_bits) < stop:
        return None, {"crc_ok": False, "failure": "short coded payload"}
    received = INTERLEAVER.gather(np.asarray(raw_bits[start:stop], dtype=np.uint8))
    if raw_soft_bits is None:
        decoded = CODE.decode_hard(received[:-4])
    else:
        soft = INTERLEAVER.gather(np.asarray(raw_soft_bits[start:stop], dtype=np.float64))
        decoded = CODE.decode_soft(soft[:-4])
    packet_bits = decoded[:INNER_PACKET_BYTES * 8]
    packet = np.packbits(packet_bits).tobytes()
    return sc_fast.sc._unpack_packet(packet, INNER_MAX_PAYLOAD_BYTES)


def demodulate(captured_12k: np.ndarray) -> dict:
    samples = np.asarray(captured_12k)
    if samples.ndim != 1 or not np.all(np.isfinite(samples)):
        return {"synced": False, "crc_ok": False, "payload": None,
                "confidence": 0.0, "freq_offset_hz": None,
                "channel_snr_db": None}
    result = PHY.demodulate(samples)
    raw_bits = result.pop("raw_bits", None)
    raw_soft_bits = result.pop("raw_soft_bits", None)
    if raw_bits is None:
        result["payload"] = None
        result["crc_ok"] = False
        return result
    payload, meta = _decode_inner(raw_bits, raw_soft_bits)
    result.update(meta)
    result["payload"] = payload
    result["inner_fec"] = "k7_rate_1_2_block_interleaved"
    return result


def airtime() -> float:
    return PHY.frame_seconds()


def max_payload_bytes() -> int:
    return INNER_MAX_PAYLOAD_BYTES
