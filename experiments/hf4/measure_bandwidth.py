"""Reproducible statistical 99%-power occupied-bandwidth campaign for HF4.

The measurement uses the complete keyed waveform returned by ``hf4.modulate``
(lead-in, OFDM frame, and tail).  For each payload class, the largest result
from N independent random payloads is a distribution-free, one-sided bound on
the population p-quantile with confidence ``1 - p**N``.  Thus N=300 makes the
sample maximum a 95.1% upper confidence bound on the population 99th
percentile, without assuming a parametric bandwidth distribution.

Mirrors `experiments/hf3/measure_bandwidth.py`, parameterized for HF4's own
ceiling: the project owner's brief for this mode overrides the project's
default 2,300 Hz HF ceiling with a 300-2,700 Hz window (2,400 Hz wide), so
both the top ceiling and a floor check are reported here.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import scipy

from . import hf4


def occupied_bandwidth_99(audio: np.ndarray, sample_rate: int) -> dict:
    """Return the equal-tail interval containing 99% of FFT-bin power."""
    audio = np.asarray(audio, dtype=np.float64)
    power = np.abs(np.fft.rfft(audio)) ** 2
    cumulative = np.cumsum(power)
    total = float(cumulative[-1])
    frequencies = np.fft.rfftfreq(len(audio), 1.0 / sample_rate)
    low_index = int(np.searchsorted(cumulative, total * 0.005))
    high_index = int(np.searchsorted(cumulative, total * 0.995))
    low_hz = float(frequencies[low_index])
    high_hz = float(frequencies[high_index])
    return {
        "low_hz": low_hz,
        "high_hz": high_hz,
        "width_hz": high_hz - low_hz,
        "fft_bins": len(power),
        "resolution_hz": sample_rate / len(audio),
    }


def quantile_upper_bound_confidence(trials: int, quantile: float) -> float:
    """Coverage of the sample maximum as an upper bound on a quantile."""
    return 1.0 - quantile ** trials


def run_campaign(*, trials: int, seed: int,
                 payload_lengths: tuple[int, ...]) -> dict:
    if trials < 1:
        raise ValueError("trials must be positive")
    rng = np.random.default_rng(seed)
    payload_classes = []
    for payload_len in payload_lengths:
        if not 0 <= payload_len <= hf4.MAX_PAYLOAD_BYTES:
            raise ValueError(f"invalid payload length: {payload_len}")
        measurements = []
        for trial in range(trials):
            payload = rng.integers(0, 256, payload_len, dtype=np.uint8).tobytes()
            measurement = occupied_bandwidth_99(
                hf4.modulate(payload), hf4.SAMPLE_RATE)
            measurement["trial"] = trial
            measurements.append(measurement)
        widths = np.asarray([row["width_hz"] for row in measurements])
        lows = np.asarray([row["low_hz"] for row in measurements])
        highs = np.asarray([row["high_hz"] for row in measurements])
        payload_classes.append({
            "payload_bytes": payload_len,
            "trials": trials,
            "width_hz": {
                "minimum": float(np.min(widths)),
                "median": float(np.median(widths)),
                "maximum": float(np.max(widths)),
            },
            "low_hz": {
                "minimum": float(np.min(lows)),
                "median": float(np.median(lows)),
                "maximum": float(np.max(lows)),
            },
            "high_hz": {
                "minimum": float(np.min(highs)),
                "median": float(np.median(highs)),
                "maximum": float(np.max(highs)),
            },
            "population_quantile": 0.99,
            "upper_confidence_bound_hz": float(np.max(widths)),
            "upper_bound_confidence": quantile_upper_bound_confidence(
                trials, 0.99),
            "high_edge_upper_confidence_bound_hz": float(np.max(highs)),
            "low_edge_lower_confidence_bound_hz": float(np.min(lows)),
            "measurements": measurements,
        })
    floor_hz = 300.0
    ceiling_hz = 2_700.0
    largest_bound = max(
        row["upper_confidence_bound_hz"] for row in payload_classes)
    worst_high_edge = max(
        row["high_edge_upper_confidence_bound_hz"] for row in payload_classes)
    worst_low_edge = min(
        row["low_edge_lower_confidence_bound_hz"] for row in payload_classes)
    passes_ceiling = worst_high_edge < ceiling_hz
    passes_floor = worst_low_edge > floor_hz
    return {
        "schema": "whalemodem.hf4_occupied_bandwidth.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": {
            "signal": "complete keyed hf4.modulate waveform",
            "power_spectrum": "squared magnitude of unwindowed real FFT",
            "occupied_interval": "0.5%-99.5% cumulative FFT-bin power",
            "statistical_bound": (
                "sample maximum is a distribution-free one-sided upper "
                "confidence bound on the population 99th percentile "
                "(sample minimum analogously bounds the low edge)"),
            "confidence_formula": "1 - 0.99**trials",
        },
        "sample_rate_hz": hf4.SAMPLE_RATE,
        "seed": seed,
        "payload_classes": payload_classes,
        "floor_hz": floor_hz,
        "ceiling_hz": ceiling_hz,
        "largest_width_upper_confidence_bound_hz": largest_bound,
        "worst_high_edge_upper_confidence_bound_hz": worst_high_edge,
        "worst_low_edge_lower_confidence_bound_hz": worst_low_edge,
        "passes_ceiling": passes_ceiling,
        "passes_floor": passes_floor,
        "passes": passes_ceiling and passes_floor,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "git_commit": _git("rev-parse", "HEAD"),
            "git_dirty": bool(_git("status", "--porcelain")),
        },
    }


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], check=False, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20260901)
    parser.add_argument("--payload-lengths", type=int, nargs="+", default=[
        hf4.MAX_PAYLOAD_BYTES // 2, hf4.MAX_PAYLOAD_BYTES])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = run_campaign(
        trials=args.trials, seed=args.seed,
        payload_lengths=tuple(args.payload_lengths))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "out": str(args.out),
        "passes": result["passes"],
        "worst_high_edge_upper_confidence_bound_hz":
            result["worst_high_edge_upper_confidence_bound_hz"],
        "worst_low_edge_lower_confidence_bound_hz":
            result["worst_low_edge_lower_confidence_bound_hz"],
    }, indent=2))
    return 0 if result["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
