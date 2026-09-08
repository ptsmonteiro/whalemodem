"""Matched HF15 FEC-rate measurement modes.

This module is intentionally a benchmark harness, not a production mode.  It
keeps the inner packet and shared single-carrier waveform fixed while changing only the
K=7 puncturing rate.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from whale.phy import sc_fast
from whale.dsp import bits as _bits
from whale.dsp import fec as _fec
from whale.dsp import interleave as _interleave


INNER_PACKET_BYTES = 1491
INNER_MAX_PAYLOAD_BYTES = INNER_PACKET_BYTES - sc_fast.sc.LENGTH_BYTES - sc_fast.sc.CRC_BYTES
INNER_INFORMATION_BITS = INNER_PACKET_BYTES * 8 + _fec.K7.tail_bits
PHY_BAUD = 1500.0
PHY_BITS_PER_SYMBOL = 3
PHY_PILOT_INTERVAL = 150


def _mask_for_rate(rate: str) -> np.ndarray:
    if rate == "1/2":
        return np.ones(2, dtype=bool)
    if rate == "2/3":
        return np.array([True, True, True, False], dtype=bool)
    if rate == "3/4":
        return np.array([True, True, False, True, False, True], dtype=bool)
    raise ValueError(f"unsupported rate {rate!r}")


def _puncture(mother: np.ndarray, mask: np.ndarray) -> np.ndarray:
    kept = np.resize(mask, len(mother))
    return mother[kept]


def _depuncture(received: np.ndarray, mask: np.ndarray, mother_len: int) -> np.ndarray:
    kept = np.resize(mask, mother_len)
    out = np.zeros(mother_len, dtype=received.dtype)
    out[kept] = received[:int(np.count_nonzero(kept))]
    return out


@dataclass(frozen=True)
class FecRateSweepMode:
    rate: str

    name: str = "hf15-sweep"
    mode_id: int = 120
    chunk_size: int = INNER_MAX_PAYLOAD_BYTES - 10
    confidence_threshold: float = 0.12
    tx_sample_rate: int = sc_fast.TX_SAMPLE_RATE
    rx_sample_rate: int = sc_fast.RX_SAMPLE_RATE

    def __post_init__(self) -> None:
        mask = _mask_for_rate(self.rate)
        mother_len = 2 * INNER_INFORMATION_BITS
        transmitted_bits = int(np.count_nonzero(np.resize(mask, mother_len)))
        coded_bytes = (transmitted_bits + 7) // 8
        packet_bytes = ((sc_fast.sc.LENGTH_BYTES + coded_bytes
                         + sc_fast.sc.CRC_BYTES + 2) // 3) * 3
        outer_coded_bytes = packet_bytes - sc_fast.sc.LENGTH_BYTES - sc_fast.sc.CRC_BYTES
        if packet_bytes * 8 % PHY_BITS_PER_SYMBOL:
            raise ValueError("sweep packet is not 8PSK-symbol aligned")
        object.__setattr__(self, "_mask", mask)
        object.__setattr__(self, "_mother_len", mother_len)
        object.__setattr__(self, "_transmitted_bits", transmitted_bits)
        object.__setattr__(self, "_coded_bytes", coded_bytes)
        object.__setattr__(self, "_outer_coded_bytes", outer_coded_bytes)
        object.__setattr__(self, "_packet_bytes", packet_bytes)
        object.__setattr__(self, "_interleaver",
                           _interleave.block(8, (outer_coded_bytes * 8) // 8))
        object.__setattr__(self, "_phy", sc_fast.SingleCarrierMode(
            baud=PHY_BAUD, bits_per_symbol=PHY_BITS_PER_SYMBOL,
            packet_bytes=packet_bytes, pilot_interval=PHY_PILOT_INTERVAL))

    def _inner_coded(self, payload: bytes) -> np.ndarray:
        packet = sc_fast.sc._pack_packet(payload, INNER_PACKET_BYTES)
        information = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
        terminated = np.concatenate((information,
                                     np.zeros(_fec.K7.tail_bits, dtype=np.uint8)))
        mother = _fec.K7.encode(terminated)
        transmitted = _puncture(mother, self._mask)
        padded = np.pad(transmitted, (0, self._outer_coded_bytes * 8 - len(transmitted)))
        return self._interleaver.spread(padded)

    def encode(self, payload: bytes, *, include_head: bool = True,
               head_seconds: float = ...) -> np.ndarray:
        if len(payload) > INNER_MAX_PAYLOAD_BYTES:
            raise ValueError("payload too large for FEC-rate sweep")
        outer = np.packbits(self._inner_coded(bytes(payload))).tobytes()
        return self._phy.modulate(outer)

    def _decode(self, result: dict) -> dict:
        raw_bits = result.pop("raw_bits", None)
        raw_soft = result.pop("raw_soft_bits", None)
        if raw_bits is None:
            result.update(payload=None, crc_ok=False)
            return result
        start = sc_fast.sc.LENGTH_BYTES * 8
        stop = start + self._outer_coded_bytes * 8
        if len(raw_bits) < stop:
            result.update(payload=None, crc_ok=False, failure="short coded payload")
            return result
        received = self._interleaver.gather(np.asarray(raw_bits[start:stop], dtype=np.uint8))
        received = received[:self._transmitted_bits]
        if raw_soft is None:
            mother = _depuncture(received, self._mask, self._mother_len)
            decoded = _fec.K7.decode_hard(mother)
        else:
            soft = self._interleaver.gather(np.asarray(raw_soft[start:stop], dtype=np.float64))
            soft = soft[:self._transmitted_bits]
            mother = _depuncture(soft, self._mask, self._mother_len)
            decoded = _fec.K7.decode_soft(mother)
        packet = np.packbits(decoded[:INNER_PACKET_BYTES * 8]).tobytes()
        payload, meta = sc_fast.sc._unpack_packet(packet, INNER_MAX_PAYLOAD_BYTES)
        result.update(meta, payload=payload)
        return result

    def decode(self, audio: np.ndarray, *, head_seconds: float = ...) -> dict:
        samples = np.asarray(audio)
        if samples.ndim != 1 or not np.all(np.isfinite(samples)):
            return {"synced": False, "crc_ok": False, "payload": None,
                    "confidence": 0.0, "freq_offset_hz": None,
                    "channel_snr_db": None}
        return self._decode(self._phy.demodulate(samples))

    def airtime(self, payload_len: int) -> float:
        return self._phy.frame_seconds()

    @property
    def fec_rate(self) -> float:
        return float(self.rate.split("/")[0]) / float(self.rate.split("/")[1])

    @property
    def net_throughput_bps(self) -> float:
        return 8 * self.chunk_size / self.airtime(self.chunk_size)
