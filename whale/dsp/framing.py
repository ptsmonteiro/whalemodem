"""The payload codec: length, CRC32, whitening, FEC and interleaving.

One object owns the whole path from payload bytes to the coded bits a
mode puts on its carriers, and back.  It is parameterized on how many
coded bits the frame has room for; everything else -- how many packet
bytes that leaves, how much payload fits after the length and CRC fields
-- follows from that and from the code.

The wire format is VF2's, unchanged:

    [2-byte big-endian length][payload][4-byte big-endian CRC32][zero fill]

whitened against a PN sequence, convolutionally coded with a zero tail,
then interleaved.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np

from . import bits as _bits
from .fec import K7, ConvolutionalCode
from .interleave import Interleaver

LENGTH_BYTES = 2
CRC_BYTES = 4


@dataclass(frozen=True)
class PacketCodec:
    """Payload bytes <-> the coded, interleaved bits of one frame."""

    payload_bits: int
    interleaver: Interleaver
    whitener_seed: int
    code: ConvolutionalCode = field(default=K7)

    def __post_init__(self) -> None:
        if self.payload_bits % 2:
            raise ValueError("a rate-1/2 frame needs an even coded-bit count")
        if self.interleaver.size != self.payload_bits:
            raise ValueError(
                f"interleaver is {self.interleaver.size} bits wide but the "
                f"frame carries {self.payload_bits}")
        if self.information_bits <= self.code.tail_bits:
            raise ValueError("frame is too small to carry a terminated packet")

    @property
    def information_bits(self) -> int:
        return self.payload_bits // 2

    @property
    def packet_bytes(self) -> int:
        return (self.information_bits - self.code.tail_bits) // 8

    @property
    def unused_information_bits(self) -> int:
        """Bits the packet cannot use because they do not fill a byte."""
        return (self.information_bits - self.code.tail_bits) % 8

    @property
    def max_payload_bytes(self) -> int:
        return self.packet_bytes - LENGTH_BYTES - CRC_BYTES

    @cached_property
    def _whitener(self) -> np.ndarray:
        return _bits.pn_bits(self.packet_bytes * 8, self.whitener_seed)

    def encode(self, payload: bytes) -> np.ndarray:
        payload = bytes(payload)
        if len(payload) > self.max_payload_bytes:
            raise ValueError(
                f"payload is {len(payload)} bytes; the maximum is "
                f"{self.max_payload_bytes}")
        packet = bytearray(self.packet_bytes)
        packet[0:LENGTH_BYTES] = len(payload).to_bytes(LENGTH_BYTES, "big")
        packet[LENGTH_BYTES:LENGTH_BYTES + len(payload)] = payload
        crc_at = LENGTH_BYTES + len(payload)
        packet[crc_at:crc_at + CRC_BYTES] = (
            binascii.crc32(payload) & 0xFFFFFFFF).to_bytes(CRC_BYTES, "big")
        information = np.zeros(self.information_bits, dtype=np.uint8)
        information[:self.packet_bytes * 8] = (
            np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
            ^ self._whitener)
        return self.interleaver.spread(self.code.encode(information))

    def decode_hard(self, coded_bits: np.ndarray) -> tuple[bytes | None, dict]:
        coded_bits = np.asarray(coded_bits, dtype=np.uint8).reshape(-1)
        if len(coded_bits) != self.payload_bits:
            raise ValueError(
                f"expected {self.payload_bits} bits, got {len(coded_bits)}")
        gathered = self.interleaver.gather(coded_bits)
        return self._unpack(self.code.decode_hard(gathered))

    def decode_soft(self, soft_bits: np.ndarray) -> tuple[bytes | None, dict]:
        soft_bits = np.asarray(soft_bits, dtype=np.float64).reshape(-1)
        if len(soft_bits) != self.payload_bits:
            raise ValueError(
                f"expected {self.payload_bits} soft bits, got {len(soft_bits)}")
        gathered = self.interleaver.gather(soft_bits)
        return self._unpack(self.code.decode_soft(gathered))

    def _unpack(self, information: np.ndarray) -> tuple[bytes | None, dict]:
        tail_ok = not np.any(information[-self.code.tail_bits:])
        packet = np.packbits(
            information[:self.packet_bytes * 8] ^ self._whitener).tobytes()
        length = int.from_bytes(packet[:LENGTH_BYTES], "big")
        meta = {"decoded_length": length, "crc_ok": False,
                "fec_tail_ok": tail_ok}
        if length > self.max_payload_bytes:
            meta["failure"] = "invalid length"
            return None, meta
        payload = packet[LENGTH_BYTES:LENGTH_BYTES + length]
        crc_at = LENGTH_BYTES + length
        received_crc = int.from_bytes(packet[crc_at:crc_at + CRC_BYTES], "big")
        computed_crc = binascii.crc32(payload) & 0xFFFFFFFF
        meta.update(received_crc32=received_crc, computed_crc32=computed_crc,
                    crc_ok=received_crc == computed_crc)
        if not meta["crc_ok"]:
            meta["failure"] = "CRC mismatch"
            return None, meta
        return payload, meta
