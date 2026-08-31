import json

import pytest

from experiments.hr0 import benchmark


def test_seed_derivation_is_mode_independent_and_point_order_independent():
    key = benchmark.canonical_point_key(
        "watterson_canonical", "mid_latitude_disturbed", -12)
    first = benchmark.derive_seed(17, "awgn", key, 3)
    assert first == benchmark.derive_seed(17, "awgn", key, 3)
    assert first != benchmark.derive_seed(17, "watterson", key, 3)
    assert first != benchmark.derive_seed(17, "awgn", key, 4)


def test_summary_keeps_acquisition_payload_and_error_outcomes_separate():
    base = {
        "mode_selector": "hc0", "mode_name": "hc0", "mode_id": 5,
        "point_key": "awgn|-|-16", "model": "awgn", "preset": None,
        "waveform_snr_db": -16.0, "checked_wrong_payload": False,
    }
    records = [
        {**base, "outcome": "decoded", "acquired_above_threshold": True},
        {**base, "outcome": "payload_failed", "acquired_above_threshold": True},
        {**base, "outcome": "acquisition_failed",
         "acquired_above_threshold": False},
        {**base, "outcome": "error", "acquired_above_threshold": False},
    ]
    row = benchmark.summarize(records)[0]
    assert row["outcomes"] == {
        "decoded": 1, "acquisition_failed": 1,
        "payload_failed": 1, "error": 1,
    }
    assert row["acquisition_probability"]["rate"] == .5
    assert row["payload_failure_conditional_on_acquisition"]["rate"] == .5
    assert row["frame_error_rate"]["rate"] == .75
    assert row["exploration_only"] is True


def test_wilson_intervals_cover_extreme_observations():
    assert benchmark.wilson_interval(0, 30) == pytest.approx([0.0, .1135], abs=.001)
    assert benchmark.wilson_interval(30, 30) == pytest.approx([.8865, 1.0], abs=.001)


@pytest.mark.parametrize("model", benchmark.MODELS)
def test_one_hc0_trial_is_deterministic_and_fully_described(model):
    preset = None if model == "awgn" else "mid_latitude_disturbed"
    point_key = benchmark.canonical_point_key(model, preset, 20)
    task = {
        "mode_selector": "hc0", "model": model, "preset": preset,
        "snr_db": 20.0, "point_key": point_key, "trial": 1,
        "derived_seeds": {
            namespace: benchmark.derive_seed(9, namespace, point_key, 1)
            for namespace in ("workload", "watterson", "awgn")
        },
    }
    first, _ = benchmark.execute_trial(task)
    second, _ = benchmark.execute_trial(task)
    assert first["outcome"] == second["outcome"] == "decoded"
    assert first["payload_sha256"] == second["payload_sha256"]
    assert first["channel_measurements"] == second["channel_measurements"]
    assert first["physical_payload_bytes"] == 64
    assert first["air_header_bytes"] == 10
    assert first["data_body_bytes"] == 54
    assert first["snr_reference_samples"] == [0, first["tx_samples"]]
    assert first["channel_description"]["stage_order"][-1] in {
        "awgn", "fixed_power_awgn_experiment_local"}
    if model == "watterson_fixed_n0":
        assert first["channel_description"]["fading_continuity"] == (
            "independent_reset_per_frame_not_continuous")


def test_sweep_artifact_replays_by_record_index(tmp_path):
    output = tmp_path / "smoke.json"
    args = benchmark.parse_args([
        "sweep", "--model", "awgn", "--points", "20", "--trials", "1",
        "--workers", "1", "--save-failures", "0", "--out", str(output),
    ])
    assert benchmark.sweep(args) == 0
    document = json.loads(output.read_text())
    assert document["schema_id"] == benchmark.SCHEMA_ID
    assert document["seed_policy"]["matched_across_modes"] is True
    replay_args = benchmark.parse_args([
        "replay", "--artifact", str(output), "--record-index", "0",
    ])
    assert benchmark.replay(replay_args) == 0


def test_tiny_workload_artifact_is_distinct_and_replayable(tmp_path):
    output = tmp_path / "tiny.json"
    args = benchmark.parse_args([
        "sweep", "--modes", "experiments.hr0.hr0b:HR0B",
        "--model", "awgn", "--points", "20", "--trials", "1",
        "--workers", "1", "--save-failures", "0",
        "--payload-bytes", "12", "--useful-application-bytes", "2",
        "--workload-name", "tiny_ack", "--out", str(output),
    ])
    assert benchmark.sweep(args) == 0
    document = json.loads(output.read_text())
    assert document["schema_version"] == 2
    assert document["workload"] == {
        "name": "tiny_ack", "physical_payload_bytes": 12,
        "air_header_bytes": 10, "data_body_bytes": 2,
        "useful_application_bytes": 2, "useful_application_bits": 16,
    }
    assert document["trials"][0]["tx_samples"] < 200_000
    assert document["mode_metadata"][0]["declared_airtime_seconds"] == 3.652
    replay_args = benchmark.parse_args([
        "replay", "--artifact", str(output), "--record-index", "0",
    ])
    assert benchmark.replay(replay_args) == 0


def test_schema_v1_style_task_defaults_to_matched_full_workload():
    key = benchmark.canonical_point_key("awgn", None, 20)
    task = {
        "mode_selector": "hc0", "model": "awgn", "preset": None,
        "snr_db": 20.0, "point_key": key, "trial": 1,
        "derived_seeds": {
            namespace: benchmark.derive_seed(31, namespace, key, 1)
            for namespace in ("workload", "watterson", "awgn")
        },
    }
    record, _ = benchmark.execute_trial(task)
    assert record["physical_payload_bytes"] == 64
    assert record["data_body_bytes"] == 54
    assert record["outcome"] == "decoded"
