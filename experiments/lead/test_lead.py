"""Software invariants for the musical lead.  No hardware, no channel sweep.

Run:  python -m pytest experiments/lead/test_lead.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from whale import rx_audio                                     # noqa: E402
from experiments.lead import lead                              # noqa: E402


def test_notes_are_exact_bins_at_both_rates():
    """A note holds a whole number of cycles at 48 kHz and at 12 kHz.

    This is what makes the lead phase-continuous without carrying phase
    across a boundary, and what makes each note an exact FFT bin after the
    production decimation rather than something needing a window.
    """
    for note in lead.NOTE_BINS:
        for rate, samples in ((lead.TX_SAMPLE_RATE,
                               lead.NOTE_SAMPLES * lead.DECIMATION),
                              (lead.RX_SAMPLE_RATE, lead.NOTE_SAMPLES)):
            cycles = note * lead.BIN_HZ * samples / rate
            assert cycles == pytest.approx(round(cycles))


def test_scale_is_justly_tuned():
    """Every interval above the root is an exact small-integer ratio.

    Just intervals are integer ratios and FFT bins are integers, so a
    justly tuned scale on an integer root needs no rounding at all --
    which is the reason for choosing it over equal temperament, whose
    rounding onto this grid would reach 40 cents at the bottom of the band.
    """
    just = {1, 9 / 8, 5 / 4, 4 / 3, 3 / 2, 5 / 3, 15 / 8, 2, 9 / 4, 5 / 2,
            8 / 3, 3, 10 / 3, 15 / 4, 4}
    for note in lead.NOTE_BINS:
        ratio = note / lead.ROOT_BIN
        assert any(abs(ratio - r) < 1e-12 for r in just), note


def test_lead_is_constant_envelope():
    audio = lead.modulate(0, 4)
    crest = np.max(np.abs(audio)) / np.sqrt(np.mean(audio ** 2))
    assert crest == pytest.approx(np.sqrt(2.0), abs=1e-3)


def test_notes_sit_inside_an_ssb_passband():
    assert 500.0 <= min(lead.NOTE_HZ) and max(lead.NOTE_HZ) <= 2400.0


def test_alphabet_is_rotationally_unambiguous():
    """No label equals a rotation of itself or of another label.

    The first would make the position within the cycle unreadable, which is
    the leading-loss measurement; the second would make the label itself
    unreadable at an unknown phase.
    """
    for sequence in lead.ALPHABET:
        assert lead._aperiodic(sequence)
    assert lead.alphabet_min_distance(lead.ALPHABET) >= 4


def test_cadence_is_far_from_every_label():
    assert min(lead._orbit_distance(lead.CADENCE, other)
               for other in lead.ALPHABET) >= 5
    assert lead._aperiodic(lead.CADENCE)


def test_every_sequence_has_a_repeated_pair():
    """The carrier-offset estimate needs two consecutive notes on one bin."""
    for sequence in list(lead.ALPHABET) + [lead.CADENCE]:
        assert np.any(sequence[:-1] == sequence[1:])


def test_round_trip_through_the_production_receive_path():
    """Noiseless, but decimated 48 kHz to 12 kHz exactly as the radio is."""
    for label in range(len(lead.ALPHABET)):
        transmitted = np.concatenate((
            lead.modulate(label, 3, amplitude=0.5),
            np.zeros(lead.NOTE_SAMPLES * lead.DECIMATION * 8),
            np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES)))
        captured = rx_audio.downsample(transmitted.astype(np.float32))
        found = lead.detect(captured)
        assert found is not None
        assert found.label == label
        expected = 4 * lead.CYCLE_NOTES * lead.NOTE_SAMPLES
        assert abs(found.start - expected) <= lead.NOTE_SAMPLES // 2
        assert found.cycles_observed == 3
        assert abs(found.offset_hz) < 1.0


def test_phase_step_limit_is_not_the_note_gap():
    """The two frequency limits are different quantities.

    A note is four times longer than the reciprocal of its own spacing, so
    the phase-step estimator wraps well before a note would be confused
    with its neighbour.  Assuming otherwise is what made a 14 Hz offset
    read as -9.4 Hz.
    """
    assert lead.PHASE_STEP_LIMIT_HZ < lead.MIN_NOTE_GAP_HZ / 2
    assert lead.OFFSET_SEARCH_HZ > lead.PHASE_STEP_LIMIT_HZ
