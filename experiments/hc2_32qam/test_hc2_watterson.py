"""Fast deterministic tests for the HC2 Watterson boundary-sweep harness.

These check the measurement harness, not HC2's fading boundary: the campaign
itself is a long-running command, not a pytest.  In particular the CRC
false-accept counter and the EVM-trigger detection rate are the two figures
milestone 4 reports as safety properties, so both are exercised against
synthetic trials whose answers are known by construction.
"""

import math

import numpy as np
import pytest

from whale.channel import WATTERSON_PRESETS, WattersonPath
from whale.qualification import trial_seed
from whale.trials import TrialOutcome, TrialResult

from experiments.hc2_32qam import benchmark_hc2_watterson as bench
from experiments.hc2_32qam import hc2_32qam as hc2


PAD = 4_096
FAST = dict(max_frequency_offset_hz=2.0, acquisition_step_hz=2.0)
# A deliberately gentle point: a delay far inside the cyclic prefix and a
# spread whose coherence time is orders of magnitude longer than the 2.928 s
# frame, so the harness is exercised on a frame that actually decodes.
BENIGN = bench.ChannelPoint(delay_seconds=0.0001, spread_hz=0.0005,
                            snr_db=30.0)


def _trial(point=BENIGN, seed=12345, **overrides):
    kwargs = dict(point=point, seed=seed, trial=1, payload_bytes=32,
                  lead_samples=PAD, tail_samples=PAD,
                  channel_diagnostics=False, **FAST)
    kwargs.update(overrides)
    return bench.frame_trial(**kwargs)


def _fake(**metrics):
    decoded = metrics.get("payload_matched", False)
    return TrialResult(
        trial=1, direction="unit", mode_id=bench.SEED_NAMESPACE,
        mode_name=bench.MODE_NAME, payload_bytes=1,
        outcome=TrialOutcome.DECODED if decoded else TrialOutcome.PAYLOAD_FAILED,
        tx_samples=1, tx_sample_rate=48_000, rx_samples=1,
        rx_sample_rate=48_000, keyed_seconds=1.0, decoder_metrics=metrics)


def test_parametric_points_reproduce_the_preset_geometry_exactly():
    """A --delay-ms/--spread-hz point must be the same channel as the preset.

    The parametric sweep is only interpretable as an explanation of the
    preset results if it builds the identical two-path, equal-power geometry.
    """
    for name, preset in WATTERSON_PRESETS.items():
        point = bench.ChannelPoint.from_preset(name, 20.0)
        assert point.preset == name
        assert point.delay_seconds == preset.differential_delay_seconds
        assert point.spread_hz == preset.frequency_spread_hz
        assert point.paths() == preset.paths()
        equivalent = bench.ChannelPoint(preset.differential_delay_seconds,
                                        preset.frequency_spread_hz, 20.0)
        assert equivalent.paths() == preset.paths()
        assert name in point.label and "20 dB" in point.label


def test_unknown_preset_names_are_rejected_with_the_available_names():
    with pytest.raises(ValueError, match="mid_latitude_quiet"):
        bench.ChannelPoint.from_preset("nonexistent", 20.0)


def test_parametric_label_states_delay_spread_and_snr():
    label = bench.ChannelPoint(0.002, 0.25, 18.0).label
    assert "2 ms" in label and "0.25 Hz" in label and "18 dB" in label


def test_build_points_orders_presets_before_parametric_points():
    args = bench.build_parser().parse_args([
        "--presets", "mid_latitude_quiet", "mid_latitude_moderate",
        "--delay-ms", "0.5", "2", "--spread-hz", "0.01",
        "--points", "10", "20"])
    points = bench.build_points(args)
    assert len(points) == 2 * 2 + 2 * 1 * 2
    assert [point.preset for point in points[:4]] == [
        "mid_latitude_quiet", "mid_latitude_quiet",
        "mid_latitude_moderate", "mid_latitude_moderate"]
    assert [point.snr_db for point in points[:4]] == [10.0, 20.0, 10.0, 20.0]
    assert [point.preset for point in points[4:]] == [None] * 4
    assert [point.delay_seconds for point in points[4:]] == [
        0.0005, 0.0005, 0.002, 0.002]
    # The enumeration order is the seeding contract; it must be stable.
    assert bench.build_points(args) == points


def test_build_points_rejects_an_empty_point_set():
    args = bench.build_parser().parse_args(["--points", "10"])
    with pytest.raises(ValueError, match="no channel points"):
        bench.build_points(args)


