"""Software invariants for the mode-independent lead.

No channel, no hardware, no statistics: these are the properties the design
argument depends on, checked so that a later edit cannot quietly remove one.
The measured behaviour lives in `RESULTS.md` and is produced by
`screen_lead2.py`.
"""

from __future__ import annotations

import numpy as np
import pytest

from experiments.lead2 import lead2


GEOMETRIES = [lead2.Geometry(128, 8), lead2.Geometry(256, 16),
              lead2.Geometry(512, 8), lead2.DEFAULT]


# -- the grid -------------------------------------------------------------

def test_tones_sit_inside_an_ssb_data_filter():
    """Both skirts clear of a 2.4 kHz filter, roughly 500-2400 Hz of audio."""
    assert lead2.TONE_HZ[0] >= 500.0
    assert lead2.TONE_HZ[-1] <= 2400.0


@pytest.mark.parametrize("geometry", GEOMETRIES)
@pytest.mark.parametrize("rate", [lead2.TX_SAMPLE_RATE, lead2.RX_SAMPLE_RATE])
def test_every_tone_is_a_whole_number_of_cycles(geometry, rate):
    """Phase continuity and a clean DFT are the same requirement.

    A note holding a whole number of cycles starts and ends at the same
    phase, so concatenating notes is already continuous with nothing at a
    boundary to splatter, and its energy lands in one bin rather than
    leaking across the analysis.  It has to hold at both rates, because the
    transmitter emits four times as many samples of the same tone.
    """
    cycles = geometry.tone_cycles(rate)
    assert np.all(cycles > 0)
    hz = cycles * rate / (geometry.note_samples
                          * (lead2.DECIMATION if rate == lead2.TX_SAMPLE_RATE
                             else 1))
    assert np.allclose(hz, lead2.TONE_HZ)


@pytest.mark.parametrize("geometry", GEOMETRIES)
def test_tones_are_orthogonal_under_non_coherent_detection(geometry):
    """Spacing must be at least one over the note duration.

    This is what makes the shortest note worth trying: at 128 samples of
    12 kHz audio the reciprocal of the duration is exactly the 93.75 Hz
    spacing, so nothing shorter stays orthogonal on this tone set.
    """
    assert lead2.TONE_SPACING_HZ >= 1.0 / geometry.note_seconds - 1e-9


def test_the_phase_step_limit_is_not_the_tone_spacing():
    """Two different quantities, and conflating them is a known bug.

    A note's phase advances by the offset times its own duration, so the
    per-note phase-step estimate wraps at half the *note rate*; a tone is
    confused with its neighbour only at half the *tone spacing*.  They
    coincide only when a note is exactly the reciprocal of the spacing.
    `experiments/lead/RESULTS.md` records a 25 Hz error from assuming they
    were the same number.
    """
    assert lead2.Geometry(512).phase_step_limit_hz < lead2.TONE_SPACING_HZ / 2
    assert lead2.Geometry(128).phase_step_limit_hz == pytest.approx(
        lead2.TONE_SPACING_HZ / 2)


def test_the_offset_search_covers_what_two_radios_do():
    """8 Hz on the bench pair, 14 Hz for half a ppm each way at 14 MHz."""
    assert lead2.OFFSET_SEARCH_HZ >= 14.0


# -- the alphabet ---------------------------------------------------------

@pytest.mark.parametrize("length", [8, 16])
def test_no_wrong_alignment_scores_like_a_right_one(length):
    """The one distance that matters, measured over the real on-air stream.

    A sliding correlator sees every window of `burn burn figure`, so timing
    sidelobes, label cross-talk and the burn/figure boundary are all the
    same quantity read at different shifts.  Requiring it to stay under half
    the figure keeps every wrong alignment further from a label than the
    right one is.
    """
    burn, figures = lead2.alphabet(length)
    assert lead2._worst_coincidence(burn, figures) < length / 2


@pytest.mark.parametrize("length", [8, 16])
def test_every_figure_has_exactly_one_repeated_adjacent_note(length):
    """The offset refinement's whole basis.

    Two consecutive notes on the same tone share their symbol-timing phase
    term, so it cancels and their phase step is the carrier offset alone.
    Across two different notes it does not cancel and leaks straight in.
    """
    burn, figures = lead2.alphabet(length)
    for pattern in list(figures) + [burn]:
        assert int(np.sum(np.diff(pattern) == 0)) == 1


@pytest.mark.parametrize("length", [8, 16])
def test_consecutive_notes_hop_far_enough_for_frequency_diversity(length):
    """What buys back the time diversity a short lead does not have."""
    burn, figures = lead2.alphabet(length)
    for pattern in list(figures) + [burn]:
        steps = np.abs(np.diff(pattern))
        assert np.min(steps[steps > 0]) >= lead2.MIN_HOP_STEPS


def test_the_alphabet_has_the_budgeted_eight_codepoints():
    """Five modes exist today; the brief budgets eight."""
    assert len(lead2.ALPHABET) == 8


# -- the waveform ---------------------------------------------------------

@pytest.mark.parametrize("geometry", GEOMETRIES)
def test_the_lead_is_constant_envelope(geometry):
    """Crest factor 1.41, the sine's own.

    HC0's 1.41 against HC1's 3.9 is worth about 8 dB of average power
    through a peak-limited transmitter, and a hard-clipped sine keeps its
    fundamental while a clipped chord makes in-band intermodulation.  This
    is why the lead is one tone at a time.
    """
    audio = lead2.modulate(0, 3, geometry=geometry)
    crest = np.max(np.abs(audio)) / np.sqrt(np.mean(audio ** 2))
    assert crest == pytest.approx(np.sqrt(2.0), abs=0.02)


