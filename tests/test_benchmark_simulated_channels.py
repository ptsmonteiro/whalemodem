import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_simulated_channels.py"
SPEC = importlib.util.spec_from_file_location("benchmark_simulated_channels", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_one_point_benchmark_writes_versioned_replayable_result(tmp_path):
    output = tmp_path / "result.json"
    result = benchmark.main([
        "--model", "awgn", "--policy", "vhf-fm", "--points", "40",
        "--trials", "1", "--modes", "300baud", "--seed", "17",
        "--out", str(output),
    ])
    assert result == 0
    document = json.loads(output.read_text())
    assert document["schema_version"] == 2
    assert document["seed"] == 17
    assert document["summary"] == {"passed": 1, "total": 1}
    assert document["metadata"]["trials_per_point"] == 1
    assert document["metadata"]["channel_descriptions_by_point"][0][
        "snr"]["db"] == 40
    assert document["trials"][0]["channel_measurements"]["waveform_snr_db"] == 40
