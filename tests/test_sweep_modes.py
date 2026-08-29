from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np

from whale.trials import TrialOutcome


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("sweep_modes", SCRIPTS / "sweep_modes.py")
sweep_modes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sweep_modes)


class FakeMode:
    name = "fake"
    mode_id = 9
    chunk_size = 6
    confidence_threshold = 0.7
    tx_sample_rate = 48_000
    rx_sample_rate = 12_000

    def encode(self, payload):
        return np.frombuffer(payload, dtype=np.uint8).astype(np.float32)

    def decode(self, audio):
        payload = bytes(np.asarray(audio, dtype=np.uint8))
        return {"payload": payload, "confidence": 0.9,
                "carrier_snr_db": np.array([10.0, 12.0])}


class FakeTransport:
    def __init__(self):
        self.peer = None
        self.audio = np.zeros(0, np.float32)

    def snapshot_rx(self):
        return self.audio.copy()

    def consume_rx(self, count):
        self.audio = self.audio[count:]

    def send(self, audio):
        self.peer.audio = np.asarray(audio, np.float32).copy()
        return len(audio) / 48_000


def fake_pair_factory(a, b, warmup):
    @contextmanager
    def pair():
        ta, tb = FakeTransport(), FakeTransport()
        ta.peer, tb.peer = tb, ta
        yield ta, tb
    return pair()


def test_channel_registries_drive_mode_selection():
    vhf = sweep_modes.registry_for("vhf-fm")
    hf = sweep_modes.registry_for("hf-ssb")
    assert sweep_modes.select_modes(vhf, None) == tuple(vhf.modes)
    assert [mode.name for mode in sweep_modes.select_modes(vhf, ["0", "vf3"])] == [
        "300baud", "vf3"]
    assert [mode.name for mode in hf.modes] == ["hc0", "hc1"]


def test_direct_trial_uses_full_link_packet_and_versioned_record(tmp_path):
    ta, tb = FakeTransport(), FakeTransport()
    ta.peer, tb.peer = tb, ta
    records = sweep_modes.run_direction(
        ta, tb, FakeMode(), "A:a->B:b", 2, 123,
        capture_dir=tmp_path, capture="all", capture_tail=0,
        inter_trial=0, sleep=lambda _: None)
    assert all(record.outcome is TrialOutcome.DECODED for record in records)
    assert all(record.payload_bytes == 16 for record in records)  # header + chunk
    assert all(record.tx_sample_rate == 48_000 for record in records)
    assert all(record.rx_sample_rate == 12_000 for record in records)
    assert all(Path(record.capture).suffix == ".npz" for record in records)
    capture = np.load(records[0].capture)
    assert np.array_equal(capture["audio"].astype(np.uint8), capture["payload"])


def test_main_writes_strict_json_and_summary_without_real_hardware(tmp_path, monkeypatch):
    class Registry:
        modes = (FakeMode(),)
        supported_ids = (9,)

    monkeypatch.setattr(sweep_modes, "registry_for", lambda _: Registry())
    exit_code = sweep_modes.main([
        "--channel", "vhf-fm", "--trials", "2", "--capture", "none",
        "--capture-tail", "0", "--inter-trial", "0",
        "--output-dir", str(tmp_path),
    ], pair_factory=fake_pair_factory)
    assert exit_code == 0
    document = json.loads((tmp_path / "result.json").read_text())
    assert document["schema_version"] == 2
    assert document["summary"] == {"passed": 4, "total": 4}
    assert len(document["metadata"]["summary_by_mode_direction"]) == 2
    assert document["metadata"]["summary_by_mode_direction"][0][
        "data_chunk_bytes"] == 6
    assert not (tmp_path / "captures").exists()
