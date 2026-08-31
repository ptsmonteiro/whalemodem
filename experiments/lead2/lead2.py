"""A mode-independent lead: one figure that burns through, names the frame,
times it, and hands back the carrier offset.

Every mode today sends a throwaway head -- one block repeated until the
requested guard is filled -- purely so the receiving station's squelch and
ALC destroy that instead of the frame.  `whale/dsp/head.py` counts identical
blocks backwards to learn how much died and `ADAPTIVE_TIMING.md` feeds that
back into the next transmission.  The audio carries no information, and
because every block is identical the measurement is ambiguous in exactly the
way that document records under "weak-signal ambiguity".

`experiments/lead/` replaced that head with a repeating arpeggio plus a fixed
closing cadence and measured it at 0.68 s.  Its floor is two eight-note
cycles, because the label lives in the *vamp* and the frame start lives in
the *cadence*, and both have to arrive.  This experiment asks the obvious
next question: **why are those two different figures?**

They need not be.  A sequence long enough to be located unambiguously is
already long enough to carry three bits, because locating it costs the whole
figure while naming it costs only log2(8) of the figure's ~L*log2(M) bits.
So here there is **one figure per frame type**, ending exactly at the frame's
first sample.  One correlation bank returns the label, the frame start and
the carrier offset together, and the minimum lead is one figure rather than
two.

    | burn | burn | burn | ... | burn | figure(label) | frame
      ^ blackout eats from here          ^ correlation peak = frame start

The burn is one universal pattern -- identical for every mode and every
label -- repeated to fill whatever guard `ADAPTIVE_TIMING.md` currently asks
for.  It is what the blackout is expected to eat and counting how many whole
repeats arrived is the leading-loss measurement.  It is *not* needed to read
the label or to find the frame, which is the whole point: on a clean path,
where the feedback has driven the guard to its 10 ms minimum, the lead
collapses to one figure.

Geometry
--------

Everything is sized on the **12 kHz decode rate**.  Production receive audio
is decimated by `whale/rx_audio.py` before any decoder sees it, so 12 kHz is
what sets the achievable frequency resolution; sizing at the 48 kHz transmit
rate would be sizing against a resolution the detector never has.

The tone set is fixed across every geometry screened here: 20 tones, 93.75 Hz
apart, 562.5-2343.75 Hz, inside a 2.4 kHz SSB data filter with both skirts
clear.  93.75 Hz is the coarsest useful spacing -- it keeps the shortest note
worth trying (128 samples at 12 kHz) non-coherently orthogonal, since
orthogonality needs a spacing of at least one over the note duration.  What
*is* swept is the note duration, because that is the only free parameter that
trades airtime against per-note energy:

    note samples (12 kHz)   duration   bin resolution   8-note figure
              512            42.67 ms   23.4375 Hz        341 ms
              256            21.33 ms   46.875  Hz        171 ms
              128            10.67 ms   93.75   Hz         85 ms

A figure of the same total duration carries the same energy at every row, so
these differ only in granularity, offset precision, and how many independent
looks a fading channel gets.  `RESULTS.md` records the sweep; the answer is
256 samples by 8 notes, and the interesting part is that lengthening the
*note* buys almost nothing while adding *notes* buys a great deal.

Two consequences of a shorter note that are easy to get wrong.  A note's
phase advances by the offset times its own duration, so the phase-step
estimate wraps at half the *note rate*, while a tone is confused with its
neighbour only at half the *tone spacing*.  Those are different numbers
whenever a note is longer than the reciprocal of the spacing, which is what
produced a 25 Hz error in the previous experiment; at 128 samples they
coincide at 46.875 Hz and the trap closes by itself.  And a shorter note has
a wider bin, so a 14 Hz offset scallops less -- but the offset is searched
rather than tolerated here regardless, because searching is nearly free.

Consecutive notes are required to be at least six tone steps (562.5 Hz)
apart.  The argument was that frequency diversity should buy back the time
diversity a four-times-shorter lead gives up, since a 171 ms figure spans a
tenth of a `mid_latitude_moderate` coherence time.  **Measured, it does much
less than that**: relaxing the constraint to one step costs three points on
`mid_latitude_moderate` at -16 dB, which 150 trials barely resolves, and
nothing at all on `mid_latitude_disturbed`, whose larger delay spread is
where the argument should pay best.  It is kept because it is free and never
negative, not because it is load-bearing.  See `RESULTS.md`; what actually
makes the short lead work is that 171 ms of a 1 ms-spread path is not often
inside a deep fade, and that eight notes over twenty tones carry far more
redundancy than three bits of label need.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import numpy as np

TX_SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = 12_000
DECIMATION = TX_SAMPLE_RATE // RX_SAMPLE_RATE

#: The tone set, in Hz.  Fixed across every geometry: 20 tones 93.75 Hz apart
#: from 562.5 to 2343.75 Hz.  93.75 Hz is what keeps a 128-sample note
#: orthogonal under non-coherent detection, and 20 of them fit between the
#: skirts of a 2.4 kHz SSB data filter.
TONE_SPACING_HZ = 93.75
FIRST_TONE_HZ = 562.5
TONE_COUNT = 20
TONE_HZ = tuple(FIRST_TONE_HZ + i * TONE_SPACING_HZ for i in range(TONE_COUNT))

#: Notes in a figure.  Swept, not assumed.  Against fading this is the
#: parameter that matters and note duration is very nearly not: at 0.171 s of
#: lead, eight 256-sample notes read correctly 97.3% of the time on
#: `mid_latitude_moderate` at -16 dB while sixteen 128-sample notes -- the
#: same airtime, the same energy, twice the looks, half the energy each --
#: manage 94.0%.  Eight is where those two pressures balance.
FIGURE_NOTES = 8

#: How far the detector searches for the carrier offset.  Set by what two SSB
#: radios actually do -- about 8 Hz on the bench pair, 14 Hz for half a ppm
#: each way at 14 MHz -- with room to spare, not by anything about the grid.
#: Both faults an uncorrected offset causes, energy scalloping out of the
#: analysis bin and the phase-step estimate wrapping, are cured by searching
#: rather than by tolerating.
OFFSET_SEARCH_HZ = 25.0

#: Zero-padded analysis resolution.  A note is a pure tone, so its DFT
#: evaluated *at its own frequency* is full amplitude whether or not that
#: frequency is an integer bin; the loss comes from evaluating somewhere
#: else.  Every geometry is padded to a common 2048-point transform, which
#: puts a grid point every 5.86 Hz, so the worst mismatch is 2.9 Hz and the
#: scalloping loss is under 0.1 dB.  Each offset hypothesis is then one
#: column shift and the whole search costs one FFT per window.
PADDED_SAMPLES = 2048
PAD_HZ = RX_SAMPLE_RATE / PADDED_SAMPLES

#: Analysis windows per note.  A quarter note is the timing granularity of
#: the correlation peak before any mode's own header refines it.
SEARCH_DIVISOR = 4

#: How far a burn repeat's score may fall below the winning figure's own
#: level and still count as lead that arrived.  This affects only the
#: leading-loss count -- the frame start comes from the figure and is
#: independent of it -- so an early stop costs a conservative head, not a
#: missed frame.
RUN_FRACTION = 0.6

#: Minimum tone steps between consecutive notes, for frequency diversity
#: against a frequency-selective fade.  Six steps is 562.5 Hz, wider than the
#: coherence bandwidth of a 1 ms delay spread.
MIN_HOP_STEPS = 6


@dataclass(frozen=True)
class Geometry:
    """One lead shape.  The tone set does not vary; these two do.

    `note_samples` buys per-note energy and frequency resolution;
    `figure_notes` buys airtime and, more importantly, independent looks at a
    fading channel.  They are separated because the sweep found them to do
    entirely different things -- see `RESULTS.md`.
    """

    note_samples: int          #: at the 12 kHz decode rate
    figure_notes: int = FIGURE_NOTES

    @property
    def note_seconds(self) -> float:
        return self.note_samples / RX_SAMPLE_RATE

    @property
    def bin_hz(self) -> float:
        return RX_SAMPLE_RATE / self.note_samples

    @property
    def figure_samples(self) -> int:
        return self.figure_notes * self.note_samples

    @property
    def figure_seconds(self) -> float:
        return self.figure_samples / RX_SAMPLE_RATE

    @property
    def phase_step_limit_hz(self) -> float:
        """Where the per-note phase-step offset estimate wraps.

        Half the *note rate*, which is not half the tone spacing whenever a
        note is longer than the reciprocal of the spacing.  Conflating the
        two is what read a 14 Hz offset as -9.4 Hz in `experiments/lead/`.
        """
        return 0.5 / self.note_seconds

    def tone_cycles(self, sample_rate: int) -> np.ndarray:
        """Whole cycles per note for each tone, at either sample rate.

        Integer at both rates by construction: 93.75 Hz times 128, 256 or
        512 samples of 12 kHz audio is a whole number of cycles, and the
        transmitter emits the same note as four times as many samples of
        48 kHz audio at the same frequency.
        """
        samples = self.note_samples * (
            DECIMATION if sample_rate == TX_SAMPLE_RATE else 1)
        cycles = np.array(TONE_HZ) * samples / sample_rate
        if np.max(np.abs(cycles - np.round(cycles))) > 1e-9:
            raise ValueError(f"tones are not whole cycles at {sample_rate} Hz")
        return np.round(cycles).astype(int)


#: Eight notes of 256 samples at 12 kHz: 21.33 ms a note, a 171 ms lead.
#: This is the answer -- a quarter of `experiments/lead/`'s 0.68 s floor at
#: the same measured robustness.  `RESULTS.md` records the sweep.
DEFAULT = Geometry(note_samples=256, figure_notes=8)


def note_audio(geometry: Geometry, tone: int, sample_rate: int) -> np.ndarray:
    """One note: an exact whole number of cycles at either rate.

    Constant envelope and, because every note closes on a whole cycle,
    phase-continuous for free -- starting each note at zero phase is already
    continuous and there is nothing at a boundary to splatter.  A hard-clipped
    sine keeps its fundamental, which is why the lead is one tone at a time
    and not a chord: HC0's crest factor of 1.41 against HC1's 3.9 is worth
    about 8 dB of average power through a peak-limited transmitter.
    """
    samples = geometry.note_samples * (
        DECIMATION if sample_rate == TX_SAMPLE_RATE else 1)
    cycles = geometry.tone_cycles(sample_rate)[tone]
    return np.cos(2.0 * np.pi * cycles * np.arange(samples) / samples)


# -- the alphabet ---------------------------------------------------------
#
# A label is one figure of `FIGURE_NOTES` notes.  Unlike the previous
# experiment's vamp, it is not compared against its own rotations -- it is
# never repeated -- but against every *time shift* of the sequence actually
# on the air, which is `... burn burn figure`.  A window of the detector's
# correlator straddling the burn/figure boundary is the alias that matters,
# and it is not a rotation of anything.
#
# Four properties:
#
#   * **Low coincidence with every shifted window.**  For every label m',
#     every label m and every shift s, the number of positions where m'
#     agrees with `(burn|burn|figure_m)[s:s+L]` must be small, except at the
#     one true alignment.  This is simultaneously the timing sidelobe, the
#     label cross-talk and the burn/figure boundary marking, because they are
#     the same quantity measured at different s.
#   * **One repeated adjacent note.**  Two consecutive notes on the same tone
#     share their symbol-timing phase term, so it cancels between them and
#     their phase step is the carrier offset alone.  Across two *different*
#     notes it does not cancel and leaks straight in -- `whale/dsp/mfsk.py`
#     measures 48 samples of timing error reading a zero offset as -13.5 Hz.
#   * **Frequency diversity.**  Consecutive notes at least `MIN_HOP_STEPS`
#     apart, so a short figure spans several coherence bandwidths.  This is
#     what buys back the time diversity a 171 ms lead does not have.
#   * **Distinct tone multisets**, so two labels differ in what they contain
#     and not only in what order it arrives.


def _windows(burn: np.ndarray, figure: np.ndarray) -> np.ndarray:
    """Every length-L window of `burn burn figure`, oldest first.

    The last row is the figure itself at its true alignment; every other row
    is an alias the detector must not prefer.
    """
    full = np.concatenate((burn, burn, figure))
    length = len(figure)
    return np.stack([full[s:s + length]
                     for s in range(len(full) - length + 1)])


def _coincidence(pattern: np.ndarray, windows: np.ndarray) -> np.ndarray:
    return np.sum(windows == pattern[None, :], axis=1)


def _legal(candidate: np.ndarray) -> bool:
    """Frequency diversity, exactly one adjacent repeat, and enough tones."""
    steps = np.abs(np.diff(candidate))
    repeats = np.flatnonzero(steps == 0)
    if len(repeats) != 1:
        return False
    if np.any(steps[steps > 0] < MIN_HOP_STEPS):
        return False
    # Spread across the band, not a two-note oscillation.  A figure longer
    # than the tone set obviously cannot be all-distinct, so the bar is how
    # much of the band it visits: at least twelve of the twenty tones, which
    # is 1.1 kHz of span whatever the figure length.
    return len(np.unique(candidate)) >= min(len(candidate) - 1, 12)


def _draw(rng: np.random.Generator, length: int) -> np.ndarray | None:
    """A candidate figure satisfying `_legal`, or None if the draw failed.

    Built rather than rejected: hop far each step, then force one repeat.
    """
    out = [int(rng.integers(0, TONE_COUNT))]
    for _ in range(length - 1):
        allowed = np.flatnonzero(
            np.abs(np.arange(TONE_COUNT) - out[-1]) >= MIN_HOP_STEPS)
        out.append(int(rng.choice(allowed)))
    at = int(rng.integers(0, length - 1))
    out[at + 1] = out[at]
    candidate = np.array(out)
    return candidate if _legal(candidate) else None


def _alias_coincidence(pattern: np.ndarray, burn: np.ndarray,
                       figure: np.ndarray, *, role: str) -> int:
    """Worst agreement of `pattern` with any *wrong* window of one on-air lead.

    The lead on the air for label m is `burn ... burn figure_m`, so the
    windows a sliding correlator sees are every window of `burn burn
    figure_m`.  Which of those windows are legitimately allowed to match
    depends on what `pattern` is, and getting that wrong is not a subtle
    failure: excluding the burn's own alignments while scoring a *figure*
    lets a figure that is identical to the burn through the sieve, and the
    detector then locks onto the first burn repeat instead of the frame.

      * `role="figure"`   -- this lead's own figure.  Only the final window
        is the true alignment.
      * `role="burn"`     -- the burn.  The first two windows are true.
      * `role="foreign"`  -- any other label's figure.  Nothing is true.
    """
    counts = _coincidence(pattern, _windows(burn, figure))
    length = len(figure)
    true = {"figure": [2 * length], "burn": [0, length], "foreign": []}[role]
    return int(np.max(np.delete(counts, true)))


def build_alphabet(size: int = 8, *, length: int = FIGURE_NOTES,
                   seed: int = 0x1EAD2, attempts: int = 60_000
                   ) -> tuple[np.ndarray, np.ndarray]:
    """The burn pattern and `size` figures, chosen to minimise coincidence.

    Greedy, tightest bar first: each candidate is admitted only if it agrees
    with no wrong alignment of any already-chosen lead in more than `bar`
    of `length` notes, and no already-chosen figure agrees with any wrong
    alignment of *its* lead in more than `bar` either.  The bar is loosened
    only when a whole pass fails to fill the alphabet, so the reported
    distance is the best this construction found rather than the first that
    worked.

    The burn is drawn first and by the same rules, because it is on the air
    for most of the lead and has to survive the same clipping and the same
    fade.
    """
    rng = np.random.default_rng(seed)
    burn = None
    while burn is None:
        burn = _draw(rng, length)

    for bar in range(1, length):
        rng = np.random.default_rng(seed)
        chosen: list[np.ndarray] = []
        for _ in range(attempts):
            candidate = _draw(rng, length)
            if candidate is None:
                continue
            if any(np.array_equal(np.sort(candidate), np.sort(other))
                   for other in chosen):
                continue
            # The candidate's own lead must not alias, for the candidate or
            # for anything already chosen...
            if _alias_coincidence(candidate, burn, candidate,
                                  role="figure") > bar:
                continue
            if _alias_coincidence(burn, burn, candidate, role="burn") > bar:
                continue
            if any(_alias_coincidence(other, burn, candidate,
                                      role="foreign") > bar
                   for other in chosen):
                continue
            # ...and the candidate must not alias against anything already
            # chosen, on their leads.
            if any(_alias_coincidence(candidate, burn, other,
                                      role="foreign") > bar
                   for other in chosen):
                continue
            chosen.append(candidate)
            if len(chosen) == size:
                return burn, np.array(chosen)
    raise RuntimeError("could not build an alphabet that large")


def _worst_coincidence(burn: np.ndarray, figures) -> int:
    """Largest agreement at any wrong alignment, over the whole alphabet."""
    worst = 0
    for index, figure in enumerate(figures):
        for other, pattern in enumerate(figures):
            role = "figure" if other == index else "foreign"
            worst = max(worst, _alias_coincidence(pattern, burn, figure,
                                                  role=role))
        worst = max(worst, _alias_coincidence(burn, burn, figure, role="burn"))
    return worst


def alphabet_report(burn: np.ndarray, figures: np.ndarray) -> dict:
    """What the alphabet actually achieves, for `RESULTS.md` and the tests."""
    return {
        "labels": len(figures),
        "notes": int(figures.shape[1]),
        "tones": TONE_COUNT,
        "worst_wrong_alignment": _worst_coincidence(burn, figures),
        "min_hop_steps": int(min(
            np.min(np.abs(np.diff(f))[np.abs(np.diff(f)) > 0])
            for f in np.vstack((burn[None, :], figures)))),
    }


@lru_cache(maxsize=None)
def alphabet(length: int = FIGURE_NOTES, size: int = 8
             ) -> tuple[np.ndarray, np.ndarray]:
    """The burn pattern and the `size` figures for one figure length."""
    return build_alphabet(size, length=length)


BURN, ALPHABET = alphabet(FIGURE_NOTES)


# -- transmit -------------------------------------------------------------

def modulate(label: int, burn_repeats: int, *, geometry: Geometry = DEFAULT,
             sample_rate: int = TX_SAMPLE_RATE,
             amplitude: float = 1.0) -> np.ndarray:
    """The lead for one frame: `burn_repeats` burns, then `label`'s figure.

    The figure's last sample is the sample before the frame's first, so the
    correlation peak *is* the frame start.  `burn_repeats` is whatever guard
    `ADAPTIVE_TIMING.md` currently asks for, and may be zero.
    """
    burn, figures = alphabet(geometry.figure_notes)
    sequence = np.concatenate((np.tile(burn, burn_repeats), figures[label]))
    table = np.stack([note_audio(geometry, t, sample_rate)
                      for t in range(TONE_COUNT)])
    return (amplitude * table[sequence]).reshape(-1)


def lead_samples(burn_repeats: int, *, geometry: Geometry = DEFAULT,
                 sample_rate: int = TX_SAMPLE_RATE) -> int:
    scale = DECIMATION if sample_rate == TX_SAMPLE_RATE else 1
    return (burn_repeats + 1) * geometry.figure_samples * scale


# -- receive --------------------------------------------------------------

#: Padded-spectrum column of each tone at zero offset.  Independent of the
#: note duration, because every geometry is padded to the same transform.
TONE_COLUMNS = np.round(np.array(TONE_HZ) / PAD_HZ).astype(int)


def _spectra(audio: np.ndarray, geometry: Geometry, step: int
             ) -> tuple[np.ndarray, np.ndarray]:
    """Zero-padded tone-band spectra for every `step`-sample window.

    Chunked rather than built as one strided array, for the reason
    `whale/dsp/mfsk.py` gives: a ten-second buffer at a quarter-note step is
    tens of megabytes, and this runs on every decode poll on hardware that
    may not have them to spare.
    """
    audio = np.asarray(audio, dtype=np.float64)
    note = geometry.note_samples
    windows = (len(audio) - note) // step + 1
    if windows <= 0:
        return np.zeros((0, 0)), np.zeros(0)
    keep = PADDED_SAMPLES // 2 + 1
    magnitudes = np.empty((windows, keep))
    rms = np.empty(windows)
    view = np.lib.stride_tricks.sliding_window_view(audio, note)
    for low in range(0, windows, 256):
        high = min(low + 256, windows)
        block = view[low * step:high * step:step]
        magnitudes[low:high] = np.abs(
            np.fft.rfft(block, n=PADDED_SAMPLES, axis=1))
        rms[low:high] = np.sqrt(np.mean(block ** 2, axis=1))
    return magnitudes, rms


def _hypotheses(search_hz: float) -> np.ndarray:
    reach = int(round(search_hz / PAD_HZ))
    return np.arange(-reach, reach + 1) * PAD_HZ


@dataclass(frozen=True)
class Detection:
    label: int
    start: int              #: sample index, in the analysed audio, of the frame
    score: float            #: the matched fraction -- the false-alarm statistic
    margin: float           #: winner less the best any other label reaches
    burns_observed: int     #: whole burn repeats that survived the blackout
    offset_hz: float


def _pattern_scores(spectra: np.ndarray, quiet: np.ndarray, offset: float,
                    patterns: np.ndarray, per_note: int, count: int
                    ) -> np.ndarray | None:
    """The matched-note statistic for each pattern at each end position.

    Matched-note magnitude as a fraction of the total, *after the across-tone
    mean is removed at each instant*.  That subtraction is not optional:
    magnitudes are non-negative, so a raw correlation scores their large
    common component against itself and reads pure noise as a lock, which is
    the failure `experiments/mfsk/RESULTS.md` records at 0.73 against a 0.70
    threshold.  Removing the mean at each instant also removes whatever a
    clipping harmonic adds to every candidate tone alike.
    """
    columns = TONE_COLUMNS + int(round(offset / PAD_HZ))
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


def detect(audio: np.ndarray, *, geometry: Geometry = DEFAULT,
           max_burns: int = 48, step: int | None = None,
           search_hz: float = OFFSET_SEARCH_HZ) -> Detection | None:
    """Read the label, find the frame start, measure the offset and the loss.

    One joint argmax over (carrier offset, label, position) answers the first
    three, because there is one figure and it carries all of them.  That is
    the difference from `experiments/lead/`, which needed a vamp for the
    label and a separate cadence for the position and therefore could not be
    shorter than both.

    The fourth question -- how much of the lead survived -- is answered
    afterwards by counting burn repeats backwards from the located figure.
    Its answer cannot move the frame start, which is what
    `ADAPTIVE_TIMING.md`'s weak-signal ambiguity needs: a burn count that
    reads short because the path was weak lengthens the next head and costs
    nothing else.
    """
    burn, figures = alphabet(geometry.figure_notes)
    step = geometry.note_samples // SEARCH_DIVISOR if step is None else step
    spectra, rms = _spectra(audio, geometry, step)
    if not len(spectra):
        return None
    per_note = geometry.note_samples // step
    length = figures.shape[1]
    count = len(spectra) - (length - 1) * per_note
    if count <= 0:
        return None
    quiet = (rms[:count] < np.max(rms) * 0.05
             if len(rms) and np.max(rms) > 0.0 else np.zeros(count, bool))

    best = None
    for offset in _hypotheses(search_hz):
        scores = _pattern_scores(spectra, quiet, offset, figures,
                                 per_note, count)
        if scores is None:
            continue
        flat = int(np.argmax(scores))
        label, at = divmod(flat, count)
        if best is None or scores[label, at] > best[0]:
            best = (float(scores[label, at]), label, at, offset, scores)
    if best is None:
        return None
    score, label, at, coarse_hz, scores = best
    rival = float(np.max(np.delete(scores, label, axis=0)))

    # Leading loss: whole burn repeats before the figure, counted backwards
    # while they still score.  An aperiodic burn makes a partial repeat at
    # the blackout edge visible rather than guessed, which is what today's
    # identical-block head cannot do.
    burn_scores = _pattern_scores(spectra, quiet, coarse_hz, burn[None, :],
                                  per_note, count)
    stride = length * per_note
    floor = RUN_FRACTION * score
    observed = 0
    if burn_scores is not None:
        while observed < max_burns:
            position = at - (observed + 1) * stride
            if position < 0 or burn_scores[0, position] < floor:
                break
            observed += 1

    return Detection(
        label=label, start=at * step + geometry.figure_samples,
        score=score, margin=score - rival, burns_observed=observed,
        offset_hz=_offset_hz(audio, at * step, geometry, figures[label],
                             coarse_hz))


def _offset_hz(audio: np.ndarray, start: int, geometry: Geometry,
               sequence: np.ndarray, coarse_hz: float) -> float:
    """Refine the carrier offset on the figure's repeated adjacent note.

    Two consecutive notes on the same tone share the symbol-timing phase
    term, so it cancels between them and what is left is the offset alone, at
    any timing.  The step is unambiguous only to half the note rate, which at
    512 samples is 11.7 Hz -- narrower than two radios can be apart -- so it
    is unwrapped against the search's coarse estimate rather than trusted
    alone.  The two together are what makes the answer both wide and precise.
    """
    pairs = np.flatnonzero(sequence[:-1] == sequence[1:])
    note = geometry.note_samples
    span = len(sequence) * note
    if not len(pairs) or start < 0 or start + span > len(audio):
        return coarse_hz
    index = np.arange(start, start + span)
    mixed = (np.asarray(audio[start:start + span], dtype=np.float64)
             * np.exp(-2j * np.pi * coarse_hz * index / RX_SAMPLE_RATE))
    grid = mixed.reshape(len(sequence), note)
    spectrum = np.fft.fft(grid, axis=1)
    cycles = geometry.tone_cycles(RX_SAMPLE_RATE)
    tones = spectrum[np.arange(len(sequence)), cycles[sequence]]
    step = np.sum(tones[pairs + 1] * np.conj(tones[pairs]))
    if step == 0:
        return coarse_hz
    residual = float(np.angle(step) * RX_SAMPLE_RATE / (2.0 * np.pi * note))
    return coarse_hz + residual
