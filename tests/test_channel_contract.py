import json

import numpy as np
import pytest

from whale.channel import (AudioChannel, AwgnChannel, ChannelChain,
                           ClippingChannel, DelayChannel, FilterChannel,
                           FrequencyOffsetChannel, IdentityChannel,
                           GainChannel, ImpulseNoiseChannel,
                           NarrowbandInterference,
                           NarrowbandInterferenceChannel, NotchChannel,
                           SampleClockChannel, SnrKind, SnrSpec,
                           waveform_power)
from whale.trials import (TrialOutcome, TrialResult, TrialRun,
                          classify_decode)


def test_identity_channel_satisfies_contract_and_does_not_alias_input():
    channel = IdentityChannel(48_000)
    original = np.array([0.25, -0.5, 0.75], dtype=np.float64)
    result = channel.process(original)
    assert isinstance(channel, AudioChannel)
    assert result.audio.dtype == np.float32
    assert np.allclose(result.audio, original)
    result.audio[0] = 0
    assert original[0] == 0.25
    assert channel.describe() == {"type": "identity", "sample_rate": 48_000}


def test_waveform_snr_reference_interval_has_explicit_power():
    audio = np.array([0.0, 1.0, -1.0, 0.0])
    spec = SnrSpec(7.0, reference_start=1, reference_stop=3)
    assert waveform_power(audio, spec) == 1.0
    assert spec.kind is SnrKind.WAVEFORM


def test_snr_kinds_require_their_defining_metadata():
    with pytest.raises(ValueError, match="band_hz"):
        SnrSpec(5.0, SnrKind.IN_BAND)
    with pytest.raises(ValueError, match="bit_rate"):
        SnrSpec(5.0, SnrKind.EB_N0)
    with pytest.raises(ValueError, match="outside"):
        waveform_power(np.ones(4), SnrSpec(5.0, reference_stop=5))


def test_trial_run_serializes_enum_and_summary():
    trial = TrialResult(
        trial=1, direction="A->B", mode_id=5, mode_name="hc0",
        payload_bytes=54, outcome=TrialOutcome.DECODED,
        tx_samples=162_240, tx_sample_rate=48_000,
        rx_samples=40_560, rx_sample_rate=12_000,
        keyed_seconds=3.38, channel_measurements={"waveform_snr_db": -12.0},
        decoder_metrics={"tone_snr_db": np.float64(4.5),
                         "carriers": np.array([4.0, -np.inf])})
    document = TrialRun(channel={"type": "identity"}, trials=[trial], seed=7).to_dict()
    assert document["schema_version"] == 2
    assert document["trials"][0]["outcome"] == "decoded"
    assert document["trials"][0]["decoded"] is True
    assert document["summary"] == {"passed": 1, "total": 1}
    assert document["trials"][0]["decoder_metrics"]["carriers"] == [4.0, None]
    json.dumps(document, allow_nan=False)


def test_decode_classification_separates_acquisition_and_payload_failures():
    expected = b"payload"
    assert classify_decode({"payload": expected}, expected, 0.7) is TrialOutcome.DECODED
    assert classify_decode({"payload": None, "confidence": 0.4}, expected, 0.7) \
        is TrialOutcome.ACQUISITION_FAILED
    assert classify_decode({"payload": None, "confidence": 0.8}, expected, 0.7) \
        is TrialOutcome.PAYLOAD_FAILED


def test_awgn_has_requested_power_and_reset_replays_exact_realization():
    audio = np.ones(100_000, dtype=np.float32) * 0.25
    channel = AwgnChannel(48_000, SnrSpec(10.0), seed=123)
    first = channel.process(audio)
    assert first.measurements["realized_waveform_snr_db"] == pytest.approx(10.0, abs=0.08)
    channel.reset()
    second = channel.process(audio)
    assert np.array_equal(first.audio, second.audio)
    assert channel.describe()["seed"] == 123


def test_frequency_offset_and_drift_continue_between_calls_then_reset():
    rate = 48_000
    duration = 0.25
    time = np.arange(round(rate * duration)) / rate
    tone = np.cos(2 * np.pi * 1_000 * time)
    channel = FrequencyOffsetChannel(rate, 20.0, drift_hz_per_second=4.0)
    first = channel.process(tone)
    second = channel.process(tone)
    assert first.measurements["frequency_offset_start_hz"] == 20.0
    assert first.measurements["frequency_offset_stop_hz"] == 21.0
    assert second.measurements["frequency_offset_start_hz"] == 21.0
    spectrum = np.fft.rfft(first.audio * np.hanning(len(first.audio)))
    peak_hz = np.fft.rfftfreq(len(first.audio), 1 / rate)[np.argmax(abs(spectrum))]
    assert peak_hz == pytest.approx(1_020, abs=4)
    channel.reset()
    assert np.allclose(channel.process(tone).audio, first.audio)


