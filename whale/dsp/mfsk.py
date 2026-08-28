"""Non-coherent M-ary FSK: tone bank, Gray mapping, soft metrics, sync.

The OFDM kernels in this package all rest on the same bet: that the
receiver can recover the transmitted *phase* well enough to use it.  Every
one of them -- the channel fit, the differential payload, the two frequency
estimators -- is machinery for making that bet pay off, and each piece
stops paying somewhere.  On a link that is 10 dB into the noise they stop
paying together, and the first to go is acquisition, because a
self-correlation's score is `SNR/(SNR+1)` and no amount of preamble moves
that.

MFSK does not make the bet.  Information is which of `M` tones is present,
detected as energy, so nothing in the receive path needs a phase reference:
not the demodulator, not the synchronizer, not the frequency estimator.
What it costs is bandwidth -- `M` tones carry `log2(M)` bits, so 16 tones
spend 16 slots to send 4 bits -- and bandwidth is the one thing an HF
control channel has to spare, because it is already spending seconds per
frame.

This module is the kernel; `whale/modes/hc0.py` is the mode that wires it
up.  Everything downstream of the tone magnitudes -- interleaving, the
convolutional code, the length/CRC32 packet -- is the same
`whale.dsp.framing`/`fec`/`interleave` the OFDM modes use, unchanged.

Four pieces:

  `ToneBank`     the geometry: sample rate, symbol length, which FFT bins
                 are tones.  Tone spacing is exactly one symbol rate, which
                 is what makes the tones orthogonal under *non-coherent*
                 detection and what makes each tone an exact FFT bin.
  `modulate`     tone indices to audio.  Phase-continuous for free: each
                 tone completes a whole number of cycles per symbol, so a
                 symbol boundary is never a phase discontinuity, and the
                 transmitted waveform is constant-envelope.
  `analyze`      audio to complex tone amplitudes, with a frequency-offset
                 hypothesis folded in at no cost.
  `correlate` /  finding a known tone pattern in a buffer, and measuring
  `offset_hz`    the carrier offset from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np

#: How finely `correlate` samples symbol timing, as a divisor of the symbol.
#: A quarter symbol always lands within an eighth of the correlation peak,
#: which is inside the flat top of a non-coherent tone detector's response;
#: `refine` then walks the winner in properly.
SEARCH_DIVISOR = 4

#: Windows quieter than this fraction of the buffer's loudest are not
#: scored.  Correlation is normalized, so near-silence would otherwise
#: produce a large ratio out of nothing.
RMS_FLOOR_FRACTION = 0.05


@dataclass(frozen=True)
class ToneBank:
    """One MFSK signal's shape.

    `first_bin` and `tone_count` are in units of `sample_rate /
    symbol_samples`, which is simultaneously the symbol rate, the tone
    spacing and the FFT bin width -- one number, because orthogonal
    non-coherent MFSK forces all three to be equal.
    """

    sample_rate: int
    symbol_samples: int
    first_bin: int
    tone_count: int

    def __post_init__(self) -> None:
        if self.tone_count < 2 or self.tone_count & (self.tone_count - 1):
            raise ValueError("tone_count must be a power of two")
        if self.first_bin < 1:
            raise ValueError("the lowest tone must be above DC")
        if self.first_bin + self.tone_count > self.symbol_samples // 2:
            raise ValueError("the highest tone must be below Nyquist")

    @property
    def spacing_hz(self) -> float:
        return self.sample_rate / self.symbol_samples

    @property
    def symbol_rate(self) -> float:
        return self.spacing_hz

    @property
    def bits_per_symbol(self) -> int:
        return int(self.tone_count).bit_length() - 1

    @property
    def bandwidth_hz(self) -> float:
        return self.tone_count * self.spacing_hz

    @cached_property
    def bins(self) -> np.ndarray:
        return np.arange(self.first_bin, self.first_bin + self.tone_count)

    @cached_property
    def tone_hz(self) -> np.ndarray:
        return self.bins * self.spacing_hz

    @property
    def symbol_seconds(self) -> float:
        return self.symbol_samples / self.sample_rate

    @property
    def offset_limit_hz(self) -> float:
        """Unambiguous range of `offset_hz`.

        The estimate is a phase step across one symbol, so it wraps at half
        a symbol rate -- which is also half a tone spacing, the point at
        which a tone would be misread as its neighbour anyway.
        """
        return self.spacing_hz / 2.0

    # -- Gray mapping ----------------------------------------------------
    #
    # Adjacent tones differ in one bit, so the error a noisy channel
    # actually makes -- reading a tone as its neighbour -- costs one bit
    # rather than several, which is what the convolutional code downstream
    # is best at absorbing.

    @cached_property
    def _gray(self) -> np.ndarray:
        i = np.arange(self.tone_count)
        return i ^ (i >> 1)

    @cached_property
    def _ungray(self) -> np.ndarray:
        return np.argsort(self._gray)

    def symbols_from_bits(self, bits: np.ndarray) -> np.ndarray:
        bits = np.asarray(bits, dtype=np.uint8).reshape(-1, self.bits_per_symbol)
        values = np.zeros(len(bits), dtype=np.int64)
        for column in range(self.bits_per_symbol):
            values = (values << 1) | bits[:, column]
        return self._ungray[values]

    def bits_from_symbols(self, tones: np.ndarray) -> np.ndarray:
        values = self._gray[np.asarray(tones, dtype=np.int64)]
        shifts = np.arange(self.bits_per_symbol - 1, -1, -1)
        return ((values[:, None] >> shifts[None, :]) & 1).reshape(-1).astype(np.uint8)


def modulate(bank: ToneBank, tones: np.ndarray, amplitude: float = 1.0
             ) -> np.ndarray:
    """Tone indices to real audio, one symbol each.

    Every tone is an exact multiple of the symbol rate, so each symbol
    holds a whole number of cycles and starting each one at zero phase is
    already continuous -- there is no phase to carry across the boundary
    and no discontinuity to splatter.  The result is constant-envelope,
    which is why this mode can be driven far harder than an OFDM one
    through the same transmitter: a peak-limited PA delivers roughly 8 dB
    more average power for a crest factor of 1.41 than for VF3-class 3.9.
    """
    tones = np.asarray(tones, dtype=np.int64).reshape(-1)
    phase = 2.0 * np.pi * np.arange(bank.symbol_samples) / bank.symbol_samples
    table = np.cos(bank.bins[:, None] * phase[None, :])
    return (amplitude * table[tones]).reshape(-1)


def analyze(bank: ToneBank, audio: np.ndarray, start: int, count: int,
            offset_hz: float = 0.0) -> np.ndarray | None:
    """Complex tone amplitudes for `count` symbols from `start`.

    `offset_hz` is a carrier-offset hypothesis, applied by mixing rather
    than by moving the analysis frequencies, so the per-symbol FFT stays an
    FFT.  Mixing a real signal by `exp(-j2*pi*f*t)` leaves the wanted
    positive-frequency content shifted down by `f` and puts its mirror near
    `-2f`, far from any tone bin, so no analytic signal is needed.

    The mixing exponent uses *absolute* sample indices, which is what makes
    the phases of successive symbols comparable -- `offset_hz` below
    depends on that.
    """
    span = count * bank.symbol_samples
    if start < 0 or start + span > len(audio):
        return None
    segment = np.asarray(audio[start:start + span], dtype=np.float64)
    if offset_hz:
        index = np.arange(start, start + span)
        segment = segment * np.exp(-2j * np.pi * offset_hz * index
                                   / bank.sample_rate)
    grid = segment.reshape(count, bank.symbol_samples)
    return np.fft.fft(grid, axis=1)[:, bank.bins]


def soft_bits(bank: ToneBank, magnitudes: np.ndarray) -> np.ndarray:
    """Max-log bit reliabilities from tone magnitudes; positive means zero.

    The metric is squared magnitude normalized by the symbol's own mean,
    which is what makes this indifferent to a receiver's AGC: only the
    contrast between the tones in one symbol carries information, never
    their absolute level.
    """
    metric = np.asarray(magnitudes, dtype=np.float64) ** 2
    metric = metric / np.maximum(np.mean(metric, axis=1, keepdims=True), 1e-30)
    labels = bank._gray
    out = np.empty((len(metric), bank.bits_per_symbol))
    for bit in range(bank.bits_per_symbol):
        set_bits = (labels >> (bank.bits_per_symbol - 1 - bit)) & 1
        out[:, bit] = (np.max(metric[:, set_bits == 0], axis=1)
                       - np.max(metric[:, set_bits == 1], axis=1))
    return out.reshape(-1)


def _magnitude_grid(bank: ToneBank, audio: np.ndarray, step: int
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Tone magnitudes for every `step`-sample window, and their RMS.

    Computed in chunks rather than as one big strided array: the whole
    thing for a 10 s buffer at a quarter-symbol step is tens of megabytes,
    and this runs on every decode poll on hardware that may not have them
    to spare.
    """
    audio = np.asarray(audio, dtype=np.float64)
    windows = (len(audio) - bank.symbol_samples) // step + 1
    if windows <= 0:
        return np.zeros((0, bank.tone_count)), np.zeros(0)
    magnitudes = np.empty((windows, bank.tone_count))
    rms = np.empty(windows)
    view = np.lib.stride_tricks.sliding_window_view(audio, bank.symbol_samples)
    for low in range(0, windows, 256):
        high = min(low + 256, windows)
        block = view[low * step:high * step:step]
        spectrum = np.fft.rfft(block, axis=1)[:, bank.bins]
        magnitudes[low:high] = np.abs(spectrum)
        rms[low:high] = np.sqrt(np.mean(block ** 2, axis=1))
    return magnitudes, rms


