"""Waveform-independent DSP kernels.

The VF modes were each written standalone, and each grew its own copy of
the same OFDM build/analyze pair, acquisition correlator, cyclic-prefix
timing fit, Viterbi decoder and CRC framing.  Those are the kernels; this
package is the one place they live, parameterized on frame geometry rather
than on a mode's module-level constants.  A mode becomes a choice of
geometry and a wiring of these together -- see `whale/modes/vf3.py`.

  `bits`          PN sequences, QPSK mapping and slicing
  `ofdm`          the frame `Geometry`, symbol build/analyze, carrier banks
  `acquire`       preamble self-correlation and candidate ranking
  `timing`        cyclic-prefix symbol timing and sample-clock fit
  `freq`          coarse and fine carrier frequency offset
  `equalize`      per-carrier channel fit, pilot phase tracking, weights
  `differential`  differentially encoded QPSK
  `interleave`    multiplicative and block bit interleavers
  `mfsk`          non-coherent M-ary FSK: tone bank, Gray map, sync
  `fec`           rate-1/2 convolutional coding, hard and soft Viterbi
  `framing`       the length/CRC32/whitening/FEC payload codec
  `head`          how much of a transmitted lead-in survived

Everything here is numerically pinned by `tests/test_dsp_kernels.py` and,
through VF3, by the recorded-capture replay in
`tests/test_vf3_capture_replay.py`.  These are on-air-compatible
definitions, not a tidy-up: changing one changes what a station transmits.
"""

from . import (acquire, bits, differential, equalize, fec, framing, freq,
               head, interleave, mfsk, ofdm, timing)
from .fec import K7, K9, ConvolutionalCode
from .framing import PacketCodec
from .interleave import Interleaver
from .mfsk import ToneBank
from .ofdm import Geometry
from .timing import TimingFit

__all__ = [
    "acquire", "bits", "differential", "equalize", "fec", "framing", "freq",
    "head", "interleave", "mfsk", "ofdm", "timing",
    "ConvolutionalCode", "Geometry", "Interleaver", "K7", "K9", "PacketCodec",
    "TimingFit", "ToneBank",
]
