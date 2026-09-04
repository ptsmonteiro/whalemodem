"""Reproducible statistical 99%-power occupied-bandwidth campaign for HF2.

The measurement uses the complete keyed waveform returned by ``hf2.modulate``
(lead-in, OFDM frame, and tail).  For each payload class, the largest result
from N independent random payloads is a distribution-free, one-sided bound on
the population p-quantile with confidence ``1 - p**N``.  Thus N=300 makes the
sample maximum a 95.1% upper confidence bound on the population 99th
percentile, without assuming a parametric bandwidth distribution.
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

from . import hf2


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
        if not 0 <= payload_len <= hf2.MAX_PAYLOAD_BYTES:
            raise ValueError(f"invalid payload length: {payload_len}")
        measurements = []
        for trial in range(trials):
            payload = rng.integers(0, 256, payload_len, dtype=np.uint8).tobytes()
            measurement = occupied_bandwidth_99(
                hf2.modulate(payload), hf2.SAMPLE_RATE)
            measurement["trial"] = trial
            measurements.append(measurement)
        widths = np.asarray([row["width_hz"] for row in measurements])
        payload_classes.append({
            "payload_bytes": payload_len,
            "trials": trials,
            "width_hz": {
                "minimum": float(np.min(widths)),
                "median": float(np.median(widths)),
                "maximum": float(np.max(widths)),
            },
            "population_quantile": 0.99,
            "upper_confidence_bound_hz": float(np.max(widths)),
            "upper_bound_confidence": quantile_upper_bound_confidence(
                trials, 0.99),
            "measurements": measurements,
        })
    ceiling_hz = 2_300.0
    largest_bound = max(
        row["upper_confidence_bound_hz"] for row in payload_classes)
    return {
        "schema": "whalemodem.hf2_occupied_bandwidth.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": {
            "signal": "complete keyed hf2.modulate waveform",
            "power_spectrum": "squared magnitude of unwindowed real FFT",
            "occupied_interval": "0.5%-99.5% cumulative FFT-bin power",
            "statistical_bound": (
                "sample maximum is a distribution-free one-sided upper "
                "confidence bound on the population 99th percentile"),
            "confidence_formula": "1 - 0.99**trials",
        },
        "sample_rate_hz": hf2.SAMPLE_RATE,
        "seed": seed,
        "payload_classes": payload_classes,
        "ceiling_hz": ceiling_hz,
        "largest_upper_confidence_bound_hz": largest_bound,
        "passes": largest_bound < ceiling_hz,
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
        hf2.MAX_PAYLOAD_BYTES // 2, hf2.MAX_PAYLOAD_BYTES])
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
        "largest_upper_confidence_bound_hz":
            result["largest_upper_confidence_bound_hz"],
    }, indent=2))
    return 0 if result["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