def test_channel_chain_is_watterson_then_awgn_with_the_paired_noise_seed():
    point = bench.ChannelPoint(0.0005, 0.1, 12.0, "mid_latitude_quiet")
    chain = point.channel(4242, (PAD, PAD + hc2.FRAME_SAMPLES))
    watterson, awgn = chain.stages
    assert watterson.seed == 4242
    assert awgn.seed == 4242 ^ 0x5A5A
    assert watterson.preset_name == "mid_latitude_quiet"
    assert watterson.paths == (WattersonPath(0.0, 0.1),
                               WattersonPath(0.0005, 0.1))
    assert awgn.snr.reference_start == PAD
    assert awgn.snr.reference_stop == PAD + hc2.FRAME_SAMPLES


def test_a_benign_fading_point_still_delivers_and_reports_health():
    result = _trial()
    assert result.outcome is TrialOutcome.DECODED
    metrics = result.decoder_metrics
    assert metrics["payload_matched"] is True
    assert metrics["crc_ok"] is True
    assert metrics["acquired"] is True
    assert abs(metrics["start_error_samples"]) <= hc2.GUARD_SAMPLES
    assert metrics["evm_percent"] < bench.EVM_TRIGGER_PERCENT


def test_trials_are_reproducible_from_the_shared_seeding_convention():
    seed = trial_seed(20260830, bench.SEED_NAMESPACE, 2, 5)
    assert seed == trial_seed(20260830, bench.SEED_NAMESPACE, 2, 5)
    assert seed != trial_seed(20260830, bench.SEED_NAMESPACE, 3, 5)
    first, second = _trial(seed=seed), _trial(seed=seed)
    assert first.decoder_metrics == second.decoder_metrics
    assert first.channel_measurements == second.channel_measurements


def test_channel_diagnostics_measure_estimate_staleness_growing_with_spread():
    """The oracle drift metric must respond to Doppler spread, not to noise.

    HC2 estimates the channel once at the head of a 2.928 s frame, so the
    residual late in the frame is the quantity milestone 4 uses to attribute
    failures to coherence time.  A wider spread must make it worse.
    """
    slow = _trial(point=bench.ChannelPoint(0.0001, 0.0005, 40.0),
                  channel_diagnostics=True).decoder_metrics
    fast = _trial(point=bench.ChannelPoint(0.0001, 0.05, 40.0),
                  channel_diagnostics=True).decoder_metrics
    assert slow["channel_estimate_degenerate"] is False
    assert slow["residual_tail_percent"] < 5.0
    assert fast["residual_tail_percent"] > slow["residual_tail_percent"]
    # While the channel is still partly correlated across the frame, the
    # head-referenced estimate can only go staler with elapsed time.  (Once
    # the spread fully decorrelates the frame the residual saturates near
    # sqrt(2) and the head/tail ordering stops being meaningful, so the
    # monotonicity is asserted at a spread inside that regime.)
    assert fast["residual_tail_percent"] > fast["residual_head_percent"]


def test_trigger_fires_on_rejection_missing_evm_and_high_evm_only():
    assert bench._trigger_fired(_fake(equalizer_rejected=True), 10.0)
    assert bench._trigger_fired(_fake(evm_percent=None), 10.0)
    assert bench._trigger_fired(_fake(evm_percent=float("nan")), 10.0)
    assert bench._trigger_fired(_fake(evm_percent=10.01), 10.0)
    assert not bench._trigger_fired(_fake(evm_percent=10.0), 10.0)
    assert not bench._trigger_fired(_fake(evm_percent=2.0), 10.0)


def test_summary_counts_crc_false_accepts_separately_from_decode_failures():
    """A frame whose CRC passed but whose bytes are wrong is the one defect.

    Ordinary decode failure is expected beyond the boundary; delivering
    corrupt bytes as good is not, so it gets its own counter rather than
    being folded into FER.
    """
    trials = [
        _fake(crc_ok=True, payload_matched=True, evm_percent=4.0),
        _fake(crc_ok=False, payload_matched=False, evm_percent=40.0),
        _fake(crc_ok=True, payload_matched=False, evm_percent=40.0),
    ]
    row = bench.summarize_point(BENIGN, trials, 10.0)
    assert row["delivered"] == 1
    assert row["fer"] == pytest.approx(2 / 3)
    assert row["crc_false_accepts"] == 1
    assert row["crc_false_accept_wilson_95"][1] > 1 / 3
    assert row["realized_payload_bps"] == pytest.approx(
        hc2.SUSTAINED_USER_BIT_RATE / 3)


