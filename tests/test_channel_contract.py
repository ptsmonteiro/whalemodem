import json

import numpy as np
import pytest

from whale.channel import (AudioChannel, AwgnChannel, ChannelChain,
                           ClippingChannel, DelayChannel, FilterChannel,
                           FrequencyOffsetChannel, IdentityChannel,
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