def correlate(bank: ToneBank, audio: np.ndarray, pattern: np.ndarray, *,
              step: int | None = None,
              rms_floor_fraction: float = RMS_FLOOR_FRACTION
              ) -> tuple[np.ndarray, int]:
    """Score every candidate start against a known tone `pattern`.

    Returns (scores, step).  A score is the pattern's own tone energy as a
    fraction of all the energy in the same windows, after the across-tone
    mean is removed at each instant.

    That subtraction is the whole trick, and it is the bug
    `experiments/mfsk` records paying for: tone magnitudes are
    non-negative, so every channel carries a large common component, and
    correlating raw magnitudes scores that common part against itself.
    Pure noise scored 0.73 against a 0.70 threshold. Centred, the channels
    sum to zero at every instant and noise scores near nothing.
    """
    step = bank.symbol_samples // SEARCH_DIVISOR if step is None else step
    magnitudes, rms = _magnitude_grid(bank, audio, step)
    pattern = np.asarray(pattern, dtype=np.int64)
    per_symbol = bank.symbol_samples // step
    count = len(magnitudes) - (len(pattern) - 1) * per_symbol
    if count <= 0:
        return np.zeros(0), step
    centred = magnitudes - magnitudes.mean(axis=1, keepdims=True)
    hit = np.zeros(count)
    total = np.zeros(count)
    for i, tone in enumerate(pattern):
        window = centred[i * per_symbol:i * per_symbol + count]
        hit += window[:, tone]
        total += np.sum(np.abs(window), axis=1)
    scores = hit / np.maximum(total, 1e-30)
    if len(rms) and np.max(rms) > 0.0:
        quiet = rms[:count] < np.max(rms) * rms_floor_fraction
        scores[quiet] = 0.0
    return scores, step


