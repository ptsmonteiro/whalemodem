"""Software tests for the fixed 2400 bit/s 4-FSK experiment.

Run: ``python -m pytest experiments/mfsk/test_fsk2400.py``
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fsk2400
import mfsk


def test_profile_matches_the_wire_spec():
    profile = fsk2400.PROFILE
    assert profile.m == 4
    assert profile.symbol_rate == 1200
    assert profile.raw_bitrate == 2400
    assert profile.tones.tolist() == [800, 1200, 1600, 2000]
    assert mfsk._value_for_tone(4).tolist() == [0b00, 0b01, 0b11, 0b10]
    assert profile.spacing_ratio == pytest.approx(1 / 3)


@pytest.mark.parametrize("sample_rate", fsk2400.SAMPLE_RATES)
@pytest.mark.parametrize("length", [0, 1, 31, 257])
def test_clean_roundtrip_at_recommended_sample_rates(sample_rate, length):
    payload = np.random.default_rng(length + sample_rate).integers(
        0, 256, length, dtype=np.uint8
    ).tobytes()
    audio = fsk2400.modulate(payload, fsk2400.PROFILE, sample_rate=sample_rate)
    result = fsk2400.demodulate(audio, fsk2400.PROFILE, sample_rate=sample_rate)
    assert result.get("payload") == payload
    assert result["confidence"] > 0.9


def test_phase_is_continuous_across_symbol_boundaries():
    profile = fsk2400.PROFILE
    symbols = np.array([0, 3, 1, 2, 0], dtype=np.int64)
    sample_rate = 9600
    actual = mfsk._cpfsk(symbols, profile, sample_rate)
    frequencies = profile.tones[symbols]
    instantaneous = np.repeat(frequencies, profile.samples_per_symbol(sample_rate))
    expected = np.cos(2 * np.pi * np.cumsum(instantaneous) / sample_rate)
    np.testing.assert_allclose(actual, expected, atol=1e-12)


def test_recommended_sample_rates_have_exact_symbol_windows():
    assert [fsk2400.PROFILE.samples_per_symbol(rate)
            for rate in fsk2400.SAMPLE_RATES] == [8, 40]
