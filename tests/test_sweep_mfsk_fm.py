"""Offline harness tests for the configurable FM MFSK radio sweep."""

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

from whale import framing


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("sweep_mfsk_fm_test_module",
                                              ROOT / "scripts/sweep_mfsk_fm.py")
sweep = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sweep
SPEC.loader.exec_module(sweep)


class FakeMode:
    _next_id = 60

    def __init__(self, **config):
        self.config = dict(config)
        self.name = f"fake-{FakeMode._next_id}"
        self.mode_id = FakeMode._next_id
        FakeMode._next_id += 1
        self.chunk_size = 7
        self.tx_sample_rate = 48_000
        self.rx_sample_rate = 12_000
        self.tone_count = config.get("tone_count")
        self.subbands = config.get("subbands")
        self.symbol_samples = config.get("symbol_samples")
        self.constraint = config.get("constraint")
        self.drive_scale = config.get("drive_scale")

    def encode(self, payload):
        assert len(payload) == self.chunk_size + framing.AIR_HEADER_BYTES
        return np.asarray(list(payload), dtype=np.float32)

    def decode(self, audio):
        payload = bytes(np.asarray(audio, dtype=np.uint8).tolist())
        return {"payload": payload, "confidence": 1.0, "tone_snr_db": 20.0,
                "offset_hz": 0.5}

    def airtime(self, payload_len):
        assert payload_len == self.chunk_size + framing.AIR_HEADER_BYTES
        return 1.0

    def net_bit_rate(self):
        return self.chunk_size * 8 / 1.0

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


def test_sweep_expands_drives_and_keeps_config_ids_and_header_accounting(tmp_path,
                                                                          monkeypatch):
    monkeypatch.setitem(sys.modules, "fake_mfsk_sweep_modes", FakeModule())
    monkeypatch.setattr(sweep.time, "sleep", lambda *_args: None)
    rc = sweep.main([
        "--module", "fake_mfsk_sweep_modes", "--trials", "1",
        "--drive-scales", "0.1,0.2", "--output-dir", str(tmp_path),
        "--config", "tone_count=16,subbands=1,symbol_samples=480",
        "--config", "tone_count=16,subbands=4,symbol_samples=1920",
    ], pair_factory=_pair_factory())
    assert rc == 0
    document = json.loads((tmp_path / "result.json").read_text())
    assert [entry["config_id"] for entry in document["modes"]] == [0, 1, 2, 3]
    assert {entry["config"]["drive_scale"] for entry in document["modes"]} == {0.1, 0.2}
    assert all(row["payload_bytes"] == 7 + framing.AIR_HEADER_BYTES
               for row in document["results"])
    assert {row["config_id"] for row in document["results"]} == {0, 1, 2, 3}
    assert all(row["decoder"].get("tone_snr_db") == 20.0 for row in document["results"])


def test_sweep_writes_completed_rows_when_a_later_trial_raises(tmp_path,
                                                                monkeypatch):
    monkeypatch.setitem(sys.modules, "fake_mfsk_sweep_modes", FakeModule())
    monkeypatch.setattr(sweep.time, "sleep", lambda *_args: None)
    rc = sweep.main([
        "--module", "fake_mfsk_sweep_modes", "--trials", "2",
        "--config", "drive_scale=0.3", "--output-dir", str(tmp_path),
    ], pair_factory=_pair_factory(fail_on_send=2))
    assert rc == 1
    document = json.loads((tmp_path / "result.json").read_text())
    assert len(document["results"]) == 4
    assert document["results"][0]["exact"] is True
    assert document["results"][0]["config_id"] == 0
    assert document["results"][0]["payload_bytes"] == 7 + framing.AIR_HEADER_BYTES
    assert document["results"][2]["error"].startswith("RuntimeError:")


def test_validate_mode_rejects_geometry_mismatch():
    class IgnoresSubbands(FakeMode):
        def __init__(self, **config):
            super().__init__(**config)
            self.subbands = 1  # pretend the factory ignored the request

    mode = IgnoresSubbands(tone_count=16, subbands=4, symbol_samples=1920)
    try:
        sweep.validate_mode(mode, {"tone_count": 16, "subbands": 4, "symbol_samples": 1920})
    except ValueError as exc:
        assert "subbands" in str(exc)
    else:
        raise AssertionError("expected validate_mode to reject the mismatch")


def test_band_fill_warning_catches_the_silent_480_sample_default():
    """Regression for the N16-combinatorial hardware sweeps that silently ran
    at 100 Bd (symbol_samples=480, sweep_mfsk_fm's DEFAULT_CONFIG/mode_for
    default) instead of the intended 150 Bd (symbol_samples=320): omitting
    symbol_samples from a --config string left band_hi_hz=3000 requested but
    only 2200 Hz actually used, and nothing said so. See
    experiments/fm_mfsk/evaluate_candidates.py's Comb_N16_* candidates
    (symbol_samples=320) vs the scratchpad hw_k*_5s/8s result.json configs
    (symbol_samples=480) for the two geometries this compares."""
    from experiments.fm_mfsk.mfsk_fm_mode import mode_for

    config = dict(tone_count=16, subbands=1, symbol_samples=480,
                  frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                  mapping="combinatorial", active_tones=4, fec_rate="7/8")
    mode = mode_for(**config)
    assert mode.symbol_samples == 480
    assert mode.geometry()["band_hi_hz"] == 2200.0

    warning = sweep.band_fill_warning(mode, config)
    assert warning is not None
    assert "2200" in warning and "3000" in warning

    # The intended geometry (symbol_samples=320, matching
    # evaluate_candidates.py) fills the requested band exactly and warns
    # about nothing.
    fixed_config = {**config, "symbol_samples": 320}
    fixed_mode = mode_for(**fixed_config)
    assert fixed_mode.geometry()["band_hi_hz"] == 3000.0
    assert sweep.band_fill_warning(fixed_mode, fixed_config) is None


def test_offline_end_to_end_against_the_real_mode(tmp_path):
    """The offline transport must round-trip a real FmMfskMode with no radios."""
    rc = sweep.main([
        "--offline", "--trials", "1", "--output-dir", str(tmp_path),
        "--config", "tone_count=16,subbands=1,symbol_samples=480,frame_seconds=5.0",
        "--drive-scales", "0.15",
    ])
    assert rc == 0
    document = json.loads((tmp_path / "result.json").read_text())
    assert document["run_type"] == "offline_simulation"
    assert document["summary"]["passed"] == document["summary"]["total"] > 0