def matched_energy(bank: ToneBank, audio: np.ndarray, pattern: np.ndarray,
                   start: int) -> float:
    """Total magnitude in the tones the pattern says were sent."""
    values = analyze(bank, audio, start, len(pattern))
    if values is None:
        return -np.inf
    pattern = np.asarray(pattern, dtype=np.int64)
    return float(np.sum(np.abs(values)[np.arange(len(pattern)), pattern]))


def pattern_score(bank: ToneBank, audio: np.ndarray, pattern: np.ndarray,
                  start: int) -> float:
    """`correlate`'s statistic at one exact start."""
    values = analyze(bank, audio, start, len(pattern))
    if values is None:
        return 0.0
    pattern = np.asarray(pattern, dtype=np.int64)
    centred = np.abs(values)
    centred = centred - centred.mean(axis=1, keepdims=True)
    total = float(np.sum(np.abs(centred)))
    return float(np.sum(centred[np.arange(len(pattern)), pattern])
                 / max(total, 1e-30))


def refine(bank: ToneBank, audio: np.ndarray, pattern: np.ndarray, start: int,
           radius: int, step: int = 8) -> int:
    """Walk `start` in by `step` samples over +-`radius`, most energy wins.

    Deliberately *not* scored by `pattern_score`.  That statistic saturates:
    once the right tone dominates every symbol it reads 0.5 whatever the
    timing, and measured on a clean frame it is flat to within 1e-9 across
    +-48 samples -- so refining on it picks an arbitrary point in the plateau
    and lands the payload window up to a tenth of a symbol out.  Matched
    tone *energy* has an actual maximum at the true boundary, because
    timing error is exactly what leaks a symbol's energy into its
    neighbour, and it still finds it 16 dB below the payload's own limit.

    `correlate` searches on a coarse grid to keep the cost of an empty poll
    down; this pays for the resolution only where something was found.
    """
    candidates = range(start - radius, start + radius + 1, step)
    return max(candidates,
               key=lambda at: matched_energy(bank, audio, pattern, at))


