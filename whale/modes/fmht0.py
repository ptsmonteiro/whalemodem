"""FMHT0: rung 0 of the FM-handheld ladder -- the link-setup waveform.

The bottom rung of the mic/speaker handheld family: beacon, link setup and
ACK-only traffic.  It must decode at squelch break, so every choice here
buys robustness and none buys rate.

  * 4-tone noncoherent FSK at 400 Bd, tones 400 Hz apart at 800, 1,200,
    1,600 and 2,000 Hz.  One tone per symbol, so the audio is
    constant-envelope and every symbol uses the full deviation -- the mic
    compressor and deviation clipper have nothing to squash.  The tones sit
    well inside the 400-2,500 Hz usable passband, away from both edges.
  * No equalizer and no channel estimate.  The de-emphasis tilt across four
    tones spanning 1.2 kHz is real, so the decoder reads the bank with
    `dsp.mfsk.fitted_soft_bits`, which fits each tone's own noise power and
    amplitude rather than assuming the bins share a floor.  That is a
    per-bin normalizer, not an equalizer: nothing is inverted and nothing is
    carried between frames.
  * Overall rate 1/3: the terminated K=9 rate-1/2 convolutional code every
    robust mode here uses, interleaved across the whole codeword, then a
    3:2 repetition -- the full codeword followed by every second coded bit
    again.  The receiver adds the repeat LLRs back into their originals
    before de-interleaving, so the repeat is soft-combined, and the two
    copies of a repeated bit are half a frame apart.
  * 250 ms preamble shared with the rest of the family: a 100 ms PN energy
    burst to open squelch and settle the receive AGC, then a 150 ms PN tone
    sequence for timing.  FM discriminator audio has no carrier offset, so
    there is no offset search -- only sound-card clock offset, which over a
    frame this short is under a sample.
  * Same `PacketCodec` framing, CRC32 grid selection and short/medium/full
    grids as the rest of the ladder, so the link's selective-repeat ARQ and
    block ACKs ride on it unchanged.  The short grid is sized exactly to a
    DATA_ACK.

Audio path: mic/speaker.  This mode assumes a compressed, pre-emphasized
handheld audio path and is not the right waveform for a flat 9,600 baud
data jack.

Simulated flat_nbfm C/N floor: not measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np

from .. import dsp
from ..dsp import mfsk as _mfsk
from .vf14 import Vf14Mode, _interleaver_stride

FMHT0_MODE_ID = 30

#: Transmitted bits per mother (rate-1/2) coded bit: the whole codeword plus
#: every second bit again.  1/2 * 2/3 = overall rate 1/3.
REPEAT_NUMERATOR, REPEAT_DENOMINATOR = 3, 2


def _expand(coded_bits: np.ndarray) -> np.ndarray:
    """The codeword followed by every second one of its bits."""
    coded_bits = np.asarray(coded_bits).reshape(-1)
    return np.concatenate((coded_bits, coded_bits[::2]))


def _combine(llrs: np.ndarray) -> np.ndarray:
    """Fold `_expand`'s repeat half back into the originals."""
    llrs = np.asarray(llrs, dtype=np.float64).reshape(-1)
    mother = 2 * len(llrs) // 3
    folded = llrs[:mother].copy()
    folded[::2] += llrs[mother:]
    return folded


@dataclass(frozen=True)
class _RepeatedCodec:
    """A `PacketCodec` seen through `_expand`/`_combine`.

    Wrapping the codec rather than the mode keeps `Vf14Mode.modulate` and
    `.demodulate` untouched: they still call `encode` and `decode_soft`, and
    the repetition lives entirely in this pair of methods.
    """

    inner: dsp.PacketCodec

    @property
    def max_payload_bytes(self) -> int:
        return self.inner.max_payload_bytes

    @property
    def coded_bits(self) -> int:
        return (self.inner.coded_bits * REPEAT_NUMERATOR) // REPEAT_DENOMINATOR

    def encode(self, payload: bytes) -> np.ndarray:
        return _expand(self.inner.encode(payload))

    def decode_soft(self, llrs: np.ndarray):
        return self.inner.decode_soft(_combine(llrs))


@dataclass(frozen=True)
class Fmht0Mode(Vf14Mode):
    """VF14's 4-FSK waveform re-fitted for rung 0: 400 Bd and rate 1/3."""

    def _make_codec(self, symbols: int, seed: int) -> _RepeatedCodec:
        transmitted = symbols * self.bits_per_symbol
        if transmitted % REPEAT_NUMERATOR:
            raise ValueError(
                f"{symbols} symbols x {self.bits_per_symbol} bits = "
                f"{transmitted} transmitted bits is not a multiple of "
                f"{REPEAT_NUMERATOR}, the repetition period")
        mother = (transmitted * REPEAT_DENOMINATOR) // REPEAT_NUMERATOR
        if mother % 2:
            raise ValueError("a rate-1/2 grid needs an even coded-bit count")
        return _RepeatedCodec(dsp.PacketCodec(
            payload_bits=mother,
            interleaver=dsp.interleave.multiplicative(
                mother, _interleaver_stride(mother)),
            whitener_seed=seed, code=dsp.fec.K9, puncture_rate="1/2"))

    #: `dsp.mfsk.correlate`'s default search grid is the symbol divided by a
    #: fixed divisor, which at 400 Bd is 30/4 = 7 samples -- not a divisor of
    #: the 30-sample symbol, so the search walks 28 samples per symbol and
    #: smears the timing peak away entirely. 6 divides 30, so the search
    #: lands on true symbol boundaries.
    search_step: int = 6

    def acquire(self, samples: np.ndarray) -> tuple[int | None, float]:
        bank = self.rx_bank
        scores, step = _mfsk.correlate(bank, samples, self.sync_pattern,
                                       step=self.search_step)
        if not len(scores):
            return None, 0.0
        coarse = int(np.argmax(scores))
        score = float(scores[coarse])
        if score < self.confidence_threshold:
            return coarse * step, score
        start = _mfsk.refine(bank, samples, self.sync_pattern, coarse * step,
                             radius=step, step=1)
        return int(start), score

    @property
    def fec(self) -> str:
        return "K=9 conv 1/2 + 3:2 repetition (rate 1/3)"

    @cached_property
    def codec(self) -> _RepeatedCodec:
        return self._make_codec(self.payload_symbols, self.whitener_seed)

    @cached_property
    def short_codec(self) -> _RepeatedCodec:
        return self._make_codec(self.short_payload_symbols,
                                self.short_whitener_seed)

    @cached_property
    def medium_codec(self) -> _RepeatedCodec:
        return self._make_codec(self.medium_payload_symbols,
                                self.medium_whitener_seed)


#: 900 full-grid symbols = 1,800 transmitted bits = 1,200 mother bits = 592
#: packet bits = 68 packet bytes, 58 of them application payload after the
#: 10-byte air header.  The short grid is 216 symbols, exactly the 11 bytes
#: of a DATA_ACK; the medium grid is 420 symbols for CONNECT exchanges.
FMHT0 = Fmht0Mode(
    name="fmht0", mode_id=FMHT0_MODE_ID, tone_count=4,
    symbol_samples=120, first_bin=2,
    sync_symbols=60, payload_symbols=900, short_payload_symbols=216,
    medium_payload_symbols=420,
    head_seconds=0.10,
    soft_metric="per_bin",
    fec_rate="1/2",
    sync_seed=0x0FB30, head_seed=0x0FB31,
    whitener_seed=0x0FB32, short_whitener_seed=0x0FB33,
    medium_whitener_seed=0x0FB34,
    confidence_threshold=0.145)
