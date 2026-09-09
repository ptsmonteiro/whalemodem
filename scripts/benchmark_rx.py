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
from whale.modes.hc1w_mode import HC1W
from whale.modes.hf7_mode import HF7
from whale.modes.hf8_mode import HF8
from whale.modes.hf9_mode import HF9
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
    # The HF OFDM modes dominate this benchmark's runtime: each attempt
    # correlates the whole buffer once per CFO hypothesis, which measured
    # ~2.5-2.7 s per attempt on a 10 s idle buffer, so the default 20 repeats
    # adds minutes. That figure is a baseline of the current search, not a
    # property of the modes; use a smaller --repeats while it stands.
    for mode in (*afsk.PROFILES, VF3, HC0, HC1W, HF9, HF8, HF7):
        measure(f"{mode.name} decoder", lambda mode=mode: mode.decode(received),
                args.repeats)


if __name__ == "__main__":
    main()