def repeated_pairs(pattern: np.ndarray) -> np.ndarray:
    """Indices `i` where `pattern[i]` and `pattern[i+1]` are the same tone."""
    pattern = np.asarray(pattern, dtype=np.int64)
    return np.flatnonzero(pattern[:-1] == pattern[1:])


def offset_hz(bank: ToneBank, audio: np.ndarray, start: int,
              pattern: np.ndarray) -> float:
    """Carrier offset in Hz, from the phase step across *repeated* tones.

    A carrier offset advances every symbol's phase by the same amount, so
    measuring one symbol's complex amplitude against the next recovers it --
    `whale.dsp.freq.fine_offset_hz`'s idea, applied to a tone sequence
    instead of an OFDM header.

    Only consecutive symbols carrying the *same* tone are used, and that is
    the whole reason a mode's preamble is worth building out of repeated
    pairs.  A symbol's measured phase also carries a symbol-timing term,
    `-2*pi*bin*delta/symbol_samples`, which differs between two different
    tones and so leaks straight into the estimate: at 48 samples of timing
    error -- well inside what the detector tolerates -- a zero offset
    measured as -13.5 Hz.  Across a repeated pair the two symbols share a
    bin, that term is identical in both and cancels exactly, and what is
    left is the offset alone, at any timing.

    Returns 0.0 for a pattern with no repeated pair, which is a mode
    declaring it does not want this measurement rather than an error.
    Unambiguous to +-`bank.offset_limit_hz`; measured better than 1 Hz at
    16 dB below the payload's own working point.
    """
    pairs = repeated_pairs(pattern)
    if not len(pairs):
        return 0.0
    values = analyze(bank, audio, start, len(pattern))
    if values is None:
        return 0.0
    pattern = np.asarray(pattern, dtype=np.int64)
    tones = values[np.arange(len(pattern)), pattern]
    # Summed before the angle is taken, so strong pairs weigh more and no
    # unwrapping is needed.
    step = np.sum(tones[pairs + 1] * np.conj(tones[pairs]))
    if step == 0:
        return 0.0
    return float(np.angle(step) * bank.sample_rate
                 / (2.0 * np.pi * bank.symbol_samples))
