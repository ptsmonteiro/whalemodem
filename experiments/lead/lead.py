"""A musical lead: one head that burns through, labels the frame, and times it.

Today every mode sends a throwaway head -- one block repeated until the
requested guard is filled -- purely so the receiving station's squelch and
ALC have something to destroy other than the frame.  `whale/dsp/head.py`
then counts identical blocks backwards to learn how much died, and
`ADAPTIVE_TIMING.md` feeds that back into the next transmission.  The audio
carries no information at all, and because every block is identical the
measurement is ambiguous in exactly the way that document records under
"weak-signal ambiguity".

This experiment replaces that head with a repeating *arpeggio* whose note
sequence names the frame behind it.  One mechanism for all modes:

  * it burns through, because it is one tone at a time -- constant
    envelope, and a hard-clipped sine keeps its fundamental;
  * it says what is coming, so the decoder does not infer the mode from
    protocol state;
  * it is self-indexing, so the surviving fragment gives both the leading
    loss and a coarse frame start, in one measurement rather than two.

Geometry lives on its own grid, unrelated to any mode's payload grid.

  Notes are *just intonation on integer FFT bins*.  This is not decoration.
  Bins are integers, just intervals are small-integer ratios, so a justly
  tuned scale built on bin 24 lands exactly on bins with no tuning error at
  all -- 24, 27, 30, 32, 36, 40, 45, 48 is a just major scale, and doubling
  it walks octaves.  Equal temperament would have to be rounded onto the
  bin grid, and at the bottom of the band that rounding is 40 cents.

  Everything is sized on the **12 kHz decode rate**, not the 48 kHz
  transmit rate: production receive audio is decimated by
  `whale/rx_audio.py` before any decoder sees it, so 12 kHz is what sets
  the achievable frequency resolution.  A 512-sample note at 12 kHz is
  23.4375 Hz of resolution and 42.67 ms of duration; the transmitter emits
  the same note as 2048 samples at 48 kHz, the same bin index, a whole
  number of cycles either way.

The lead is `cycles` repeats of an `L`-note arpeggio, ending exactly at the
frame's first sample.  The detector hypothesises a frame start and a label
and scores the notes immediately before it, so one search returns both.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np

TX_SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = 12_000
DECIMATION = TX_SAMPLE_RATE // RX_SAMPLE_RATE

#: 512 samples at 12 kHz: 42.67 ms, 23.4375 Hz per bin.  Four times an HC0
#: symbol, which buys 6 dB of per-note processing gain -- the lead has to
#: work where the *weakest* mode works, and unlike a payload symbol it is
#: not competing with a bit rate.
NOTE_SAMPLES = 512
BIN_HZ = RX_SAMPLE_RATE / NOTE_SAMPLES
NOTE_SECONDS = NOTE_SAMPLES / RX_SAMPLE_RATE

#: A just major scale on bin 24 (562.5 Hz), carried up two octaves to bin
#: 96 (2250 Hz).  Every entry is an exact bin, and every interval is an
#: exact just ratio, because those are the same requirement.
ROOT_BIN = 24
_SCALE = (1, 9 / 8, 5 / 4, 4 / 3, 3 / 2, 5 / 3, 15 / 8)
NOTE_BINS = tuple(sorted(
    {int(round(ROOT_BIN * r * (2 ** octave)))
     for octave in range(2) for r in _SCALE} | {ROOT_BIN * 4}))
NOTE_HZ = tuple(b * BIN_HZ for b in NOTE_BINS)
NOTE_COUNT = len(NOTE_BINS)

#: Notes per arpeggio cycle.  Eight is one bar of quavers and 341 ms.
CYCLE_NOTES = 8

#: A note's phase advances by the offset times its own duration, so the
#: phase-step estimate wraps at half the *note rate* -- 11.7 Hz, not half
#: the narrowest note gap (23.4 Hz), which is a different and larger
#: number.  Conflating the two is a real trap: notes here are four times
#: longer than their spacing's reciprocal would make them, so the two
#: limits are not the same quantity, and the bench's 8 Hz plus half a ppm
#: each way at 14 MHz (14 Hz) lands between them.
PHASE_STEP_LIMIT_HZ = 0.5 * BIN_HZ

#: The narrowest gap in the scale, bin 30 to 32: a just semitone.  Beyond
#: half of this a note is read as its neighbour whatever the estimator does.
MIN_NOTE_GAP_HZ = BIN_HZ * min(
    b - a for a, b in zip(NOTE_BINS, NOTE_BINS[1:]))

#: How far the detector searches for the carrier offset.  Both faults the
#: offset causes -- energy scalloping out of the analysis bin, and the
#: phase-step estimate wrapping -- are cured by searching rather than by
#: tolerating, so this is set by what two SSB radios actually do (about
#: 8 Hz on the bench pair, 14 Hz for half a ppm each way at 14 MHz) with
#: room to spare, not by anything about the grid.
OFFSET_SEARCH_HZ = 25.0

#: Zero-padding factor for the analysis.  A note is a pure tone, so its
#: DFT evaluated *at its own frequency* is full-amplitude whether or not
#: that frequency is an integer bin -- the loss comes from evaluating
#: somewhere else.  Padding by four puts a grid point every 5.86 Hz, so
#: the worst mismatch is 2.9 Hz and the scalloping loss is under 0.1 dB,
#: for one longer FFT per window and no change to the transmitted signal.
PAD = 4
PAD_HZ = BIN_HZ / PAD


def note_audio(bin_index: int, sample_rate: int) -> np.ndarray:
    """One note, an exact whole number of cycles at either rate."""
    samples = NOTE_SAMPLES * (DECIMATION if sample_rate == TX_SAMPLE_RATE else 1)
    phase = 2.0 * np.pi * np.arange(samples) / samples
    return np.cos(bin_index * phase)


# -- the alphabet ---------------------------------------------------------
#
# A label is a cyclic sequence of note indices.  Three properties are
# required and one is wanted:
#
#   * **Aperiodic under rotation.**  A sequence equal to one of its own
#     rotations could not tell the receiver *which* note of the cycle it
#     caught, and that index is the leading-loss measurement.
#   * **Far from every rotation of every other sequence.**  The receiver
#     compares a fragment against all labels at all phases at once, so the
#     distance that matters is over the whole rotation orbit, not between
#     the sequences as written.
#   * **Distinct multiset of notes.**  Two sequences using the same notes
#     in a different order are separated only by timing; different note
#     content survives a worse timing estimate.
#   * **One repeated adjacent note**, wanted rather than required: two
#     consecutive symbols on the same bin share their symbol-timing phase
#     term, so it cancels and their phase step is the carrier offset alone.
#     This is `whale/dsp/mfsk.py`'s `offset_hz` trick, and the reason HC0's
#     preamble is twelve tones each sent twice.

def _rotations(sequence: np.ndarray) -> np.ndarray:
    length = len(sequence)
    index = (np.arange(length)[None, :] + np.arange(length)[:, None]) % length
    return sequence[index]


def _aperiodic(sequence: np.ndarray) -> bool:
    return not any(np.array_equal(sequence, np.roll(sequence, shift))
                   for shift in range(1, len(sequence)))


def _orbit_distance(a: np.ndarray, b: np.ndarray) -> int:
    """Smallest Hamming distance between `a` and any rotation of `b`."""
    return int(np.min(np.sum(_rotations(b) != a[None, :], axis=1)))


def build_alphabet(size: int, *, cycle: int = CYCLE_NOTES, seed: int = 0x5EED,
                   attempts: int = 200_000) -> np.ndarray:
    """`size` cyclic sequences, greedily spread apart under rotation."""
    rng = np.random.default_rng(seed)
    chosen: list[np.ndarray] = []
    best_min = 0
    for _ in range(attempts):
        candidate = rng.integers(0, NOTE_COUNT, cycle)
        # Force exactly one repeated adjacent pair, at a random place.
        candidate[1:] = np.where(candidate[1:] == candidate[:-1],
                                 (candidate[1:] + 1) % NOTE_COUNT,
                                 candidate[1:])
        at = int(rng.integers(0, cycle - 1))
        candidate[at + 1] = candidate[at]
        if not _aperiodic(candidate):
            continue
        if any(np.array_equal(np.sort(candidate), np.sort(other))
               for other in chosen):
            continue
        distance = min((_orbit_distance(candidate, other) for other in chosen),
                       default=cycle)
        if distance < best_min:
            continue
        chosen.append(candidate)
        best_min = min(best_min, distance) if len(chosen) > 1 else cycle
        if len(chosen) == size:
            return np.array(chosen)
        # Restart the distance bar for the next pick.
        best_min = 0
    raise RuntimeError("could not build an alphabet that large")


def alphabet_min_distance(alphabet: np.ndarray) -> int:
    return min(_orbit_distance(alphabet[i], alphabet[j])
               for i in range(len(alphabet)) for j in range(len(alphabet))
               if i != j)


#: Eight labels is more than the mode ladder needs (five modes today), and
#: the spare codepoints are what a sixth mode or a distinguished control
#: keying would use without redesigning the head.
ALPHABET = build_alphabet(8)


def _build_cadence(alphabet: np.ndarray, *, seed: int = 0xCADE) -> np.ndarray:
    """A fixed closing figure, far from every rotation of every label.

    The vamp cannot mark its own end.  A repeated arpeggio ends where the
    frame begins, but *detecting* that end means detecting the absence of
    further cycles -- and on a fading path the lead stopping and the
    channel fading are the same observation.  Measured on
    `mid_latitude_moderate` at -16 dB: the label was right in 120 of 120
    trials while the frame start landed one to six whole cycles early in 29
    of them, because a Watterson fade at 0.5 Hz of spread lasts one to
    three seconds, which is several cycles, and a fade over the last cycles
    is indistinguishable from the lead having stopped there.

    So the end is *marked* rather than inferred: the vamp resolves onto a
    cadence, one fixed figure shared by every label, whose correlation peak
    is where the frame starts.  It fails only when a fade covers the
    cadence itself -- which is a fade over the first moments of the frame,
    where the frame was lost anyway.  That is the right coupling; run-length
    detection had the frame start failing while the frame was still fine.
    """
    rng = np.random.default_rng(seed)
    cycle = alphabet.shape[1]
    best, best_distance = None, -1
    for _ in range(20_000):
        candidate = rng.integers(0, NOTE_COUNT, cycle)
        candidate[1:] = np.where(candidate[1:] == candidate[:-1],
                                 (candidate[1:] + 1) % NOTE_COUNT,
                                 candidate[1:])
        at = int(rng.integers(0, cycle - 1))
        candidate[at + 1] = candidate[at]
        if not _aperiodic(candidate):
            continue
        distance = min(_orbit_distance(candidate, other) for other in alphabet)
        if distance > best_distance:
            best, best_distance = candidate, distance
    return best


#: The closing figure.  Same length as a vamp cycle, so the lead stays on
#: one grid and the transmitter has nothing special to time.
CADENCE = _build_cadence(ALPHABET)


# -- transmit -------------------------------------------------------------

def modulate(label: int, cycles: int, *, alphabet: np.ndarray = ALPHABET,
             sample_rate: int = TX_SAMPLE_RATE, amplitude: float = 1.0,
             cadence: np.ndarray | None = None) -> np.ndarray:
    """The lead for one frame: `cycles` of `label`'s vamp, then the cadence.

    The vamp is what the blackout is expected to eat and what carries the
    label; the cadence is the last thing before the frame's first sample
    and is what locates it.  `cycles` counts vamp cycles only, so the lead
    is `cycles + 1` cycles long.

    Phase-continuous and constant-envelope for free -- every note holds a
    whole number of cycles, so starting each at zero phase is already
    continuous and there is nothing at a boundary to splatter.
    """
    cadence = CADENCE if cadence is None else cadence
    sequence = np.concatenate((np.tile(alphabet[label], cycles), cadence))
    table = np.stack([note_audio(b, sample_rate) for b in NOTE_BINS])
    return (amplitude * table[sequence]).reshape(-1)


# -- receive --------------------------------------------------------------

def _spectrum_grid(audio: np.ndarray, step: int
                   ) -> tuple[np.ndarray, np.ndarray]:
    """Zero-padded note-band spectra for every `step`-sample window.

    Chunked rather than built as one strided array, for the reason
    `whale/dsp/mfsk.py` gives: a ten-second buffer at a quarter-note step
    is tens of megabytes, and this runs on every decode poll on hardware
    that may not have them to spare.
    """
    audio = np.asarray(audio, dtype=np.float64)
    windows = (len(audio) - NOTE_SAMPLES) // step + 1
    if windows <= 0:
        return np.zeros((0, 0)), np.zeros(0)
    padded = NOTE_SAMPLES * PAD
    keep = padded // 2 + 1
    magnitudes = np.empty((windows, keep))
    rms = np.empty(windows)
    view = np.lib.stride_tricks.sliding_window_view(audio, NOTE_SAMPLES)
    for low in range(0, windows, 256):
        high = min(low + 256, windows)
        block = view[low * step:high * step:step]
        magnitudes[low:high] = np.abs(np.fft.rfft(block, n=padded, axis=1))
        rms[low:high] = np.sqrt(np.mean(block ** 2, axis=1))
    return magnitudes, rms


def _hypotheses(search_hz: float) -> np.ndarray:
    """Carrier-offset hypotheses, one per padded-spectrum grid point."""
    reach = int(round(search_hz / PAD_HZ))
    return np.arange(-reach, reach + 1) * PAD_HZ


@dataclass(frozen=True)
class Detection:
    label: int
    start: int          #: sample index, in the analysed audio, of the frame
    score: float
    margin: float       #: winning score less the best score of any other label
    cycles_observed: int   #: whole vamp cycles that survived the blackout
    offset_hz: float
    cadence_score: float


SEARCH_DIVISOR = 4

#: How many vamp cycles before the cadence are accumulated to read the
#: label.  Together with the cadence this sets the minimum lead --
#: `DECISION_CYCLES + 1` cycles -- which is the floor
#: `ADAPTIVE_TIMING.md`'s feedback may never shorten the head below.
DECISION_CYCLES = 1

#: How far a vamp cycle's score may fall below the winning label's own
#: level and still be counted as lead that arrived.  This now only affects
#: the leading-loss count, not the frame start -- the cadence fixes that
#: independently -- so an early stop costs a slightly conservative head,
#: not a missed frame.
RUN_FRACTION = 0.6



def _cycle_scores(spectra: np.ndarray, quiet: np.ndarray, offset: float,
                  patterns: np.ndarray, per_note: int, count: int
                  ) -> np.ndarray | None:
    """The matched-note statistic for each pattern at each position.

    Matched-note magnitude as a fraction of the total, *after the
    across-note mean is removed at each instant*.  That subtraction is not
    optional -- magnitudes are non-negative, so raw correlation scores
    their large common component against itself and reads pure noise as a
    lock, which is the failure `experiments/mfsk/RESULTS.md` records at
    0.73 against a 0.70 threshold.
    """
    columns = np.round((np.array(NOTE_BINS) * BIN_HZ + offset)
                       / PAD_HZ).astype(int)
    if columns.min() < 0 or columns.max() >= spectra.shape[1]:
        return None
    magnitudes = spectra[:, columns]
    centred = magnitudes - magnitudes.mean(axis=1, keepdims=True)
    absolute = np.sum(np.abs(centred), axis=1)
    length = patterns.shape[1]
    total = np.zeros(count)
    for i in range(length):
        total += absolute[i * per_note:i * per_note + count]
    scores = np.empty((len(patterns), count))
    for index, sequence in enumerate(patterns):
        hit = np.zeros(count)
        for i, note in enumerate(sequence):
            hit += centred[i * per_note:i * per_note + count, note]
        scores[index] = hit / np.maximum(total, 1e-30)
    scores[:, quiet] = -np.inf
    return scores


def detect(audio: np.ndarray, *, alphabet: np.ndarray = ALPHABET,
           cadence: np.ndarray | None = None,
           decision_cycles: int = DECISION_CYCLES,
           max_cycles: int = 24, step: int | None = None,
           search_hz: float = OFFSET_SEARCH_HZ) -> Detection | None:
    """Find the frame start, read the label, and estimate the carrier offset.

    Three questions, asked in the order that lets each one use the answer
    before it:

      * *What carrier offset, and where does the frame start?*  Both come
        from the cadence, in one joint argmax over (offset, position).  The
        offset is searched rather than tolerated: two SSB radios differ by
        8-14 Hz, over half a note gap here, which left uncorrected both
        scallops a note's energy out of its analysis bin *and* wraps the
        phase-step estimator.  Measured: 46% correct at -20 dB with a 14 Hz
        offset against 100% without one.  Each hypothesis is one column
        shift in a zero-padded spectrum, so the whole search costs one FFT
        per window rather than one per hypothesis.
      * *Which label?*  Read from the `decision_cycles` vamp cycles
        immediately before the cadence, whose position is now known.  Their
        scores are summed and the argmax over labels wins; `margin` is the
        gap to the runner-up, which is the quantity a threshold belongs on
        -- an absolute score says only that *something* periodic is there.
      * *How much of the lead survived?*  Count vamp cycles backward from
        the cadence while they still score.  This is what
        `whale/dsp/head.py` counts identical blocks for today, except that
        an aperiodic arpeggio's rotation is observable, so a partial cycle
        at the blackout edge is located rather than guessed.
    """
    cadence = CADENCE if cadence is None else cadence
    step = NOTE_SAMPLES // SEARCH_DIVISOR if step is None else step
    spectra, rms = _spectrum_grid(audio, step)
    if not len(spectra):
        return None
    per_note = NOTE_SAMPLES // step
    cycle = len(cadence)
    stride = cycle * per_note
    count = len(spectra) - (cycle - 1) * per_note
    if count <= 0:
        return None
    quiet = (rms[:count] < np.max(rms) * 0.05
             if len(rms) and np.max(rms) > 0.0 else np.zeros(count, bool))

    best = None
    for offset in _hypotheses(search_hz):
        scores = _cycle_scores(spectra, quiet, offset, cadence[None, :],
                               per_note, count)
        if scores is None:
            continue
        at = int(np.argmax(scores[0]))
        if best is None or scores[0, at] > best[0]:
            best = (float(scores[0, at]), at, offset)
    if best is None:
        return None
    cadence_score, at, coarse_hz = best

    labels = _cycle_scores(spectra, quiet, coarse_hz, alphabet, per_note, count)
    if labels is None:
        return None
    accumulated = np.zeros(len(alphabet))
    used = 0
    for d in range(1, decision_cycles + 1):
        position = at - d * stride
        if position < 0:
            break
        accumulated += labels[:, position]
        used += 1
    if not used:
        return None
    accumulated /= used
    label = int(np.argmax(accumulated))
    score = float(accumulated[label])
    rival = float(np.max(np.delete(accumulated, label)))

    floor = RUN_FRACTION * score
    observed = 0
    while observed < max_cycles:
        position = at - (observed + 1) * stride
        if position < 0 or labels[label, position] < floor:
            break
        observed += 1

    return Detection(
        label=label, start=at * step + cycle * NOTE_SAMPLES,
        score=score, margin=score - rival,
        cycles_observed=observed,
        offset_hz=_offset_hz(audio, at * step, cadence, coarse_hz),
        cadence_score=cadence_score)


def _offset_hz(audio: np.ndarray, start: int, sequence: np.ndarray,
               coarse_hz: float) -> float:
    """Refine the carrier offset on the repeated adjacent note.

    Two consecutive notes on the same bin share the symbol-timing phase
    term `-2*pi*bin*delta/NOTE_SAMPLES`, so it cancels between them and
    what is left is the offset alone, at any timing.  Across two
    *different* notes it does not cancel and leaks straight in --
    `whale/dsp/mfsk.py` measures 48 samples of timing error reading a zero
    offset as -13.5 Hz.  This is why every sequence in the alphabet is
    built with exactly one repeated pair.

    The step is unambiguous only to +-`PHASE_STEP_LIMIT_HZ` (11.7 Hz),
    which is *narrower* than two radios can be apart, so it is unwrapped
    against the search's coarse estimate rather than trusted alone.  The
    two together are what makes the answer both wide and precise.
    """
    pairs = np.flatnonzero(sequence[:-1] == sequence[1:])
    if not len(pairs):
        return coarse_hz
    span = len(sequence) * NOTE_SAMPLES
    if start < 0 or start + span > len(audio):
        return coarse_hz
    index = np.arange(start, start + span)
    mixed = (np.asarray(audio[start:start + span], dtype=np.float64)
             * np.exp(-2j * np.pi * coarse_hz * index / RX_SAMPLE_RATE))
    grid = mixed.reshape(len(sequence), NOTE_SAMPLES)
    spectrum = np.fft.fft(grid, axis=1)
    tones = spectrum[np.arange(len(sequence)),
                     [NOTE_BINS[n] for n in sequence]]
    step = np.sum(tones[pairs + 1] * np.conj(tones[pairs]))
    if step == 0:
        return coarse_hz
    residual = float(np.angle(step) * RX_SAMPLE_RATE
                     / (2.0 * np.pi * NOTE_SAMPLES))
    return coarse_hz + residual
