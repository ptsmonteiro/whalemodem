"""Reproducible CPU and allocation benchmark for the production VF6 decoder.

Channel construction and impairment are deliberately outside timed regions.
The benchmark feeds the same 12 kHz capture used by production to ``VF6.decode``
and separately times its 12-to-48 kHz reconstruction and 48 kHz demodulator.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import platform
import pstats
import statistics
import sys
import time
import tracemalloc

import numpy as np
import scipy
from scipy.signal import resample_poly

from whale import rx_audio
from whale.fm_channel import ComplexFmChannel
from whale.modes import vf6
from whale.modes.vf6_mode import VF6


def capture(payload: bytes, kind: str, cn_db: float, seed: int) -> np.ndarray:
    tx = VF6.encode(payload)
    if kind == "clean":
        audio = tx
    else:
        channel = ComplexFmChannel.from_profile(
            48_000, "flat_nbfm", carrier_to_noise_db=cn_db, seed=seed)
        impaired = channel.process(tx)
        drained = channel.drain()
        audio = np.concatenate((impaired.audio, drained.audio))
    # Match whale.qualification.run_frame_trial exactly, including FIR flush.
    return rx_audio.downsample(np.concatenate((
        audio, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, np.float32))))


def timed(call, warmup: int, repetitions: int) -> dict:
    for _ in range(warmup):
        call()
    wall, cpu = [], []
    for _ in range(repetitions):
        cpu_start = time.process_time()
        wall_start = time.perf_counter()
        call()
        wall.append(time.perf_counter() - wall_start)
        cpu.append(time.process_time() - cpu_start)
    return {
        "wall_seconds": wall,
        "cpu_seconds": cpu,
        "wall_median_seconds": statistics.median(wall),
        "wall_mean_seconds": statistics.mean(wall),
        "wall_stdev_seconds": statistics.stdev(wall) if len(wall) > 1 else 0.0,
        "cpu_median_seconds": statistics.median(cpu),
        # Windows process_time() commonly advances in 15.625 ms quanta.  The
        # mean across repetitions is therefore more useful than its median
        # for short stages such as the polyphase reconstruction.
        "cpu_mean_seconds": statistics.mean(cpu),
        "cpu_total_seconds": sum(cpu),
    }


def allocation_peak(call) -> int:
    tracemalloc.start()
    call()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def profile_text(call, repetitions: int, lines: int) -> str:
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(repetitions):
        call()
    profiler.disable()
    output = io.StringIO()
    pstats.Stats(profiler, stream=output).strip_dirs().sort_stats(
        pstats.SortKey.CUMULATIVE).print_stats(lines)
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--repetitions", type=int, default=15)
    parser.add_argument("--profile-repetitions", type=int, default=3)
    parser.add_argument("--profile-lines", type=int, default=30)
    parser.add_argument("--cn-db", type=float, default=40.0)
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--output")
    args = parser.parse_args()
    if min(args.warmup, args.repetitions, args.profile_repetitions) < 1:
        parser.error("warmup and repetition counts must be positive")

    rng = np.random.default_rng(args.seed)
    payload = rng.integers(0, 256, vf6.MAX_PAYLOAD_BYTES,
                           dtype=np.uint8).tobytes()
    report = {
        "environment": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": sys.version,
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "logical_cpus": os.cpu_count(),
        },
        "settings": vars(args).copy(),
        "frame_seconds": vf6.FRAME_SECONDS,
        "payload_bytes": len(payload),
        "cases": {},
    }
    del report["settings"]["output"]

    for kind in ("clean", "flat_nbfm"):
        samples_12k = capture(payload, kind, args.cn_db, args.seed)
        samples_48k = resample_poly(samples_12k.astype(np.float64), 4, 1)
        full_call = lambda: VF6.decode(samples_12k)
        upsample_call = lambda: resample_poly(
            samples_12k.astype(np.float64), 4, 1)
        demod_call = lambda: vf6.demodulate(samples_48k)
        verification = full_call()
        if verification.get("payload") != payload:
            raise RuntimeError(f"{kind} benchmark capture did not decode")
        metrics = {}
        for name, call in (("full_decode", full_call),
                           ("reconstruct_12_to_48k", upsample_call),
                           ("demodulate_48k", demod_call)):
            measurement = timed(call, args.warmup, args.repetitions)
            measurement["realtime_factor_median"] = (
                measurement["wall_median_seconds"] / vf6.FRAME_SECONDS)
            measurement["tracemalloc_peak_bytes"] = allocation_peak(call)
            metrics[name] = measurement
        report["cases"][kind] = {
            "capture_samples_12k": len(samples_12k),
            "decoded": True,
            "rs_corrected_bytes": verification["rs_corrected_bytes"],
            "median_carrier_snr_db": float(np.median(
                verification["carrier_snr_db"])),
            "metrics": metrics,
            "profile_full_decode": profile_text(
                full_call, args.profile_repetitions, args.profile_lines),
        }

    encoded = json.dumps(report, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    main()
