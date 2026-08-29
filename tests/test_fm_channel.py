"""Complex-IQ FM path, nonlinear threshold, and measured preset validation."""

import json
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import freqz

from whale import afsk, rx_audio
from whale.fm_channel import (FM_RADIO_PRESETS, ComplexFmChannel, FmRfPath)


ROOT = Path(__file__).parents[1]
SAMPLE_RATE = 48_000


def _tone(seconds=2.0, frequency=1_000.0, amplitude=0.3):
    n = np.arange(round(seconds * SAMPLE_RATE))
    return amplitude * np.sin(2 * np.pi * frequency * n / SAMPLE_RATE)


def _tone_snr(audio, frequency=1_000.0, skip=5_000):
    samples = np.asarray(audio[skip:], dtype=np.float64)
    time = np.arange(skip, skip + len(samples)) / SAMPLE_RATE
    basis = np.column_stack((np.sin(2 * np.pi * frequency * time),
                             np.cos(2 * np.pi * frequency * time),
                             np.ones(len(time))))
    fitted = basis @ np.linalg.lstsq(basis, samples, rcond=None)[0]
    return 10 * np.log10(np.var(fitted) / np.var(samples - fitted))


def test_high_cn_fm_round_trip_recovers_audio_and_reports_rf_reference():
    audio = _tone()
    channel = ComplexFmChannel(SAMPLE_RATE, 40.0, seed=4)
    result = channel.process(audio)
    assert _tone_snr(result.audio) > 32
    assert result.measurements["realized_rf_carrier_to_noise_db"] == pytest.approx(
        40.0, abs=0.1)
    assert channel.describe()["carrier_to_noise_reference"] == \
        "complex_iq_full_nyquist"


def test_fm_threshold_emerges_below_about_ten_db_carrier_to_noise():
    audio = _tone()
    output_snr = {}
    for cn in (20, 10, 5, 0):
        output_snr[cn] = _tone_snr(
            ComplexFmChannel(SAMPLE_RATE, cn, seed=12).process(audio).audio)
    assert output_snr[20] > 13
    assert output_snr[10] > 3
    assert output_snr[5] < 0
    # The discriminator has entered threshold: another 5 dB RF loss costs
    # appreciably more than 5 dB at audio.
    assert output_snr[5] - output_snr[0] > 6


def test_rf_frequency_error_is_rejected_by_receiver_if_filter():
    audio = _tone()
    centred = ComplexFmChannel(
        SAMPLE_RATE, 15, seed=6, rf_bandwidth_hz=7_500,
        rf_frequency_error_hz=0).process(audio).audio
    near_edge = ComplexFmChannel(
        SAMPLE_RATE, 15, seed=6, rf_bandwidth_hz=7_500,
        rf_frequency_error_hz=7_500).process(audio).audio
    assert _tone_snr(centred) > _tone_snr(near_edge) + 9


def test_seeded_noise_and_all_filter_state_replay_after_reset():
    channel = ComplexFmChannel.from_preset(
        SAMPLE_RATE, "kg_uv9d_to_ic705", 15.0, seed=99)
    first = channel.process(_tone(0.5)).audio
    second = channel.process(_tone(0.5)).audio
    assert not np.array_equal(first, second)
    channel.reset()
    assert np.array_equal(channel.process(_tone(0.5)).audio, first)


def test_measured_audio_response_hits_recorded_six_and_ten_db_edges():
    preset = FM_RADIO_PRESETS["ic705_to_kg_uv9d"]
    channel = ComplexFmChannel.from_preset(SAMPLE_RATE, preset.name, 40, seed=1)
    frequencies, response = freqz(channel._audio_fir, fs=SAMPLE_RATE, worN=131_072)
    magnitude = 20 * np.log10(np.maximum(abs(response), 1e-12))
    for edge in preset.audio_band_6db_hz:
        assert np.interp(edge, frequencies, magnitude) == pytest.approx(-6, abs=0.35)
    for edge in preset.audio_band_10db_hz:
        assert np.interp(edge, frequencies, magnitude) == pytest.approx(-10, abs=0.7)


def test_directional_preset_values_match_committed_bench_measurement():
    document = json.loads((ROOT / "experiments/ofdm/results/measurements/bandwidth.json").read_text())
    mappings = {
        "ic705_to_kg_uv9d": "ic705->ht",
        "kg_uv9d_to_ic705": "ht->ic705",
    }
    for preset_name, measurement_name in mappings.items():
        measured = document["directions"][measurement_name]["middle"]
        preset = FM_RADIO_PRESETS[preset_name]
        assert preset.audio_band_6db_hz == pytest.approx(
            measured["band_6db_hz"], abs=0.05)
        assert preset.audio_band_10db_hz == pytest.approx(
            measured["band_10db_hz"], abs=0.05)


def test_preset_applies_blackout_and_directional_sample_clock():
    channel = ComplexFmChannel.from_preset(
        SAMPLE_RATE, "ic705_to_kg_uv9d", 40, seed=2)
    result = channel.process(_tone(1.0))
    assert result.measurements["muted_samples"] == 5_280
    assert len(result.audio) == 48_000  # -3.7 ppm rounds back at one second
    assert np.all(result.audio[:5_279] == 0)
    assert channel.describe()["measurement_source"].startswith("experiments/")


def test_static_rf_multipath_adds_its_differential_delay_to_capture():
    channel = ComplexFmChannel(
        SAMPLE_RATE, 30, seed=3,
        rf_paths=(FmRfPath(), FmRfPath(0.001, 0.5, np.pi / 3)))
    result = channel.process(_tone(0.25))
    assert len(result.audio) == 12_048


def test_vhf_control_mode_decodes_through_measured_fm_preset():
    payload = bytes(range(32))
    transmitted = afsk.PROFILE_300.encode(payload)
    channel = ComplexFmChannel.from_preset(
        SAMPLE_RATE, "ic705_to_kg_uv9d", 35, seed=8)
    received = channel.process(transmitted).audio
    snapshot = rx_audio.downsample(np.concatenate((
        received, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES))))
    assert afsk.PROFILE_300.decode(snapshot)["payload"] == payload


def test_invalid_preset_and_rf_configuration_are_rejected():
    with pytest.raises(ValueError, match="unknown FM radio preset"):
        ComplexFmChannel.from_preset(SAMPLE_RATE, "generic_ht", 20, seed=1)
    with pytest.raises(ValueError, match="at least one RF path"):
        ComplexFmChannel(SAMPLE_RATE, 20, seed=1, rf_paths=())
