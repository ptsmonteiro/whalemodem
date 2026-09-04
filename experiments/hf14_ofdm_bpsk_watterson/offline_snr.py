"""Offline AWGN sweep over HF14's retained IC-7300 -> IC-705 captures.

This file never imports the hardware transport and cannot key a radio.  It
reconstructs each trial's deterministic payload, estimates signal and
pre-existing receiver-noise power from the recording, adds seeded real white
noise, and passes the impaired capture to the original OFDM demodulator.

The requested SNR is *incremental*: estimated signal power divided by added
full-Nyquist noise power.  ``estimated_total_snr_db`` also includes the
recording's estimated baseline noise and is the appropriate x-axis when
comparing captures with different receiver-noise floors.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import zlib
from pathlib import Path

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from experiments.hf10_ofdm49_v6 import ofdm49_v6 as ofdm

DEFAULT_INPUTS = (
    "rank1_fft240_cp36_pi8",
    "rank2_fft240_cp36_pi4",
    "rank3_fft400_cp50_pi8",
    "rank4_fft600_cp38_pi8",
    "control_fft120_cp36_pi8",
)
DEFAULT_SNRS = (30.0, 24.0, 20.0, 16.0, 12.0, 8.0, 4.0, 0.0, -4.0)
DEFAULT_MASTER_SEED = 20260903
Z_95 = 1.959963984540054


def build_mode(config: dict) -> ofdm.OFDM49Mode:
    return ofdm.OFDM49Mode(
        fft_size=config["fft_size"], cp_len=config["cp_len"],
        active_bins=tuple(config["active_bins"]),
        bits_per_symbol=config["bits_per_symbol"],
        packet_bytes=config["packet_bytes"],
        pilot_interval=config["pilot_interval"],
        pilot_comb_stride=config["pilot_comb_stride"],
        equalizer=config["equalizer"],
        edge_guard_bins=config["edge_guard_bins"],
        edge_taper=config["edge_taper"],
        n_preamble_symbols=config["n_preamble_symbols"],
        fec_rate=config["fec_rate"])


def estimate_powers(capture: np.ndarray, frame_samples: int,
                    guard_samples: int) -> dict:
    """Estimate frame signal and baseline-noise powers from one capture.

    The highest-power frame-length window locates the received keying without
    relying on a successful decode.  Baseline noise is measured outside that
    window plus a one-symbol guard.  Signal power is window power minus the
    baseline, preventing the receiver noise already present in the capture
    from being counted twice as signal.
    """
    x = np.asarray(capture, dtype=np.float64)
    if x.ndim != 1 or frame_samples <= 0 or len(x) < frame_samples:
        raise ValueError("capture must be mono and at least one frame long")
    if guard_samples < 0:
        raise ValueError("guard_samples must be non-negative")
    window_power = np.convolve(x * x, np.ones(frame_samples), mode="valid")
    window_power /= frame_samples
    start = int(np.argmax(window_power))
    stop = start + frame_samples
    keep = np.ones(len(x), dtype=bool)
    keep[max(0, start - guard_samples):min(len(x), stop + guard_samples)] = False
    if not np.any(keep):
        raise ValueError("capture has no off-frame samples for noise estimate")
    baseline_noise_power = float(np.mean(x[keep] ** 2))
    frame_power = float(window_power[start])
    signal_power = frame_power - baseline_noise_power
    if not signal_power > 0:
        raise ValueError("estimated signal power is not positive")
    baseline_snr_db = (math.inf if baseline_noise_power == 0 else
                       10.0 * math.log10(signal_power / baseline_noise_power))
    return {
        "frame_start_sample": start,
        "frame_stop_sample": stop,
        "frame_power": frame_power,
        "baseline_noise_power": baseline_noise_power,
        "signal_power": signal_power,
        "estimated_baseline_snr_db": baseline_snr_db,
    }


def add_awgn(capture: np.ndarray, signal_power: float, added_snr_db: float,
             seed: int) -> tuple[np.ndarray, float, float]:
    """Return impaired float32 audio, realized noise power, realized SNR."""
    if not signal_power > 0 or not math.isfinite(signal_power):
        raise ValueError("signal_power must be finite and positive")
    if not math.isfinite(added_snr_db):
        raise ValueError("added_snr_db must be finite")
    requested_power = signal_power / (10.0 ** (added_snr_db / 10.0))
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, math.sqrt(requested_power), len(capture))
    realized_power = float(np.mean(noise ** 2))
    realized_snr = 10.0 * math.log10(signal_power / realized_power)
    return (np.asarray(capture, dtype=np.float64) + noise).astype(np.float32), \
        realized_power, realized_snr


def wilson(passed: int, total: int) -> list[float]:
    if total == 0:
        return [0.0, 1.0]
    p = passed / total
    denominator = 1 + Z_95 * Z_95 / total
    centre = (p + Z_95 * Z_95 / (2 * total)) / denominator
    margin = Z_95 / denominator * math.sqrt(
        p * (1 - p) / total + Z_95 * Z_95 / (4 * total * total))
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def _payload(seed: int, trial: int, size: int) -> bytes:
    rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
    return rng.integers(0, 256, size, dtype=np.uint8).tobytes()


def _noise_seed(master: int, name: str, snr: float, trial: int, repeat: int) -> int:
    point = zlib.crc32(f"{name}@{snr:g}".encode()) & 0xFFFFFFFF
    return int(np.random.SeedSequence(
        [master, point, trial, repeat]).generate_state(1, dtype=np.uint32)[0])


def run_input(path: Path, snrs: tuple[float, ...], repeats: int,
              master_seed: int) -> dict:
    source = json.loads((path / "result.json").read_text())
    mode = build_mode(source["config"])
    captures = []
    for trial in source["trials"]:
        capture_file = trial.get("capture_file")
        if not capture_file:
            raise ValueError(f"trial {trial['trial']} has no capture_file")
        audio = np.load(path / "captures" / capture_file)
        powers = estimate_powers(
            audio, mode.total_ofdm_symbols() * mode.symbol_len, mode.symbol_len)
        captures.append((trial, audio, powers))

    points = []
    for snr in snrs:
        records = []
        for trial, audio, powers in captures:
            payload = _payload(source["seed"], trial["trial"], mode.max_payload_bytes)
            for repeat in range(1, repeats + 1):
                seed = _noise_seed(master_seed, path.name, snr, trial["trial"], repeat)
                impaired, added_power, realized_added_snr = add_awgn(
                    audio, powers["signal_power"], snr, seed)
                decoded = mode.demodulate(impaired)
                outcome = ("decoded" if decoded.get("payload") == payload else
                           "no_sync" if not decoded.get("synced") else
                           "crc_fail" if not decoded.get("crc_ok") else
                           "payload_mismatch")
                total_noise = powers["baseline_noise_power"] + added_power
                total_snr = 10.0 * math.log10(powers["signal_power"] / total_noise)
                records.append({
                    "source_trial": trial["trial"], "noise_repeat": repeat,
                    "noise_seed": seed, "outcome": outcome,
                    "realized_added_snr_db": realized_added_snr,
                    "estimated_total_snr_db": total_snr,
                    "decoder_channel_snr_db": decoded.get("channel_snr_db"),
                    "confidence": decoded.get("confidence"),
                })
        passed = sum(r["outcome"] == "decoded" for r in records)
        points.append({
            "requested_added_snr_db": snr,
            "estimated_total_snr_db_mean": float(np.mean(
                [r["estimated_total_snr_db"] for r in records])),
            "decoded": passed, "total": len(records),
            "success_rate": passed / len(records),
            "wilson_95": wilson(passed, len(records)), "trials": records,
        })

    baseline_snrs = [p["estimated_baseline_snr_db"] for _, _, p in captures]
    return {
        "name": path.name, "source_result": str(path / "result.json"),
        "source_capture_count": len(captures), "noise_repeats_per_capture": repeats,
        "config": source["config"],
        "power_estimator": {
            "frame_samples": mode.total_ofdm_symbols() * mode.symbol_len,
            "guard_samples": mode.symbol_len,
            "signal_definition": "maximum frame-window mean square minus off-frame baseline noise power",
            "noise_definition": "off-frame mean square outside frame plus one-symbol guard",
            "estimated_baseline_snr_db_mean": float(np.mean(baseline_snrs)),
            "estimated_baseline_snr_db_min": float(np.min(baseline_snrs)),
            "estimated_baseline_snr_db_max": float(np.max(baseline_snrs)),
        },
        "points": points,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware-dir", type=Path, default=Path(__file__).parent / "hardware")
    parser.add_argument("--input", action="append", dest="inputs")
    parser.add_argument("--snrs", type=float, nargs="+", default=list(DEFAULT_SNRS))
    parser.add_argument("--noise-repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=DEFAULT_MASTER_SEED)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "offline_snr.json")
    args = parser.parse_args(argv)
    if args.noise_repeats <= 0:
        parser.error("--noise-repeats must be positive")
    names = tuple(args.inputs or DEFAULT_INPUTS)
    results = []
    for name in names:
        print(f"sweeping {name} ...", flush=True)
        result = run_input(args.hardware_dir / name, tuple(args.snrs),
                           args.noise_repeats, args.seed)
        results.append(result)
        summary = " ".join(
            f"{p['requested_added_snr_db']:g}dB:{p['decoded']}/{p['total']}"
            for p in result["points"])
        print(f"  {summary}", flush=True)
    artifact = {
        "experiment": "hf14 retained-radio-capture offline AWGN sweep",
        "offline_only": True,
        "sample_rate": ofdm.DESIGN_RATE,
        "master_seed": args.seed,
        "requested_snr_definition": "estimated captured signal power / added real full-Nyquist AWGN power",
        "estimated_total_snr_definition": "estimated captured signal power / (off-frame baseline noise power + added AWGN power)",
        "limitations": [
            "post-capture noise injection does not exercise RF AGC, receiver sensitivity, or weak-signal front-end behavior",
            "off-frame receiver noise is treated as additive and stationary",
            "the maximum-energy frame window is an estimator, not a calibrated RF power measurement",
        ],
        "inputs": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2, allow_nan=False) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
