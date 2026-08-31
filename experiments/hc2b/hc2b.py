"""HC2b: isolate the value and cost of longer HC1 QPSK frames.

This experiment deliberately changes only HC1's payload grid.  Acquisition,
carrier geometry, differential QPSK, channel estimation, rate-1/2 K=7 FEC,
CRC, transmit RMS, and decoder are the production HC1 implementation.  Four
payload lengths approximate 0.7, 1.0, 1.4, and 2.0 seconds on air.

HC1 is currently expressed with module-level geometry constants.  Rather
than copy that implementation, ``configure`` installs one immutable variant
before each sequential encode/decode.  It is intentionally unsuitable for a
registry or concurrent receiver and therefore remains under experiments/.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from math import gcd

import numpy as np

from whale import dsp, framing
from whale.modes import hc1, hf_lead


PAYLOAD_SYMBOL_MATRIX = (34, 58, 90, 130)
EXPERIMENTAL_MODE_ID_BASE = 20_000


def _circular_distance(value: int, size: int) -> int:
    value %= size
    return min(value, size - value)


def best_multiplicative_stride(size: int) -> int:
    """Choose balanced coded/on-air separation for this exact grid.

    The score maximizes the worse circular separation of adjacent on-air
    bits and adjacent coded bits.  The final term prefers spreading the two
    coded bits emitted by one trellis step across different OFDM symbols.
    """
    candidates = []
    for stride in range(2, size):
        if gcd(stride, size) != 1:
            continue
        inverse = pow(stride, -1, size)
        score = (min(_circular_distance(stride, size),
                     _circular_distance(inverse, size)),
                 _circular_distance(2 * inverse, size), -stride)
        candidates.append((score, stride))
    if not candidates:
        raise ValueError(f"no interleaver stride exists for {size} bits")
    return max(candidates)[1]


@dataclass(frozen=True)
class Variant:
    payload_symbols: int
    name: str
    mode_id: int
    codec: dsp.PacketCodec

    @property
    def total_symbols(self) -> int:
        return hc1.HEADER_SYMBOLS + self.payload_symbols

    @property
    def payload_bits(self) -> int:
        return self.payload_symbols * hc1.BITS_PER_SYMBOL

    @property
    def max_payload_bytes(self) -> int:
        return self.codec.max_payload_bytes

    @property
    def chunk_size(self) -> int:
        return self.max_payload_bytes - framing.AIR_HEADER_BYTES

    @property
    def frame_samples(self) -> int:
        return (hf_lead.MIN_SAMPLES + self.total_symbols * hc1.SYMBOL_SAMPLES
                + hc1.TAIL_SAMPLES)

    @property
    def frame_seconds(self) -> float:
        return self.frame_samples / hc1.SAMPLE_RATE


def make_variant(payload_symbols: int, index: int) -> Variant:
    payload_bits = payload_symbols * hc1.BITS_PER_SYMBOL
    stride = best_multiplicative_stride(payload_bits)
    codec = dsp.PacketCodec(
        payload_bits=payload_bits,
        interleaver=dsp.interleave.multiplicative(payload_bits, stride),
        whitener_seed=0x1A5C7,
        code=dsp.K7,
    )
    if codec.unused_information_bits:
        raise ValueError(
            f"{payload_symbols} symbols strand "
            f"{codec.unused_information_bits} information bits")
    milliseconds = round((hf_lead.MIN_SAMPLES
                          + (hc1.HEADER_SYMBOLS + payload_symbols)
                          * hc1.SYMBOL_SAMPLES + hc1.TAIL_SAMPLES)
                         / hc1.SAMPLE_RATE * 1000)
    return Variant(payload_symbols, f"hc2b-qpsk-{milliseconds}ms",
                   EXPERIMENTAL_MODE_ID_BASE + index, codec)


VARIANTS = tuple(make_variant(symbols, index)
                 for index, symbols in enumerate(PAYLOAD_SYMBOL_MATRIX))


_HC1_MUTABLE_NAMES = (
    "PAYLOAD_SYMBOLS", "TOTAL_SYMBOLS", "PAYLOAD_BITS", "FRAME_SAMPLES",
    "FRAME_SECONDS", "CODEC", "FEC_INPUT_BITS", "FEC_TAIL_BITS",
    "PACKET_BYTES", "UNUSED_INFO_BITS", "MAX_PAYLOAD_BYTES",
    "encode_payload_bits", "decode_payload_bits", "decode_payload_soft",
    "_TIMING_SYMBOLS",
)


def configure(variant: Variant) -> None:
    """Install a variant into HC1's module globals for one sequential trial."""
    hc1.PAYLOAD_SYMBOLS = variant.payload_symbols
    hc1.TOTAL_SYMBOLS = variant.total_symbols
    hc1.PAYLOAD_BITS = variant.payload_bits
    hc1.FRAME_SAMPLES = variant.frame_samples
    hc1.FRAME_SECONDS = variant.frame_seconds
    hc1.CODEC = variant.codec
    hc1.FEC_INPUT_BITS = variant.codec.information_bits
    hc1.FEC_TAIL_BITS = variant.codec.code.tail_bits
    hc1.PACKET_BYTES = variant.codec.packet_bytes
    hc1.UNUSED_INFO_BITS = variant.codec.unused_information_bits
    hc1.MAX_PAYLOAD_BYTES = variant.codec.max_payload_bytes
    hc1.encode_payload_bits = variant.codec.encode
    hc1.decode_payload_bits = variant.codec.decode_hard
    hc1.decode_payload_soft = variant.codec.decode_soft
    hc1._TIMING_SYMBOLS = np.arange(  # noqa: SLF001 - experiment seam
        hc1.SYNC_SYMBOLS, variant.total_symbols, dtype=np.int32)


@contextmanager
def configured(variant: Variant):
    """Temporarily configure HC1 and leave the production module untouched."""
    previous = {name: getattr(hc1, name) for name in _HC1_MUTABLE_NAMES}
    configure(variant)
    try:
        yield
    finally:
        for name, value in previous.items():
            setattr(hc1, name, value)


@dataclass(frozen=True)
class ExperimentalMode:
    variant: Variant

    @property
    def name(self):
        return self.variant.name

    @property
    def mode_id(self):
        return self.variant.mode_id

    @property
    def chunk_size(self):
        return self.variant.chunk_size

    confidence_threshold: float = hc1.ACQUISITION_THRESHOLD
    tx_sample_rate: int = hc1.SAMPLE_RATE
    rx_sample_rate: int = hc1.RX_SAMPLE_RATE
    baud: float = hc1.SAMPLE_RATE / hc1.SYMBOL_SAMPLES

    def encode(self, payload: bytes, **kwargs):
        with configured(self.variant):
            head_seconds = kwargs.pop("head_seconds", hf_lead.MIN_SECONDS)
            body = hc1.modulate(payload, head_seconds=hc1.DEFAULT_HEAD_SECONDS)[
                hc1.lead_in_samples():]
            return np.concatenate((
                hf_lead.modulate(hf_lead.HC1_LABEL, head_seconds), body))

    def decode(self, audio, **kwargs):
        with configured(self.variant):
            return hc1.demodulate(audio, **kwargs)

    def airtime(self, payload_len: int) -> float:
        del payload_len
        return self.variant.frame_seconds


MODES = tuple(ExperimentalMode(variant) for variant in VARIANTS)