def test_delay_and_clipping_report_what_they_changed():
    delayed = DelayChannel(1_000, 0.003).process(np.array([2.0, -2.0]))
    assert np.array_equal(delayed.audio, [0, 0, 0, 2, -2])
    clipped = ClippingChannel(1_000, 0.5).process(delayed.audio)
    assert np.array_equal(clipped.audio, [0, 0, 0, 0.5, -0.5])
    assert clipped.measurements["clipped_samples"] == 2


def test_bandpass_filter_rejects_out_of_band_tone_and_reset_is_repeatable():
    rate = 12_000
    time = np.arange(rate) / rate
    low = np.sin(2 * np.pi * 1_000 * time)
    high = np.sin(2 * np.pi * 4_000 * time)
    channel = FilterChannel(rate, low_hz=700, high_hz=2_400, order=6)
    passed = channel.process(low).audio
    channel.reset()
    rejected = channel.process(high).audio
    assert np.sqrt(np.mean(passed[1000:] ** 2)) > 100 * np.sqrt(
        np.mean(rejected[1000:] ** 2))
    channel.reset()
    assert np.array_equal(channel.process(low).audio, passed)


def test_sample_clock_error_changes_length_with_documented_sign():
    audio = np.arange(100_000, dtype=np.float32)
    fast = SampleClockChannel(48_000, 100.0).process(audio)
    slow = SampleClockChannel(48_000, -100.0).process(audio)
    assert len(fast.audio) == 100_010
    assert len(slow.audio) == 99_990


def test_chain_preserves_stage_order_and_namespaces_measurements():
    chain = ChannelChain((DelayChannel(1_000, 0.002),
                          ClippingChannel(1_000, 0.5)))
    result = chain.process(np.array([1.0], dtype=np.float32))
    assert np.array_equal(result.audio, [0, 0, 0.5])
    assert result.measurements["stage_0"]["delay_samples"] == 2
    assert result.measurements["stage_1"]["clipped_samples"] == 1
    assert [stage["type"] for stage in chain.describe()["stages"]] == [
        "delay", "clipping"]


def test_gain_accepts_db_and_reports_actual_levels():
    result = GainChannel(8_000, gain_db=-6.020599913).process(np.ones(32))
    assert result.audio == pytest.approx(0.5)
    assert result.measurements["input_power"] == 1.0
    assert result.measurements["output_power"] == pytest.approx(0.25)


def test_impulses_replay_and_continue_across_process_boundaries():
    channel = ImpulseNoiseChannel(1_000, 100, .01, 2, seed=4,
                                  burst_shape="hann")
    first_parts = [channel.process(np.zeros(73)).audio,
                   channel.process(np.zeros(127)).audio]
    joined = np.concatenate(first_parts)
    assert np.count_nonzero(joined) > 0
    channel.reset()
    assert np.array_equal(channel.process(np.zeros(200)).audio, joined)
    assert channel.describe()["seed"] == 4


def test_tone_interference_has_requested_relative_power_drift_and_duty():
    source = NarrowbandInterference(
        1_000, -10, power_reference="relative", drift_hz_per_second=10,
        duty_cycle=.5)
    channel = NarrowbandInterferenceChannel(
        8_000, [source], seed=8, duty_period_seconds=1)
    result = channel.process(np.ones(8_000))
    assert result.measurements["sources"][0]["active_fraction"] == .5
    assert result.measurements["injected_power"] == pytest.approx(.05, rel=.02)
    assert result.measurements["sources"][0]["frequency_stop_hz"] == 1_010
    channel.reset()
    assert np.array_equal(channel.process(np.ones(8_000)).audio, result.audio)


def test_narrow_noise_is_seeded_and_reports_realized_power():
    source = NarrowbandInterference(1_500, -20, kind="noise", width_hz=100)
    channel = NarrowbandInterferenceChannel(8_000, [source], seed=19)
    result = channel.process(np.zeros(16_000))
    assert result.measurements["injected_power"] == pytest.approx(.01, rel=.08)
    channel.reset()
    assert np.array_equal(channel.process(np.zeros(16_000)).audio, result.audio)


def test_notch_rejects_center_and_tracks_drift_across_calls():
    rate = 8_000
    time = np.arange(rate) / rate
    tone = np.sin(2 * np.pi * 1_000 * time)
    notch = NotchChannel(rate, 1_000, 100, 40, drift_hz_per_second=5)
    first = notch.process(tone)
    assert np.sqrt(np.mean(first.audio[2_000:] ** 2)) < .06
    second = notch.process(tone)
    assert second.measurements["center_start_hz"] == 1_005
    notch.reset()
    assert np.array_equal(notch.process(tone).audio, first.audio)
