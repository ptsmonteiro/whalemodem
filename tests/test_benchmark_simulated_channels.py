import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_simulated_channels.py"
SPEC = importlib.util.spec_from_file_location("benchmark_simulated_channels", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_one_point_benchmark_writes_versioned_replayable_result(tmp_path):
    output = tmp_path / "result.json"
    result = benchmark.main([
        "--model", "awgn", "--policy", "fm", "--points", "40",
        "--trials", "1", "--modes", "vf14-4", "--seed", "17",
        "--workers", "1",
        "--out", str(output),
    ])
    assert result == 0
    document = json.loads(output.read_text())
    assert document["schema_version"] == 2
    assert document["seed"] == 17
    assert document["summary"] == {"passed": 1, "total": 1}
    assert document["metadata"]["trials_per_point"] == 1
    assert document["metadata"]["worker_processes"] == 1
    assert document["metadata"]["requested_payload_bytes"] is None
    assert document["metadata"]["data_payload_bytes_by_mode"] == {"23": 264}
    assert document["metadata"]["actual_payload_bytes_by_mode"] == {"23": 274}
    assert document["trials"][0]["payload_bytes"] == 274
    assert document["metadata"]["channel_descriptions_by_point"][0][
        "snr"]["db"] == 40
    assert document["trials"][0]["channel_measurements"]["snr_3khz_db"] == 40
    summary = document["metadata"]["summary_by_mode_point"][0]
    assert summary["acquisition_probability"]["rate"] == 1
    assert summary["frame_error_rate"]["rate"] == 0
    assert summary["payload_delivery_rate"]["rate"] == 1
    # No mode currently ships bit-level decode diagnostics (that evidence
    # was CPFSK-only, and the CPFSK modes are gone), so "ber" has no
    # evidence frames and each trial's decoder_metrics has no bit-error
    # keys -- see whale/qualification.py's run_frame_trial.
    assert summary["ber"] is None
    assert "total_bit_errors" not in document["trials"][0]["decoder_metrics"]


@pytest.mark.parametrize("value", ["-1", "not-an-integer"])
def test_payload_rejects_negative_or_invalid_values(value, tmp_path, capsys):
    with pytest.raises(SystemExit):
        benchmark.main([
            "--model", "awgn", "--policy", "fm", "--points", "40",
            "--trials", "1", "--modes", "vf14-4",
            "--payload-bytes", value, "--out", str(tmp_path / "result.json"),
        ])
    assert "payload-bytes" in capsys.readouterr().err


def test_payload_must_fit_every_selected_mode(tmp_path, capsys):
    with pytest.raises(SystemExit):
        benchmark.main([
            "--model", "awgn", "--policy", "fm", "--points", "40",
            "--trials", "1", "--modes", "vf14-4", "vf13",
            "--payload-bytes", "300", "--out", str(tmp_path / "result.json"),
        ])
    message = capsys.readouterr().err
    assert "exceeds the selected mode capacity" in message
    assert "vf14-4: 264" in message


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_workers_must_be_positive(value, tmp_path, capsys):
    with pytest.raises(SystemExit):
        benchmark.main([
            "--model", "awgn", "--policy", "fm", "--points", "40",
            "--trials", "1", "--modes", "vf14-4", "--workers", value,
            "--out", str(tmp_path / "result.json"),
        ])
    assert "workers" in capsys.readouterr().err


def test_default_worker_count_uses_eighty_percent_of_available_cpus(monkeypatch):
    monkeypatch.setattr(benchmark, "available_cpu_count", lambda: 10)
    assert benchmark.default_worker_count() == 8
