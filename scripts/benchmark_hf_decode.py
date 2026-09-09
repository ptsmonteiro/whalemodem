"""Reproducible CPU and allocation benchmark for the HF OFDM decoders.

Modelled on `scripts/benchmark_vf6_decode.py`.  Channel construction and
impairment are deliberately outside the timed regions: this measures
`HF7.decode` / `HF8.decode` on the same 12 kHz capture production feeds them,
nothing else.

The number to judge decode work on is the **realtime factor**: decode wall
time divided by the duration of the audio decoded.  The denominator is the
capture duration rather than `mode.airtime()` because it is the only quantity
defined for every case here -- `airtime()` is a constant that ignores the
negotiated lead, and the idle case has no frame in it at all.

The `idle` case is the one that matters most.  A 10 s buffer with no frame in
it is what the receive loop actually spends its time on: the poll loop
re-searches the whole retained buffer every 150 ms whether or not anything is
there, so idle cost sets the background load and the latency floor for
everything else.  It is also the case a bounded search window cannot help,
only a cheaper search.  It is reported first, and profiled by default.
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

from whale import framing, rx_audio
from whale.channel import AwgnChannel, SnrSpec
from whale.modes.hf7_mode import HF7
from whale.modes.hf8_mode import HF8


MODES = {"hf7": HF7, "hf8": HF8}
CASE_GROUPS = ("idle", "clean", "head", "noisy")


def frame_capture(mode, payload: bytes, *, head_seconds=None,
                  channel=None) -> np.ndarray:
    """Encode one frame and return the 12 kHz capture production would see.

    Matches `whale.qualification.run_frame_trial` and the mode tests exactly,
    including the FIR flush the decimator needs to emit the last samples.
    """
    tx = (mode.encode(payload) if head_seconds is None
          else mode.encode(payload, head_seconds=head_seconds))
    if channel is not None:
        # Impairment is realized here, once, so it is never inside a timed
        # region and every repetition decodes byte-identical audio.
        impaired = channel.process(tx)
        drained = channel.drain()
        tx = np.concatenate((np.asarray(impaired.audio, dtype=np.float32),
                             np.asarray(drained.audio, dtype=np.float32)))
    return rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))


def idle_capture(seconds: float, rng) -> np.ndarray:
    """A bounded no-frame noise buffer, matching `scripts/benchmark_rx.py`."""
    return rx_audio.downsample(rng.normal(
        0.0, 0.02, int(seconds * rx_audio.CAPTURE_SAMPLE_RATE)
    ).astype(np.float32))


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
        # The median is the headline, but a mean over the repetitions is what
        # survives a coarse process_time() quantum on platforms that have one.
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


def build_cases(mode, payload: bytes, args, rng) -> list[tuple[str, dict]]:
    """Return (case name, case record) pairs, idle first.

    Each record carries the capture and the decode keyword arguments; the
    caller times `mode.decode(capture, **decode_kwargs)`.
    """
    cases = []
    if "idle" in args.cases:
        cases.append(("idle", {
            "capture": idle_capture(args.idle_seconds, rng),
            "decode_kwargs": {},
            "expect_payload": None,
            "airtime_seconds": None,
        }))
    if "clean" in args.cases:
        cases.append(("clean", {
            "capture": frame_capture(mode, payload),
            "decode_kwargs": {},
            "expect_payload": payload,
            "airtime_seconds": mode.airtime(len(payload)),
        }))
    if "head" in args.cases:
        # head_seconds goes to both encode and decode: it is what puts more
        # than one entry in hf_lead.measured_candidates, and the multi-
        # candidate loop is where a decode that misses on its first boundary
        # pays for a second, third and fourth full OFDM acquisition.
        for head_seconds in args.head_seconds:
            cases.append((f"head_{head_seconds:.2f}", {
                "capture": frame_capture(mode, payload,
                                         head_seconds=head_seconds),
                "decode_kwargs": {"head_seconds": head_seconds},
                "expect_payload": payload,
                "airtime_seconds": mode.airtime(len(payload)),
            }))
    if "noisy" in args.cases:
        channel = AwgnChannel(48_000, SnrSpec(args.cn_db), args.seed)
        cases.append(("noisy", {
            "capture": frame_capture(mode, payload, channel=channel),
            "decode_kwargs": {},
            # A frame that does not decode is recorded, not raised on: an
            # exhausted candidate list followed by the whole-buffer fallback
            # is the most expensive path this decoder has, and it is one of
            # the things this benchmark exists to measure.
            "expect_payload": None,
            "airtime_seconds": mode.airtime(len(payload)),
        }))
    return cases


def measure_case(mode, record: dict, args) -> dict:
    capture = record["capture"]
    kwargs = record["decode_kwargs"]
    call = lambda: mode.decode(capture, **kwargs)
    verification = call()
    payload = verification.get("payload")
    if record["expect_payload"] is not None and payload != record["expect_payload"]:
        raise RuntimeError(f"{mode.name} benchmark capture did not decode")
    capture_seconds = len(capture) / mode.rx_sample_rate
    metrics = timed(call, args.warmup, args.repetitions)
    metrics["realtime_factor_median"] = (
        metrics["wall_median_seconds"] / capture_seconds)
    metrics["tracemalloc_peak_bytes"] = allocation_peak(call)
    return {
        "capture_samples": len(capture),
        "capture_seconds": capture_seconds,
        "airtime_seconds": record["airtime_seconds"],
        "head_seconds": kwargs.get("head_seconds"),
        "decoded": payload is not None,
        "crc_ok": bool(verification.get("crc_ok")),
        "metrics": metrics,
    }


def print_table(report: dict) -> None:
    header = (f"{'mode':5s} {'case':11s} {'audio s':>8s} {'wall ms':>9s} "
              f"{'rtf':>6s} {'cpu ms':>9s} {'peak MiB':>9s}  decoded")
    def row(name, case, entry):
        metrics = entry["metrics"]
        return (f"{name:5s} {case:11s} {entry['capture_seconds']:8.3f} "
                f"{metrics['wall_median_seconds'] * 1e3:9.1f} "
                f"{metrics['realtime_factor_median']:6.3f} "
                f"{metrics['cpu_median_seconds'] * 1e3:9.1f} "
                f"{metrics['tracemalloc_peak_bytes'] / 2**20:9.1f}  "
                f"{'yes' if entry['decoded'] else 'NO'}")

    # Idle first and on its own: it is the production pathology, the case a
    # bounded search window does not improve, and the one to read first.
    idle_rows = [(name, mode["cases"]["idle"])
                 for name, mode in report["modes"].items()
                 if "idle" in mode["cases"]]
    if idle_rows:
        print("\nidle search -- no frame present, whole buffer scanned")
        print(header)
        for name, entry in idle_rows:
            print(row(name, "idle", entry))
    print("\nframe decode")
    print(header)
    for name, mode in report["modes"].items():
        for case, entry in mode["cases"].items():
            if case != "idle":
                print(row(name, case, entry))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", default="hf7,hf8",
                        help="comma-separated modes: hf7, hf8")
    parser.add_argument("--cases", default=",".join(CASE_GROUPS),
                        help="comma-separated case groups: "
                             + ", ".join(CASE_GROUPS))
    parser.add_argument("--head-seconds", default="0.0,0.31,0.75,1.0",
                        help="negotiated lead durations for the head cases")
    parser.add_argument("--idle-seconds", type=float, default=10.0)
    # A single HF acquisition costs seconds, not milliseconds, so these
    # default far lower than the single-carrier benchmarks' counts.
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--profile-repetitions", type=int, default=1)
    parser.add_argument("--profile-lines", type=int, default=30)
    parser.add_argument("--profile-case", default="idle",
                        help="case to dump a cumulative cProfile for")
    parser.add_argument("--cn-db", type=float, default=20.0,
                        help="noisy-case AWGN SNR referenced to a 3 kHz "
                             "passband, the qualification convention")
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument("--output")
    args = parser.parse_args()
    if min(args.warmup, args.repetitions, args.profile_repetitions) < 1:
        parser.error("warmup and repetition counts must be positive")
    if args.idle_seconds <= 0:
        parser.error("--idle-seconds must be positive")
    args.mode = [name.strip() for name in args.mode.split(",") if name.strip()]
    for name in args.mode:
        if name not in MODES:
            parser.error(f"unknown mode {name!r}; choose from "
                         + ", ".join(MODES))
    args.cases = [name.strip() for name in args.cases.split(",") if name.strip()]
    for name in args.cases:
        if name not in CASE_GROUPS:
            parser.error(f"unknown case group {name!r}; choose from "
                         + ", ".join(CASE_GROUPS))
    args.head_seconds = [float(value) for value in args.head_seconds.split(",")
                         if value.strip()]

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
        "modes": {},
    }
    del report["settings"]["output"]

    for name in args.mode:
        mode = MODES[name]
        rng = np.random.default_rng(args.seed)
        # The full-capacity packet, matching whale.qualification's trials.
        payload = rng.integers(
            0, 256, mode.chunk_size + framing.AIR_HEADER_BYTES,
            dtype=np.uint8).tobytes()
        entry = {
            "payload_bytes": len(payload),
            "airtime_seconds": mode.airtime(len(payload)),
            "cases": {},
        }
        cases = build_cases(mode, payload, args, rng)
        for case, record in cases:
            entry["cases"][case] = measure_case(mode, record, args)
        for case, record in cases:
            if case == args.profile_case:
                entry[f"profile_{case}"] = profile_text(
                    lambda record=record: mode.decode(
                        record["capture"], **record["decode_kwargs"]),
                    args.profile_repetitions, args.profile_lines)
        report["modes"][name] = entry

    encoded = json.dumps(report, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        print_table(report)
    else:
        print_table(report)
        print()
        print(encoded)


if __name__ == "__main__":
    main()
