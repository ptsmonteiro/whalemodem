import importlib.util
import json
from pathlib import Path
import sys

import pytest
import numpy as np

from whale.trials import common_decoder_metrics


SCRIPT = Path(__file__).parents[1] / "scripts" / "compare_hc1_hf2.py"
SPEC = importlib.util.spec_from_file_location("compare_hc1_hf2", SCRIPT)
comparison = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = comparison
SPEC.loader.exec_module(comparison)


def test_pairing_seed_is_mode_independent_and_point_stable():
    seed = comparison.paired_seed(17, 2, 3, 4)
    assert seed == comparison.paired_seed(17, 2, 3, 4)
    assert seed != comparison.paired_seed(17, 2, 3, 5)
    assert seed != comparison.paired_seed(17, 2, 4, 4)


def test_common_diagnostics_retain_fec_tail_status():
    metrics = common_decoder_metrics(
        {"fec_tail_ok": False, "crc_ok": False}, np.zeros(4))
    assert metrics["fec_tail_ok"] is False
    assert metrics["crc_ok"] is False


@pytest.mark.parametrize(
    "total, acquired, errors, expected",
    [(300, 300, 0, "pass"), (300, 0, 0, "confirmed_fail"),
     (20, 20, 0, "exploratory"), (300, 300, 1, "confirmed_fail")])
def test_gate_status_applies_trial_and_wilson_requirements(
        total, acquired, errors, expected):
    acquisition = comparison.proportion(acquired, total)
    fer = comparison.proportion(total - acquired, total)
    assert comparison.gate_status(total, acquisition, fer, errors) == expected


def test_one_trial_uses_production_payload_sizes_and_retains_assumptions(tmp_path):
    output = tmp_path / "result.json"
    assert comparison.main([
        "--condition", "static", "--points", "30", "--trials", "1",
        "--workers", "1", "--seed", "19", "--out", str(output),
    ]) == 0
    document = json.loads(output.read_text())
    assert {(trial["mode_name"], trial["payload_bytes"])
            for trial in document["trials"]} == {("hc1", 74), ("hf2", 117)}
    summaries = document["metadata"]["summaries"]
    assert {row["data_chunk_bytes"] for row in summaries} == {64, 107}
    assert all(row["qualification_gate"] == "exploratory" for row in summaries)
    assert document["metadata"]["tx_rms_target"] == 0.128
    assert "no turnaround" in document["metadata"]["hr0_data_ack"]["assumptions"]
    stage = document["trials"][0]["channel_measurements"]["stage_0"]
    assert stage["target_rms"] == 0.128
    assert stage["input_rms"] > 0
