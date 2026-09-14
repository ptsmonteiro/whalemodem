"""Offline harness tests for the configurable HF7/FM radio sweep."""

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

from whale import framing


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("sweep_hf7_fm_test_module",
                                              ROOT / "scripts/sweep_hf7_fm.py")
sweep = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sweep
SPEC.loader.exec_module(sweep)


class FakeMode:
    _next_id = 40

    def __init__(self, **config):
        self.config = dict(config)
        self.name = f"fake-{FakeMode._next_id}"
        self.mode_id = FakeMode._next_id
        FakeMode._next_id += 1
        self.chunk_size = 7
        self.tx_sample_rate = 48_000
        self.rx_sample_rate = 12_000
        self.carrier_spacing_hz = config.get("carrier_spacing_hz", 50.0)
        self.bits_per_symbol = config.get("bits_per_symbol",
                                          config.get("bits_per_carrier"))
        self.fft_size = config.get("fft_size", 240)
        self.cp_len = config["cp_len"]

    def encode(self, payload):
        assert len(payload) == self.chunk_size + framing.AIR_HEADER_BYTES
        return np.asarray(list(payload), dtype=np.float32)

    def decode(self, audio):
        payload = bytes(np.asarray(audio, dtype=np.uint8).tolist())
        return {"payload": payload, "confidence": 1.0}

    def airtime(self, payload_len):
        assert payload_len == self.chunk_size + framing.AIR_HEADER_BYTES
        return 1.0

    def geometry(self):
        return dict(self.config)


class FakeModule:
    @staticmethod
    def mode_for(**config):
        return FakeMode(**config)


class FakeTransport:
    def __init__(self, fail_on_send=None):
        self.peer = None
        self.data = np.zeros(0, dtype=np.float32)
        self.count = 0
        self.fail_on_send = fail_on_send

    def snapshot_rx(self):
        return self.data.copy()

    def consume_rx(self, count):
        self.data = self.data[int(count):]

    def send(self, audio):
        self.count += 1
        if self.fail_on_send == self.count:
            raise RuntimeError("injected transport failure")
        self.peer.data = np.asarray(audio, dtype=np.float32).copy()
        return len(audio) / 48_000

    def close(self):
        pass

    def start_receiving(self):
        pass


def _pair_factory(fail_on_send=None):
    def factory(*_args, **_kwargs):
        class Pair:
            def __enter__(self):
                self.a, self.b = FakeTransport(fail_on_send), FakeTransport()
                self.a.peer, self.b.peer = self.b, self.a
                return self.a, self.b

            def __exit__(self, *_exc):
                return False
        return Pair()
    return factory


def test_sweep_keeps_config_ids_and_header_accounting(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "fake_sweep_modes", FakeModule())
    monkeypatch.setattr(sweep.time, "sleep", lambda *_args: None)
    rc = sweep.main([
        "--module", "fake_sweep_modes", "--trials", "1",
        "--output-dir", str(tmp_path),
        "--config", "band_lo_hz=300,band_hi_hz=2700",
        "--config", "band_lo_hz=500,band_hi_hz=2900",
    ], pair_factory=_pair_factory())
    assert rc == 0
    document = json.loads((tmp_path / "result.json").read_text())
    assert [entry["config_id"] for entry in document["modes"]] == [0, 1]
    assert all(row["payload_bytes"] == 7 + framing.AIR_HEADER_BYTES
               for row in document["results"])
    assert {row["config_id"] for row in document["results"]} == {0, 1}


def test_sweep_writes_completed_rows_when_a_later_trial_raises(tmp_path,
                                                                monkeypatch):
    monkeypatch.setitem(sys.modules, "fake_sweep_modes", FakeModule())
    monkeypatch.setattr(sweep.time, "sleep", lambda *_args: None)
    rc = sweep.main([
        "--module", "fake_sweep_modes", "--trials", "2",
        "--output-dir", str(tmp_path),
    ], pair_factory=_pair_factory(fail_on_send=2))
    assert rc == 1
    document = json.loads((tmp_path / "result.json").read_text())
    assert len(document["results"]) == 4
    assert document["results"][0]["exact"] is True
    assert document["results"][0]["config_id"] == 0
    assert document["results"][0]["payload_bytes"] == 7 + framing.AIR_HEADER_BYTES
    assert document["results"][2]["error"].startswith("RuntimeError:")
