"""Unit checks for the calibrated offline ht->ic705 replay channel.

Not a hardware test: this only pins the channel model's own arithmetic
(tilt direction and magnitude, K/baud-dependent noise scaling, determinism)
so a future recalibration cannot silently invert a sign or drop a term.
"""

import numpy as np
import pytest

from experiments.fm_mfsk.channel_model import (FmHtIc705Channel, apply_tilt,
                                                tilt_gain)


def test_tilt_gain_is_unity_at_band_bottom_and_matches_tilt_db_at_top():
    lo, hi = 500.0, 3000.0
    assert tilt_gain(np.array([lo]), -13.0, lo, hi)[0] == pytest.approx(1.0)
    top = tilt_gain(np.array([hi]), -13.0, lo, hi)[0]
    assert 20 * np.log10(top) == pytest.approx(-13.0, abs=1e-6)


def test_tilt_gain_clamps_outside_the_band():
    lo, hi = 500.0, 3000.0
    below = tilt_gain(np.array([100.0]), -13.0, lo, hi)[0]
    above = tilt_gain(np.array([4000.0]), -13.0, lo, hi)[0]
    assert below == pytest.approx(1.0)
    assert above == pytest.approx(10 ** (-13.0 / 20.0))


def test_apply_tilt_attenuates_a_high_tone_more_than_a_low_one():
    sample_rate = 48_000
    n = sample_rate  # 1 second, 1 Hz bin resolution
    t = np.arange(n) / sample_rate
    low_tone = np.sin(2 * np.pi * 600.0 * t)
    high_tone = np.sin(2 * np.pi * 2800.0 * t)
    low_out = apply_tilt(low_tone, sample_rate, -13.0)
    high_out = apply_tilt(high_tone, sample_rate, -13.0)
    low_power = np.mean(low_out ** 2)
    high_power = np.mean(high_out ** 2)
    assert high_power < low_power


def test_apply_tilt_is_a_no_op_at_zero_db():
    audio = np.random.default_rng(1).normal(size=4800)
    out = apply_tilt(audio, 48_000, 0.0)
    assert np.allclose(out, audio, atol=1e-5)


def test_noise_penalty_grows_with_subbands_and_is_zero_at_reference():
    channel = FmHtIc705Channel(self_noise_db_per_k_double=6.0, k_reference=4)
    assert channel.noise_penalty_db(4) == pytest.approx(0.0)
    assert channel.noise_penalty_db(8) == pytest.approx(6.0)
    assert channel.noise_penalty_db(2) == pytest.approx(-6.0)


def test_noise_penalty_grows_with_baud_and_is_zero_at_reference():
    channel = FmHtIc705Channel(isi_db_per_baud_double=10.0, baud_reference=150.0)
    assert channel.noise_penalty_db(4, baud=150.0) == pytest.approx(0.0)
    assert channel.noise_penalty_db(4, baud=300.0) == pytest.approx(10.0)


def test_process_is_deterministic_for_a_fixed_seed():
    channel = FmHtIc705Channel()
    audio = np.random.default_rng(2).normal(size=4800).astype(np.float32)
    a = channel.process(audio, 48_000, subbands=4, seed=7, baud=150.0)
    b = channel.process(audio, 48_000, subbands=4, seed=7, baud=150.0)
    assert np.array_equal(a, b)


def test_process_more_subbands_yields_lower_realized_snr():
    channel = FmHtIc705Channel()
    audio = np.ones(48_000, dtype=np.float32) * 0.1
    quiet = channel.process(audio, 48_000, subbands=2, seed=3, baud=150.0)
    loud = channel.process(audio, 48_000, subbands=8, seed=3, baud=150.0)
    quiet_noise_power = np.mean((quiet - audio) ** 2)
    loud_noise_power = np.mean((loud - audio) ** 2)
    assert loud_noise_power > quiet_noise_power
