"""Statistical and contract tests for the ITU-R F.1487 HF channel model."""

import numpy as np
import pytest

from whale.channel import (WATTERSON_PRESETS, WattersonChannel,
                           WattersonPath)


def _sample_gain(channel, path, seconds, rate=100.0):
    positions = np.arange(round(seconds * rate)) * channel.sample_rate / rate
    return channel._path_gain(path, positions)


def test_itu_presets_use_two_equal_power_paths_and_2sigma_spread():
    expected = {
        "low_latitude_quiet": (0.0005, 0.5),
        "low_latitude_moderate": (0.002, 1.5),
        "low_latitude_disturbed": (0.006, 10.0),
        "mid_latitude_quiet": (0.0005, 0.1),
        "mid_latitude_moderate": (0.001, 0.5),
        "mid_latitude_disturbed": (0.002, 1.0),
        "mid_latitude_disturbed_nvis": (0.007, 1.0),
        "high_latitude_quiet": (0.001, 0.5),
        "high_latitude_moderate": (0.003, 10.0),
        "high_latitude_disturbed": (0.007, 30.0),
    }
    assert set(WATTERSON_PRESETS) == set(expected)
    for name, (delay, spread) in expected.items():
        paths = WATTERSON_PRESETS[name].paths()
        assert [path.delay_seconds for path in paths] == [0.0, delay]
        assert [path.frequency_spread_hz for path in paths] == [spread, spread]
        assert [path.power for path in paths] == [1.0, 1.0]


def test_scatter_gain_is_zero_mean_circular_gaussian_with_unit_power():
    channel = WattersonChannel(
        48_000, (WattersonPath(0, 2.0),), seed=72, oscillators=512)
    gain = _sample_gain(channel, 0, seconds=1_000, rate=20)
    power = np.mean(np.abs(gain) ** 2)
    assert abs(np.mean(gain)) < 0.06
    assert power == pytest.approx(1.0, abs=0.06)
    assert np.var(gain.real) == pytest.approx(0.5, abs=0.05)
    assert np.var(gain.imag) == pytest.approx(0.5, abs=0.05)
    # A circular complex Gaussian has E[|g|^4] / E[|g|^2]^2 = 2, the
    # corresponding Rayleigh-envelope moment test without histogram bins.
    fourth_moment_ratio = np.mean(np.abs(gain) ** 4) / power ** 2
    assert fourth_moment_ratio == pytest.approx(2.0, abs=0.15)


def test_doppler_power_spectrum_has_requested_shift_and_2sigma_width():
    shift, spread = 3.0, 2.0
    sample_rate = 100.0
    channel = WattersonChannel(
        48_000, (WattersonPath(0, spread, doppler_shift_hz=shift),),
        seed=19, oscillators=1_024)
    gain = _sample_gain(channel, 0, seconds=400, rate=sample_rate)
    window = np.hanning(len(gain))
    spectrum = np.fft.fftshift(np.fft.fft(gain * window))
    frequencies = np.fft.fftshift(np.fft.fftfreq(len(gain), 1 / sample_rate))
    power = np.abs(spectrum) ** 2
    mean = np.sum(frequencies * power) / np.sum(power)
    sigma = np.sqrt(np.sum((frequencies - mean) ** 2 * power) / np.sum(power))
    assert mean == pytest.approx(shift, abs=0.12)
    assert sigma == pytest.approx(spread / 2, abs=0.12)


def test_paths_are_independent_and_have_equal_realized_mean_power():
    channel = WattersonChannel.from_preset(
        48_000, "high_latitude_moderate", seed=91, oscillators=512)
    first = _sample_gain(channel, 0, seconds=300, rate=100)
    second = _sample_gain(channel, 1, seconds=300, rate=100)
    correlation = abs(np.mean(first * np.conj(second))) / np.sqrt(
        np.mean(abs(first) ** 2) * np.mean(abs(second) ** 2))
    assert correlation < 0.08
    assert np.mean(abs(first) ** 2) == pytest.approx(
        np.mean(abs(second) ** 2), rel=0.12)


def test_processing_applies_multipath_delay_and_reset_replays_realization():
    channel = WattersonChannel.from_preset(
        48_000, "mid_latitude_disturbed", seed=5)
    time = np.arange(48_000) / 48_000
    audio = np.sin(2 * np.pi * 1_500 * time).astype(np.float32)
    first = channel.process(audio)
    second = channel.process(audio)
    assert len(first.audio) == len(audio) + 96
    assert not np.array_equal(first.audio, second.audio)
    assert first.measurements["paths"] == 2
    channel.reset()
    replay = channel.process(audio)
    assert np.array_equal(first.audio, replay.audio)
    assert channel.describe()["frequency_spread_convention"] == "2_sigma"


def test_invalid_watterson_configuration_is_rejected():
    with pytest.raises(ValueError, match="spread"):
        WattersonPath(0, 0)
    with pytest.raises(ValueError, match="unknown Watterson preset"):
        WattersonChannel.from_preset(48_000, "oceanic_typo", seed=1)
    with pytest.raises(ValueError, match="too low"):
        WattersonChannel(48_000, (WattersonPath(0, 30),), seed=1,
                          fading_sample_rate=20)