@pytest.mark.parametrize("geometry", GEOMETRIES)
@pytest.mark.parametrize("rate", [lead2.TX_SAMPLE_RATE, lead2.RX_SAMPLE_RATE])
def test_the_lead_is_phase_continuous(geometry, rate):
    """Nothing at a note boundary that is not already inside a note.

    Every note holds a whole number of cycles, so starting each at zero
    phase is already continuous; the boundary step must be no larger than
    the largest step the highest tone takes anyway.
    """
    audio = lead2.modulate(0, 2, geometry=geometry, sample_rate=rate)
    samples = geometry.note_samples * (
        lead2.DECIMATION if rate == lead2.TX_SAMPLE_RATE else 1)
    steps = np.abs(np.diff(audio))
    boundaries = steps[samples - 1::samples]
    assert np.max(boundaries) <= np.max(steps) + 1e-12


@pytest.mark.parametrize("repeats", [0, 1, 5])
def test_length_is_the_guard_plus_exactly_one_figure(repeats):
    """The floor is one figure, which is the whole point of the design.

    `experiments/lead/` cannot go below two cycles because the label lives
    in the vamp and the frame start in a separate cadence.  Merging them
    halves the floor.
    """
    audio = lead2.modulate(0, repeats)
    assert len(audio) == lead2.lead_samples(repeats)
    assert len(audio) == (repeats + 1) * lead2.DEFAULT.figure_samples \
        * lead2.DECIMATION


# -- the detector ---------------------------------------------------------

@pytest.mark.parametrize("geometry", GEOMETRIES)
@pytest.mark.parametrize("label", range(8))
def test_a_clean_lead_reads_back_exactly(geometry, label):
    """Right label, and the frame start is the sample after the figure."""
    lead = lead2.modulate(label, 2, geometry=geometry,
                          sample_rate=lead2.RX_SAMPLE_RATE)
    audio = np.concatenate((np.zeros(geometry.note_samples), lead,
                            np.zeros(4 * geometry.note_samples)))
    found = lead2.detect(audio, geometry=geometry)
    assert found is not None
    assert found.label == label
    assert found.start == geometry.note_samples + len(lead)
    assert found.burns_observed == 2


def test_the_burn_count_tracks_a_blackout():
    """The leading-loss measurement, which is what `ADAPTIVE_TIMING.md` reads.

    Today's head is one identical block repeated, so counting it backwards
    cannot say *which* repeat survived.  Here the burn is aperiodic and the
    figure marks the end, so the count is a position rather than a guess.
    """
    geometry = lead2.DEFAULT
    lead = lead2.modulate(3, 5, geometry=geometry,
                          sample_rate=lead2.RX_SAMPLE_RATE)
    for eaten in range(6):
        kept = lead[eaten * geometry.figure_samples:]
        audio = np.concatenate((np.zeros(len(lead) - len(kept)), kept,
                                np.zeros(4 * geometry.note_samples)))
        found = lead2.detect(audio, geometry=geometry)
        assert found is not None and found.label == 3
        assert found.burns_observed == 5 - eaten


def test_a_carrier_offset_is_searched_rather_than_tolerated():
    """The bench pair's own 14 Hz, read back rather than survived."""
    geometry = lead2.DEFAULT
    lead = lead2.modulate(1, 2, geometry=geometry,
                          sample_rate=lead2.RX_SAMPLE_RATE)
    index = np.arange(len(lead))
    shifted = (lead * np.cos(2 * np.pi * 14.0 * index / lead2.RX_SAMPLE_RATE)
               - np.imag(_analytic(lead))
               * np.sin(2 * np.pi * 14.0 * index / lead2.RX_SAMPLE_RATE))
    audio = np.concatenate((shifted, np.zeros(4 * geometry.note_samples)))
    found = lead2.detect(audio, geometry=geometry)
    assert found is not None and found.label == 1
    assert found.offset_hz == pytest.approx(14.0, abs=1.0)


def _analytic(real: np.ndarray) -> np.ndarray:
    spectrum = np.fft.fft(real)
    spectrum[len(real) // 2 + 1:] = 0.0
    spectrum[1:len(real) // 2] *= 2.0
    return np.fft.ifft(spectrum)


def test_the_mean_is_removed_across_tones_at_each_instant():
    """Not optional: without it pure noise reads as a lock.

    Magnitudes are non-negative, so a raw correlation scores their large
    common component against itself.  `experiments/mfsk/RESULTS.md` records
    that failure at 0.73 against a 0.70 threshold.  Flat white noise must
    therefore score near zero rather than near one.
    """
    geometry = lead2.DEFAULT
    rng = np.random.default_rng(7)
    audio = rng.normal(0.0, 1.0, 40 * geometry.note_samples)
    found = lead2.detect(audio, geometry=geometry)
    assert found is not None
    assert found.score < 0.15


def test_the_detector_prefers_a_real_lead_to_noise():
    """The score has to separate the two; a threshold lives between them."""
    geometry = lead2.DEFAULT
    rng = np.random.default_rng(11)
    lead = lead2.modulate(5, 1, geometry=geometry,
                          sample_rate=lead2.RX_SAMPLE_RATE)
    noise = rng.normal(0.0, 1.0, len(lead))
    quiet = lead2.detect(noise, geometry=geometry)
    loud = lead2.detect(np.concatenate((lead, noise)), geometry=geometry)
    assert loud is not None and quiet is not None
    assert loud.score > 3.0 * quiet.score
