from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pytest

from whale.trials import TrialOutcome


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("sweep_modes", SCRIPTS / "sweep_modes.py")
sweep_modes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sweep_modes)

import bench


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
    vhf = sweep_modes.registry_for("fm")
    hf = sweep_modes.registry_for("hf")
    hf_experimental = sweep_modes.registry_for("hf", "experimental")
    assert sweep_modes.select_modes(vhf, None) == tuple(vhf.modes)
    assert [mode.name for mode in sweep_modes.select_modes(vhf, ["23", "vf13"])] == [
        "vf14-4", "vf13"]
    assert [mode.name for mode in hf.modes] == [
        "hr0", "hc0", "hc1w", "hf8", "hf7"]
    assert "hf2" not in [mode.name for mode in hf.modes]
    assert [mode.name for mode in hf_experimental.modes] == [
        "hr0", "hc0", "hf9", "hc1w", "hf8", "hf5", "hf6", "hf7", "hf2"]


RADIO_FIELDS = """audio.input = "in"
audio.output = "out"
ptt.backend = "vox"
"""


def _inventory(tmp_path, entries):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text("\n".join(
        f'[radios."{id_}"]\n{RADIO_FIELDS}'
        + "channels = [" + ", ".join(f'"{name}"' for name in channels) + "]\n"
        for id_, channels in entries))
    return path


def test_bench_pair_comes_from_the_configured_inventory_in_file_order(tmp_path):
    path = _inventory(tmp_path, [("hf-only", ["hf"]), ("first", ["fm", "hf"]),
                                 ("second", ["fm"]), ("third", ["fm"])])
    assert bench.channel_pair("fm", path) == ("first", "second")
    assert bench.channel_pair("hf", path) == ("hf-only", "first")

    lonely = _inventory(tmp_path / "lonely", [("only", ["fm"]), ("hf-only", ["hf"])])
    with pytest.raises(ValueError, match="needs two configured radios"):
        bench.channel_pair("fm", lonely)


def test_default_radios_are_the_first_two_configured_for_the_channel():
    inventory = {"one": ("fm", "hf"), "two": ("hf",), "three": ("fm",),
                 "four": ("fm",)}
    pair = lambda channel: tuple(
        id_ for id_, channels in inventory.items() if channel in channels)[:2]
    assert sweep_modes.resolve_radios("fm", None, None, pair=pair) == (
        "one", "three")
    assert sweep_modes.resolve_radios("fm", "given", None, pair=pair) == (
        "given", "one")
    # A radio named explicitly is not also chosen as the other station.
    assert sweep_modes.resolve_radios("fm", "one", None, pair=pair) == (
        "one", "three")
    assert sweep_modes.resolve_radios("fm", None, "one", pair=pair) == (
        "three", "one")
    assert sweep_modes.resolve_radios("fm", "a", "b", pair=pair) == ("a", "b")


def test_a_channel_without_two_radios_is_refused(tmp_path, monkeypatch):
    def pair(channel):
        raise ValueError(f"channel {channel!r} needs two configured radios")

    with pytest.raises(ValueError):
        sweep_modes.resolve_radios("fm", None, None, pair=pair)

    class Registry:
        modes = (FakeMode(),)
        supported_ids = (9,)

    monkeypatch.setattr(sweep_modes, "registry_for", lambda _channel, _level: Registry())
    monkeypatch.setattr(sweep_modes.bench, "channel_pair", pair)
    with pytest.raises(SystemExit) as refused:
        sweep_modes.main(["--channel", "fm", "--output-dir", str(tmp_path)],
                         pair_factory=fake_pair_factory)
    assert refused.value.code == 2
    assert not (tmp_path / "result.json").exists()


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

    monkeypatch.setattr(sweep_modes, "registry_for", lambda _channel, _level: Registry())
    monkeypatch.setattr(sweep_modes.bench, "channel_pair", lambda _channel: ("a", "b"))
    exit_code = sweep_modes.main([
        "--channel", "fm", "--trials", "2", "--capture", "none",
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
    assert document["metadata"]["mode_level"] == "default"
    assert not (tmp_path / "captures").exists()


def test_main_records_git_state_before_creating_output(tmp_path, monkeypatch):
    class Registry:
        modes = (FakeMode(),)
        supported_ids = (9,)

    output_dir = tmp_path / "new-output"
    monkeypatch.setattr(sweep_modes, "registry_for", lambda _channel, _level: Registry())
    monkeypatch.setattr(sweep_modes.bench, "channel_pair", lambda _channel: ("a", "b"))
    monkeypatch.setattr(sweep_modes, "_git_commit", lambda: "clean-start")
    monkeypatch.setattr(sweep_modes, "_git_dirty", output_dir.exists)

    exit_code = sweep_modes.main([
        "--channel", "fm", "--trials", "1", "--capture", "all",
        "--capture-tail", "0", "--inter-trial", "0",
        "--output-dir", str(output_dir),
    ], pair_factory=fake_pair_factory)

    assert exit_code == 0
    document = json.loads((output_dir / "result.json").read_text())
    assert document["metadata"]["git_commit"] == "clean-start"
    assert document["metadata"]["git_dirty"] is False
    assert (output_dir / "captures").is_dir()