def test_summary_detection_rate_counts_only_frames_the_channel_broke():
    trials = [
        _fake(crc_ok=True, payload_matched=True, evm_percent=4.0),
        _fake(crc_ok=True, payload_matched=True, evm_percent=12.0),
        _fake(crc_ok=False, payload_matched=False, evm_percent=40.0),
        _fake(crc_ok=False, payload_matched=False, evm_percent=6.0),
    ]
    row = bench.summarize_point(BENIGN, trials, 10.0)
    assert row["failed_frames"] == 2
    assert row["failed_frames_flagged"] == 1
    assert row["failed_frames_silent"] == 1
    assert row["detection_rate"] == pytest.approx(0.5)
    assert row["detection_wilson_95"][0] < 0.5 < row["detection_wilson_95"][1]
    assert row["delivered_frames_flagged"] == 1
    assert row["false_alarm_rate"] == pytest.approx(0.5)


def test_summary_names_the_retired_mis_acquisition_class():
    assert bench.MIS_ACQUISITION_START_ERROR == hc2.SYMBOL_SAMPLES
    trials = [
        _fake(start_error_samples=hc2.SYMBOL_SAMPLES, cfo_hz=0.1),
        _fake(start_error_samples=-3, cfo_hz=0.2),
        _fake(start_error_samples=900, cfo_hz=-0.4),
    ]
    row = bench.summarize_point(BENIGN, trials, 10.0)
    assert row["start_error_mis_acquisitions"] == 1
    assert row["start_error_beyond_cyclic_prefix"] == 2
    assert row["start_error_abs_max"] == hc2.SYMBOL_SAMPLES
    assert row["cfo_abs_max_hz"] == pytest.approx(0.4)


def test_summary_with_no_failures_reports_no_detection_rate():
    row = bench.summarize_point(
        BENIGN, [_fake(crc_ok=True, payload_matched=True, evm_percent=3.0)],
        10.0)
    assert row["failed_frames"] == 0
    assert row["detection_rate"] is None
    assert row["detection_wilson_95"] is None
    assert row["false_alarm_rate"] == 0.0


def test_tiny_smoke_sweep_produces_a_complete_artifact():
    args = bench.build_parser().parse_args([
        "--trials", "1", "--points", "30",
        "--presets", "mid_latitude_disturbed",
        "--delay-ms", "0.1", "--spread-hz", "0.0005",
        "--payload-bytes", "32", "--lead-samples", str(PAD),
        "--tail-samples", str(PAD), "--max-frequency-offset-hz", "2",
        "--acquisition-step-hz", "2", "--quiet"])
    args.out = None
    artifact = bench.run(args)

    assert artifact["schema"] == "whalemodem.hc2-watterson-boundary.v1"
    assert artifact["qualification_evidence"] is False
    assert artifact["capture"]["frame_samples"] == hc2.FRAME_SAMPLES
    assert artifact["channel"]["noise_seed"] == "trial seed ^ 0x5A5A"
    assert artifact["integrity"]["trials"] == 2
    assert artifact["integrity"]["crc_false_accepts"] == 0
    assert len(artifact["trials"]) == 2

    disturbed, benign = artifact["summaries"]
    assert disturbed["preset"] == "mid_latitude_disturbed"
    assert disturbed["frequency_spread_hz"] == 1.0
    assert benign["preset"] is None
    assert benign["delivered"] == 1
    assert benign["realized_payload_bps"] == pytest.approx(
        hc2.SUSTAINED_USER_BIT_RATE)
    assert benign["fer"] == 0.0
    assert math.isclose(benign["cyclic_prefix_ms"], 2.6667, rel_tol=1e-3)
    # The SNR reference must be the transmitted frame span, never the padding.
    reference = artifact["trials"][0]["channel_measurements"]["stage_1"][
        "reference_samples"]
    assert reference == [PAD, PAD + hc2.FRAME_SAMPLES]


def test_cli_requires_delay_and_spread_together():
    parser = bench.build_parser()
    args = parser.parse_args(["--delay-ms", "1"])
    assert bool(args.delay_ms) != bool(args.spread_hz)
    args = parser.parse_args(["--delay-ms", "1", "--spread-hz", "0.1"])
    assert bool(args.delay_ms) == bool(args.spread_hz)
