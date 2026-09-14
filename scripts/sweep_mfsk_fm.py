"""Hardware sweep harness for the FM MFSK candidates (single-band or K subbands).

Modeled on ``scripts/sweep_hf7_fm.py``: bypasses Link/ARQ entirely.  Every
trial sends one deterministic, full-capacity packet in each requested
direction and accepts it only when the decoder returns the exact packet
bytes.  Configurations are round-robined so slow changes on the IC-705 <->
HT path do not bias one candidate.  Every decode result's ``tone_snr_db``,
``offset_hz`` and (for K>1) ``tone_snr_db_per_subband`` are logged so a
phase-2 campaign can see per-candidate margin, not just pass/fail.

    python scripts/sweep_mfsk_fm.py --module experiments.fm_mfsk.mfsk_fm_mode \
      --config tone_count=16,subbands=1,symbol_samples=480,frame_seconds=5.0 \
      --config tone_count=16,subbands=4,symbol_samples=1920,frame_seconds=5.0

This script never keys radios during import or configuration parsing, and
``--offline`` runs the whole encode/channel/decode loop against a plain
audio loopback (through ``whale.rx_audio.downsample``, exactly as the
hardware transport would) so the harness is verified before any radio use.
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

#: The shipped mode (whale/modes/vf13.py), not the experiments/ prototype:
#: this harness now measures the on-air configuration, and a candidate that
#: diverges from it is reached with an explicit --module/--config.
DEFAULT_MODULE = "whale.modes.vf13"
#: VF13's shipped configuration (config A). Every key `mode_for` reads by
#: name, spelled out so a config string that omits one (e.g. symbol_samples)
#: cannot silently fall back to a different geometry than the one on the air
#: -- that used to default to symbol_samples=480 (100 Bd) here regardless of
#: what a candidate's own module shipped at.
DEFAULT_CONFIG = {
    "tone_count": 16,
    "subbands": 1,
    "mapping": "combinatorial",
    "active_tones": 6,
    "symbol_samples": 320,
    "frame_seconds": 8.0,
    "constraint": 7,
    "band_lo_hz": 600.0,
    "band_hi_hz": 3000.0,
    "fec_rate": "7/8",
    # drive_scale is deliberately left out: --drive-scales expands each
    # config across drive levels unless a --config string sets its own, and
    # VF13's shipped drive (0.077) is exactly this default's first value.
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
            result[key] = _value(value)
    return result


def make_mode(module_name: str, config: dict):
    module = importlib.import_module(module_name)
    factory_config = dict(config)
    for name in ("mode_for", "build_mode", "make_mode"):
        factory = getattr(module, name, None)
        if factory is not None:
            return factory(**factory_config)
    for name in ("MODE", "FM_MFSK"):
        mode = getattr(module, name, None)
        if mode is not None:
            if config != DEFAULT_CONFIG:
                raise ValueError(f"{module_name}.{name} is fixed; use mode_for/build_mode for sweeps")
            return mode
    raise AttributeError(f"{module_name} exposes no mode_for/build_mode/MODE")


def packet_size(mode) -> int:
    # chunk_size is the link DATA payload; the on-air packet also carries the
    # fixed air header, so this is what a real trial must send.
    return int(framing.AIR_HEADER_BYTES + mode.chunk_size)


def _phy_value(mode, key):
    return getattr(mode, key, None)


def validate_mode(mode, config):
    """Reject a candidate when its exposed geometry contradicts the request."""
    for requested in ("tone_count", "subbands", "symbol_samples", "constraint",
                      "drive_scale"):
        if requested not in config:
            continue
        actual = _phy_value(mode, requested)
        if actual is not None and float(actual) != float(config[requested]):
            raise ValueError(f"candidate ignored {requested}={config[requested]!r}; reports {actual!r}")


def band_fill_warning(mode, config):
    """Warn when a config's realized tone grid leaves more than one tone's
    worth of the requested band unused at the top.

    This is exactly the failure mode that silently ran several N=16
    combinatorial hardware sweeps at 100 Bd (symbol_samples=480, the
    ``DEFAULT_CONFIG``/``mode_for`` default) instead of the intended 150 Bd
    (symbol_samples=320): the config's ``band_hi_hz`` was requested as
    3000 but the actual grid only reached 2200, a full 800 Hz short, with
    nothing surfaced to say so. ``symbol_samples`` (and therefore spacing
    and baud) is easy to omit from a ``--config`` string and fall back to a
    default that does not match the geometry a candidate was designed
    around, so flag the gap instead of running an unintentionally slower
    trial silently.
    """
    band_hi_hz = config.get("band_hi_hz")
    if band_hi_hz is None:
        return None
    geometry = getattr(mode, "geometry", None)
    geometry = geometry() if callable(geometry) else None
    if not isinstance(geometry, dict):
        return None
    actual_hi = geometry.get("band_hi_hz")
    spacing_hz = geometry.get("spacing_hz")
    if actual_hi is None or spacing_hz is None or spacing_hz <= 0:
        return None
    unused_hz = float(band_hi_hz) - float(actual_hi)
    if unused_hz > spacing_hz:
        return (f"requested band_hi_hz={band_hi_hz:g} but the tone grid "
                f"(symbol_samples={getattr(mode, 'symbol_samples', '?')}, "
                f"spacing={spacing_hz:g}Hz) only reaches {actual_hi:g}Hz "
                f"-- {unused_hz:.0f}Hz of the requested band is unused; "
                f"check symbol_samples was set intentionally")
    return None


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
        "net_bit_rate": (float(mode.net_bit_rate())
                          if hasattr(mode, "net_bit_rate") else None),
        "tx_sample_rate": getattr(mode, "tx_sample_rate", None),
        "rx_sample_rate": getattr(mode, "rx_sample_rate", None),
        "geometry": geometry if isinstance(geometry, dict) else None,
        "describe": describe() if callable(describe) else None,
        "band_fill_warning": band_fill_warning(mode, config),
    }


def symbol_error_rate(mode, payload, decoded):
    """Pre-FEC symbol error rate: hard tone decisions vs. the tones actually
    transmitted for `payload`, plus where the errors concentrate.

    Returns None when the mode does not expose ``tone_grid``/``hard_tones``
    (e.g. the offline-test FakeMode) or the decoder never synced.
    """
    tone_grid = getattr(mode, "tone_grid", None)
    hard = decoded.get("hard_tones")
    if tone_grid is None or hard is None:
        return None
    try:
        truth = np.asarray(tone_grid(payload))
        hard = np.asarray(hard)
    except Exception:
        return None
    if truth.shape != hard.shape:
        return None
    wrong = truth != hard
    total = wrong.size
    if total == 0:
        return None
    half = max(1, wrong.shape[0] // 2)
    return {
        "rate": float(np.sum(wrong)) / total,
        "errors": int(np.sum(wrong)),
        "symbols": int(total),
        "per_subband": [float(np.mean(wrong[:, j])) for j in range(wrong.shape[1])],
        "first_half_rate": float(np.mean(wrong[:half])),
        "second_half_rate": float(np.mean(wrong[half:])) if wrong.shape[0] > half else None,
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
    diagnostics = {k: v for k, v in decoded.items()
                    if isinstance(v, (str, int, float, bool, type(None)))}
    if isinstance(decoded.get("tone_snr_db_per_subband"), (list, tuple)):
        diagnostics["tone_snr_db_per_subband"] = [float(v) for v in decoded["tone_snr_db_per_subband"]]
    ser = symbol_error_rate(mode, payload, decoded)
    if ser is not None:
        diagnostics["ser"] = ser
    peak = float(np.max(np.abs(captured))) if len(captured) else 0.0
    diagnostics["rx_peak"] = peak
    record = {
        "direction": direction, "trial": trial, "payload_bytes": len(payload),
        "tx_samples": len(audio), "rx_samples": len(captured),
        "keyed_seconds": keyed, "wall_seconds": time.time() - t0,
        "exact": exact, "confidence": decoded.get("confidence"),
        "decoder": diagnostics,
        "error": error,
    }
    if peak >= 0.98:
        print(f"    WARNING: rx peak {peak:.3f} at/over clipping on {direction}")
    record["config_id"] = config_id
    if capture_dir is not None:
        filename = f"c{config_id:02d}_{getattr(mode, 'name', 'mode')}_{direction.replace('->', '_')}_{trial:03d}.npz"
        np.savez_compressed(capture_dir / filename, audio=captured,
                            payload=np.frombuffer(payload, dtype=np.uint8))
        record["capture"] = filename
    status = "PASS" if exact else "FAIL"
    print(f"    {direction} {trial}: {status} {len(payload)}B keyed={keyed:.2f}s "
          f"rx={len(captured)} conf={record['confidence']} "
          f"snr={diagnostics.get('tone_snr_db')} offset={diagnostics.get('offset_hz')}")
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
    parser.add_argument("--drive-scales", default="0.077,0.15,0.25",
                        help="drive_scale values; ignored for configs that set it")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--offline", action="store_true",
                        help="run the exact payload/encode/decode loop through rx_audio.downsample; no radios")
    args = parser.parse_args(argv)
    if args.trials < 1 or args.capture_tail < 0 or args.inter_trial < 0:
        parser.error("trials must be positive and timing intervals nonnegative")
    configs = args.config or [dict(DEFAULT_CONFIG)]
    drives = [float(value) for value in args.drive_scales.split(",") if value.strip()]
    expanded = []
    for config in configs:
        if "drive_scale" in config:
            expanded.append(config)
        else:
            expanded.extend({**config, "drive_scale": drive} for drive in drives)
    configs = expanded
    modes = []
    for cfg in configs:
        mode = make_mode(args.module, cfg)
        validate_mode(mode, cfg)
        warning = band_fill_warning(mode, cfg)
        if warning:
            print(f"WARNING: {warning}")
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
    print(f"FM MFSK sweep: {args.module}, {len(modes)} configs, {args.trials} trials/config")
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
    metadata["summary_by_config_direction"] = summary_by_config_direction(records)
    (args.output_dir / "result.json").write_text(json.dumps(metadata, indent=2, default=str) + "\n")
    print(f"wrote {args.output_dir / 'result.json'}")
    return 0 if metadata["summary"]["passed"] == metadata["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
