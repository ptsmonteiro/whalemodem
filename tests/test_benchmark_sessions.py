import json
import subprocess
import sys
from pathlib import Path


def test_one_session_benchmark_writes_directional_full_stack_metrics(tmp_path):
    root = Path(__file__).parents[1]
    output = tmp_path / "sessions.json"
    completed = subprocess.run([
        sys.executable, str(root / "scripts" / "benchmark_sessions.py"),
        "--model", "awgn", "--policy", "vhf-fm", "--points", "40",
        "--trials", "1", "--bytes", "1", "--seed", "17",
        "--out", str(output),
    ], cwd=root, timeout=30)
    assert completed.returncode == 0
    document = json.loads(output.read_text())
    trial = document["trials"][0]
    assert trial["connection_success"]
    assert trial["transfer_completion"]
    assert trial["disconnect_success"]
    assert trial["setup_time_simulated_seconds"] > 0
    assert trial["useful_bytes_per_simulated_second"] > 0
    assert set(trial["directions"]) == {"A->B", "B->A"}
    assert set(trial["channel_measurements"]) == {"A->B", "B->A"}
    assert "retransmissions" in trial["link_metrics"]["A->B"]
