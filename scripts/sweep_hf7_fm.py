"""Hardware sweep harness for the HF7-geometry FM OFDM candidate.

This deliberately bypasses Link/ARQ.  Every trial sends one deterministic,
full-capacity packet in each requested direction and accepts it only when the
decoder returns the exact packet bytes.  Configurations are round-robined so
slow changes on the IC-705 <-> HT path do not bias one candidate.

The mode module is intentionally injectable because this harness is useful
while the candidate is being tuned::

    python scripts/sweep_hf7_fm.py --module whale.modes.hf7_fm_mode \
      --config bits_per_symbol=5,fft_size=240,band_lo_hz=300,band_hi_hz=2700 \
      --config bits_per_symbol=6,fft_size=240,band_lo_hz=300,band_hi_hz=2700

The module should expose ``mode_for(**kwargs)`` (preferred), ``build_mode``
or a mode object named ``MODE``/``HF7_FM``.  A mode's own configuration is
recorded in the result when it provides ``describe`` or ``geometry``.
This script never keys radios during import or configuration parsing.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import bench
from whale import framing
from whale import rx_audio

DEFAULT_MODULE = "whale.modes.vf12"
DEFAULT_CONFIG = {
    "bits_per_carrier": 4,
    "cp_len": 36,
    "band_lo_hz": 500.0,
    "band_hi_hz": 3000.0,
    "carrier_spacing_hz": 50.0,
    "lead_in_seconds": 0.5,
    "pilot_comb_stride": 8,
    "pilot_time_span": 3,
}


def _value(text: str):
    lowered = text.strip().lower()
    if lowered in {"none", "null"}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        number = float(text)
        return int(number) if number.is_integer() else number
    except ValueError:
        return text


def parse_config(text: str) -> dict:
    result = dict(DEFAULT_CONFIG)
    if text.strip():
        for item in text.split(","):
            if "=" not in item:
                raise argparse.ArgumentTypeError("config items must be key=value")
            key, value = item.split("=", 1)
            key = key.strip()
            if not key:
                raise argparse.ArgumentTypeError("config key cannot be empty")
            if key == "bits_per_symbol":
                key = "bits_per_carrier"
            result[key] = _value(value)
    return result


def make_mode(module_name: str, config: dict):
    for key in ("carrier_spacing_hz", "carrier_offset_hz", "spacing_hz"):
        if key in config and float(config[key]) != 50.0:
            raise ValueError("HF7 FM sweep fixes carrier spacing at 50 Hz")
    module = importlib.import_module(module_name)
    factory_config = dict(config)
    if "n_preamble_symbols" in factory_config:
        raise ValueError("VF12 uses settled lead_in_seconds=0.5; n_preamble_symbols is unsupported")
    # Geometry validation above applies even to injectable factories.
    factory_config.pop("carrier_spacing_hz", None)
    factory_config.pop("carrier_offset_hz", None)
    factory_config.pop("spacing_hz", None)
    if "bits_per_symbol" in factory_config and "bits_per_carrier" not in factory_config:
        factory_config["bits_per_carrier"] = factory_config.pop("bits_per_symbol")
    for name in ("mode_for", "build_mode", "make_mode"):
        factory = getattr(module, name, None)
        if factory is not None:
            return factory(**factory_config)
    for name in ("MODE", "FM_OFDM", "HF7_FM", "HF7FM", "HF7"):
        mode = getattr(module, name, None)
        if mode is not None:
            if config != DEFAULT_CONFIG:
                raise ValueError(f"{module_name}.{name} is fixed; use mode_for/build_mode for sweeps")
            return mode
    raise AttributeError(f"{module_name} exposes no mode_for/build_mode/MODE")


def packet_size(mode) -> int:
    # chunk_size is the link DATA payload. The on-air packet also carries the
    # fixed air header; using max_payload_bytes here made older wrappers hide
    # this accounting and risks testing a short packet.
    return int(framing.AIR_HEADER_BYTES + mode.chunk_size)


def _phy_value(mode, key):
    for obj in (mode, getattr(mode, "codec", None),
                getattr(getattr(mode, "codec", None), "streaming_phy", None),
                getattr(getattr(mode, "codec", None), "phy", None)):
        if obj is not None and hasattr(obj, key):
            return getattr(obj, key)
    return None


def validate_mode(mode, config):
    """Reject a candidate when its exposed geometry contradicts the request."""
    actual_spacing = (_phy_value(mode, "carrier_spacing_hz") or
                      _phy_value(mode, "spacing_hz"))
    if actual_spacing is not None and abs(float(actual_spacing) - 50.0) > 1e-9:
        raise ValueError(f"candidate reports {actual_spacing:g} Hz carrier spacing; expected 50 Hz")
    for requested, aliases in {
        "bits_per_carrier": ("bits_per_carrier",),
        "fft_size": ("fft_size",),
        "cp_len": ("cp_len",),
    }.items():
        if requested not in config:
            continue
        actual = next((_phy_value(mode, key) for key in aliases
                       if _phy_value(mode, key) is not None), None)
        if actual is not None and float(actual) != float(config[requested]):
            raise ValueError(f"candidate ignored {requested}={config[requested]!r}; reports {actual!r}")


def mode_metadata(mode, config):
    geometry = getattr(mode, "geometry", None)
    if callable(geometry):
        try:
            geometry = geometry()
        except TypeError:
            geometry = None
    describe = getattr(mode, "describe", None)
    return {
        "config": config,
        "name": getattr(mode, "name", type(mode).__name__),
        "mode_id": getattr(mode, "mode_id", None),
        "packet_bytes": packet_size(mode),
        "chunk_bytes": getattr(mode, "chunk_size", None),
        "airtime_seconds": (float(mode.airtime(packet_size(mode)))
                             if hasattr(mode, "airtime") else None),
        "tx_sample_rate": getattr(mode, "tx_sample_rate", None),
        "rx_sample_rate": getattr(mode, "rx_sample_rate", None),
        "geometry": geometry if isinstance(geometry, dict) else None,
        "describe": describe() if callable(describe) else None,
    }


def run_trial(tx, rx, mode, direction, trial, seed, capture_dir, capture_tail, config_id):
    stale = rx.snapshot_rx()
    rx.consume_rx(len(stale))
    code = 0 if direction == "ic705->ht" else 1
    payload = np.random.default_rng(np.random.SeedSequence(
        [seed, int(getattr(mode, "mode_id", 0) or 0), code, trial]
    )).integers(0, 256, packet_size(mode), dtype=np.uint8).tobytes()
    t0 = time.time()
    audio = np.zeros(0, dtype=np.float32)
    keyed = 0.0
    captured = np.zeros(0, dtype=np.float32)
    try:
        audio = np.asarray(mode.encode(payload), dtype=np.float32)
        keyed = float(tx.send(audio))
        time.sleep(capture_tail)
        captured = np.asarray(rx.snapshot_rx(), dtype=np.float32)
        decoded = mode.decode(captured)
        result_payload = decoded.get("payload")
        exact = result_payload == payload
        if isinstance(exact, np.ndarray):
            exact = bool(np.all(exact))
        exact = bool(exact)
        error = None
    except Exception as exc:  # retain the trial as a failed measurement
        decoded = {}
        exact = False
        error = f"{type(exc).__name__}: {exc}"
    record = {
        "direction": direction, "trial": trial, "payload_bytes": len(payload),
        "tx_samples": len(audio), "rx_samples": len(captured),
        "keyed_seconds": keyed, "wall_seconds": time.time() - t0,
        "exact": exact, "confidence": decoded.get("confidence"),
        "decoder": {k: v for k, v in decoded.items()
                    if isinstance(v, (str, int, float, bool, type(None)))},
        "error": error,
    }
    record["config_id"] = config_id
    if capture_dir is not None:
        filename = f"c{config_id:02d}_{getattr(mode, 'name', 'mode')}_{direction.replace('->', '_')}_{trial:03d}.npz"
        np.savez_compressed(capture_dir / filename, audio=captured,
                            payload=np.frombuffer(payload, dtype=np.uint8))
        record["capture"] = filename
    status = "PASS" if exact else "FAIL"
    print(f"    {direction} {trial}: {status} {len(payload)}B keyed={keyed:.2f}s "
          f"rx={len(captured)} conf={record['confidence']}")
    return record


class _OfflineTransport:
    """Two ended audio loopback used by ``--offline``; never opens a radio."""
    def __init__(self):
        self.peer = None
        self._audio = np.zeros(0, dtype=np.float32)

    def start_receiving(self):
        return None

    def snapshot_rx(self):
        return self._audio.copy()

    def consume_rx(self, count):
        self._audio = self._audio[int(count):]

    def send(self, audio):
        received = rx_audio.downsample(np.asarray(audio, dtype=np.float32))
        # The real transport leaves a little trailing audio after PTT drops.
        self.peer._audio = np.concatenate((received, np.zeros(2400, np.float32)))
        return len(audio) / 48000.0

    def close(self):
        return None


@contextmanager
def offline_pair(*_args, **_kwargs):
    a, b = _OfflineTransport(), _OfflineTransport()
    a.peer, b.peer = b, a
    yield a, b


def summary_by_config_direction(records):
    groups = {}
    for row in records:
        key = (row["config_id"], row["direction"])
        groups.setdefault(key, []).append(row)
    return [{"config_id": config_id, "direction": direction,
             "passed": sum(r["exact"] for r in rows), "total": len(rows),
             "rate": sum(r["exact"] for r in rows) / len(rows)}
            for (config_id, direction), rows in sorted(groups.items())]


def main(argv=None, *, pair_factory=bench.radio_pair):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--module", default=DEFAULT_MODULE)
    parser.add_argument("--config", action="append", type=parse_config, default=None)
    parser.add_argument("--direction", choices=("both", "ic705->ht", "ht->ic705"), default="both")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260913)
    parser.add_argument("--capture-tail", type=float, default=1.5)
    parser.add_argument("--inter-trial", type=float, default=0.5)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--offline", action="store_true",
                        help="run the exact payload/encode/decode loop through rx_audio.downsample; no radios")
    args = parser.parse_args(argv)
    if args.trials < 1 or args.capture_tail < 0 or args.inter_trial < 0:
        parser.error("trials must be positive and timing intervals nonnegative")
    configs = args.config or [dict(DEFAULT_CONFIG)]
    modes = []
    for cfg in configs:
        mode = make_mode(args.module, cfg)
        validate_mode(mode, cfg)
        modes.append((cfg, mode))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    capture_dir = args.output_dir / "captures"
    capture_dir.mkdir(exist_ok=True)
    metadata = {"utc": datetime.now(timezone.utc).isoformat(), "module": args.module,
                "run_type": "offline_simulation" if args.offline else "hardware",
                "trials": args.trials, "seed": args.seed, "directions": args.direction,
                "capture_tail": args.capture_tail,
                "modes": [{**mode_metadata(mode, cfg), "config_id": i}
                          for i, (cfg, mode) in enumerate(modes)]}
    print(f"HF7 FM OFDM sweep: {args.module}, {len(modes)} configs, {args.trials} trials/config")
    records = []
    started = False
    try:
        with (offline_pair("ic705", "ht", warmup=0.0) if args.offline
              else pair_factory("ic705", "ht", warmup=3.0)) as (a, b):
            started = True
            for trial in range(1, args.trials + 1):
                for config_id, (cfg, mode) in enumerate(modes):
                    legs = []
                    if args.direction in ("both", "ic705->ht"):
                        legs.append((a, b, "ic705->ht"))
                    if args.direction in ("both", "ht->ic705"):
                        legs.append((b, a, "ht->ic705"))
                    for tx, rx, direction in legs:
                        records.append(run_trial(tx, rx, mode, direction, trial,
                                             args.seed, capture_dir, args.capture_tail, config_id))
                        metadata["results"] = records
                        metadata["summary"] = {"passed": sum(r["exact"] for r in records), "total": len(records)}
                        metadata["summary_by_config_direction"] = summary_by_config_direction(records)
                        (args.output_dir / "result.json").write_text(json.dumps(metadata, indent=2, default=str) + "\n")
                        time.sleep(args.inter_trial)
    except Exception as exc:
        metadata["preflight_error" if not started else "trial_error"] = f"{type(exc).__name__}: {exc}"
        metadata["results"] = records
        metadata["summary"] = {"passed": sum(r["exact"] for r in records), "total": len(records)}
        metadata["summary_by_config_direction"] = summary_by_config_direction(records)
        (args.output_dir / "result.json").write_text(
            json.dumps(metadata, indent=2, default=str) + "\n")
        error_key = "preflight_error" if not started else "trial_error"
        print(f"{error_key.replace('_', ' ')}: {metadata[error_key]}")
        print(f"wrote {args.output_dir / 'result.json'}")
        return 2 if not started else 1
    metadata["results"] = records
    metadata["summary"] = {"passed": sum(r["exact"] for r in records), "total": len(records)}
    (args.output_dir / "result.json").write_text(json.dumps(metadata, indent=2, default=str) + "\n")
    print(f"wrote {args.output_dir / 'result.json'}")
    return 0 if metadata["summary"]["passed"] == metadata["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
