"""Pilot-tracked differential QPSK on HC2b's selected 1.521 s frame.

Eight known full-band payload pilots replace data symbols.  They measure
per-carrier phase evolution after the header; unwrapped phase is interpolated
between pilots before differential decoding, and every pilot resets the
differential chain.  Everything else is production HC1 via HC2b's temporary,
restoring configuration seam.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import numpy as np

from whale import dsp, framing
from whale.modes import hc1, hf_lead

from experiments.hc2b import hc2b


PAYLOAD_SYMBOLS = 90
PILOT_POSITIONS = np.arange(10, 90, 10, dtype=np.int32)
DATA_POSITIONS = np.setdiff1d(np.arange(PAYLOAD_SYMBOLS), PILOT_POSITIONS)
DATA_SYMBOLS = len(DATA_POSITIONS)
PAYLOAD_BITS = DATA_SYMBOLS * hc1.BITS_PER_SYMBOL
INTERLEAVER_STRIDE = hc2b.best_multiplicative_stride(PAYLOAD_BITS)
CODEC = dsp.PacketCodec(
    payload_bits=PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS,
                                               INTERLEAVER_STRIDE),
    whitener_seed=0x1A5C7,
    code=dsp.K7,
)
PILOT_VALUES = dsp.bits.qpsk_from_bits(
    dsp.bits.pn_bits(len(PILOT_POSITIONS) * hc1.BITS_PER_SYMBOL, 0x13A6D)
).reshape(len(PILOT_POSITIONS), hc1.N_CARRIERS)


@dataclass(frozen=True)
class PilotVariant:
    payload_symbols: int = PAYLOAD_SYMBOLS
    name: str = "hc2c-qpsk-pilots-1521ms"
    mode_id: int = 21_000
    codec: dsp.PacketCodec = CODEC

    @property
    def total_symbols(self):
        return hc1.HEADER_SYMBOLS + self.payload_symbols

    @property
    def payload_bits(self):
        return self.codec.payload_bits

    @property
    def max_payload_bytes(self):
        return self.codec.max_payload_bytes

    @property
    def chunk_size(self):
        return self.max_payload_bytes - framing.AIR_HEADER_BYTES

    @property
    def frame_samples(self):
        return (hf_lead.MIN_SAMPLES + self.total_symbols * hc1.SYMBOL_SAMPLES
                + hc1.TAIL_SAMPLES)

    @property
    def frame_seconds(self):
        return self.frame_samples / hc1.SAMPLE_RATE


VARIANT = PilotVariant()


def _pilot_constellation(payload: bytes) -> np.ndarray:
    bits = CODEC.encode(payload).reshape(DATA_SYMBOLS, hc1.N_CARRIERS, 2)
    indices = bits[..., 0] * 2 + bits[..., 1]
    increments = dsp.differential.POINTS[indices]
    values = np.empty((PAYLOAD_SYMBOLS, hc1.N_CARRIERS), np.complex128)
    previous = hc1.HEADER_VALUES[-1]
    data_at = pilot_at = 0
    pilot_set = set(PILOT_POSITIONS.tolist())
    for symbol in range(PAYLOAD_SYMBOLS):
        if symbol in pilot_set:
            values[symbol] = PILOT_VALUES[pilot_at]
            pilot_at += 1
        else:
            values[symbol] = previous * increments[data_at]
            data_at += 1
        previous = values[symbol]
    return np.vstack((hc1.HEADER_VALUES, values))


class _TrackedDifferential:
    """The differential API HC1 calls, with pilots removed from bit output."""

    @staticmethod
    def observations(values, initial):
        values = np.asarray(values, np.complex128)
        anchors = np.concatenate((np.array([-1]), PILOT_POSITIONS))
        ratios = np.vstack((np.ones((1, hc1.N_CARRIERS), np.complex128),
                            values[PILOT_POSITIONS] / PILOT_VALUES))
        phases = np.unwrap(np.angle(ratios), axis=0)
        track = np.empty_like(values.real)
        positions = np.arange(PAYLOAD_SYMBOLS)
        for carrier in range(hc1.N_CARRIERS):
            track[:, carrier] = np.interp(
                positions, anchors, phases[:, carrier])
        corrected = values * np.exp(-1j * track)
        observations = np.empty_like(corrected)
        previous = np.asarray(initial)
        pilot_set = set(PILOT_POSITIONS.tolist())
        for symbol in range(PAYLOAD_SYMBOLS):
            if symbol in pilot_set:
                observations[symbol] = 1.0 + 0j
            else:
                delta = corrected[symbol] * np.conj(previous)
                observations[symbol] = delta / np.maximum(np.abs(delta), 1e-30)
            previous = corrected[symbol]
        return observations

    @staticmethod
    def decisions(values):
        return dsp.differential.decisions(values)

    @staticmethod
    def hard_bits(values):
        return dsp.differential.hard_bits(np.asarray(values)[DATA_POSITIONS])

    @staticmethod
    def soft_bits(values, weights):
        return dsp.differential.soft_bits(
            np.asarray(values)[DATA_POSITIONS], weights)


@contextmanager
def configured():
    with hc2b.configured(VARIANT):
        previous_constellation = hc1.frame_constellation
        previous_diff = hc1._diff  # noqa: SLF001 - deliberate experiment seam
        hc1.frame_constellation = _pilot_constellation
        hc1._diff = _TrackedDifferential  # noqa: SLF001
        try:
            yield
        finally:
            hc1.frame_constellation = previous_constellation
            hc1._diff = previous_diff  # noqa: SLF001


@dataclass(frozen=True)
class PilotMode:
    name: str = VARIANT.name
    mode_id: int = VARIANT.mode_id
    chunk_size: int = VARIANT.chunk_size
    confidence_threshold: float = hc1.ACQUISITION_THRESHOLD
    tx_sample_rate: int = hc1.SAMPLE_RATE
    rx_sample_rate: int = hc1.RX_SAMPLE_RATE
    baud: float = hc1.SAMPLE_RATE / hc1.SYMBOL_SAMPLES

    def encode(self, payload: bytes, **kwargs):
        with configured():
            head_seconds = kwargs.pop("head_seconds", hf_lead.MIN_SECONDS)
            body = hc1.modulate(payload, head_seconds=hc1.DEFAULT_HEAD_SECONDS)[
                hc1.lead_in_samples():]
            return np.concatenate((
                hf_lead.modulate(hf_lead.HC1_LABEL, head_seconds), body))

    def decode(self, audio, **kwargs):
        with configured():
            return hc1.demodulate(audio, **kwargs)

    def airtime(self, payload_len):
        del payload_len
        return VARIANT.frame_seconds


MODE = PilotMode()
