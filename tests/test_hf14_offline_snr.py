import numpy as np
import pytest

from experiments.hf14_ofdm_bpsk_watterson.offline_snr import (
    add_awgn, estimate_powers, wilson)


def test_power_estimator_subtracts_off_frame_noise():
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 0.01, 4000)
    capture = noise.copy()
    capture[1200:2200] += 0.1
    result = estimate_powers(capture, frame_samples=1000, guard_samples=100)
    assert result["frame_start_sample"] == pytest.approx(1200, abs=4)
    assert result["baseline_noise_power"] == pytest.approx(0.01 ** 2, rel=0.12)
    assert result["signal_power"] == pytest.approx(0.1 ** 2, rel=0.04)


def test_awgn_is_deterministic_and_calibrated():
    capture = np.zeros(200_000, dtype=np.float32)
    first, power, realized = add_awgn(capture, signal_power=0.04,
                                      added_snr_db=10.0, seed=42)
    second, _, _ = add_awgn(capture, signal_power=0.04,
                            added_snr_db=10.0, seed=42)
    assert np.array_equal(first, second)
    assert power == pytest.approx(0.004, rel=0.015)
    assert realized == pytest.approx(10.0, abs=0.07)


def test_invalid_power_and_wilson_bounds():
    with pytest.raises(ValueError, match="positive"):
        add_awgn(np.zeros(4), signal_power=0.0, added_snr_db=10.0, seed=1)
    assert wilson(0, 0) == [0.0, 1.0]
    lo, hi = wilson(50, 50)
    assert 0.92 < lo < hi == 1.0
