"""Benchmark the shared decimator and each production receive decoder.

The input is a bounded idle-noise buffer, matching the repeated no-frame
search that dominates background receive cost.  Run this on development and
minimum-target hardware when changing the RX path::

    python scripts/benchmark_rx.py --seconds 10 --repeats 20
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from whale import afsk, rx_audio
from whale.modes.hc0_mode import HC0
from whale.modes.hc1_mode import HC1
from whale.modes.vf3_mode import VF3


def measure(label, operation, repeats):
    started = time.perf_counter()
    for _ in range(repeats):
        operation()
    mean_ms = (time.perf_counter() - started) * 1_000.0 / repeats
    print(f"{label:24s} {mean_ms:8.2f} ms/attempt")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    if args.seconds <= 0 or args.repeats <= 0:
        parser.error("--seconds and --repeats must be positive")

    rng = np.random.default_rng(20260828)
    captured = rng.normal(
        0.0, 0.02, int(args.seconds * rx_audio.CAPTURE_SAMPLE_RATE)
    ).astype(np.float32)
    received = rx_audio.downsample(captured)

    print(f"buffer={args.seconds:g}s, repeats={args.repeats}")
    measure("48->12 kHz decimator", lambda: rx_audio.downsample(captured),
            args.repeats)
    for mode in (*afsk.PROFILES, VF3, HC0, HC1):
        measure(f"{mode.name} decoder", lambda mode=mode: mode.decode(received),
                args.repeats)


if __name__ == "__main__":
    main()
