"""Monte Carlo sweep over the HC1P pilot arms.

Delivery rate comes from `whale.qualification.run_frame_trial` -- the same
trial path `scripts/benchmark_simulated_channels.py` uses -- so the arms are
scored by the shipped harness rather than by anything this experiment
invented.  The `baseline` arm transmits audio bit-identical to shipped HC1W
(asserted in `test_waveform.py`), so it is a live control rather than a
recorded number.

Examples:
    python experiments/hc1p_pilots/sweep.py --model watterson \\
        --watterson-preset mid_latitude_moderate \\
        --points 8 12 16 20 --trials 100
    python experiments/hc1p_pilots/sweep.py --model awgn \\
        --points 4 5 6 7 8 --trials 100 --arms baseline cohe-s24
    python experiments/hc1p_pilots/sweep.py --model watterson \\
        --watterson-preset mid_latitude_moderate --points 16 --drift-frames 20
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import sys
from dataclasses import astuple
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

from whale import framing, rx_audio
from whale.channel import WATTERSON_PRESETS
from whale.qualification import (channel_factory, channel_point_label,
                                 run_frame_trial, trial_seed)

from waveform import ARMS, Variant, VariantMode, arm_by_name

DEFAULT_SEED = 20260908
#: One mode id for every arm, so `trial_seed` gives each arm the *same*
#: channel realisations at the same point.  The arms are then compared on
#: identical noise and identical fading, which is what makes a difference of a
#: few frames in a hundred worth reading at all.
SHARED_MODE_ID = 18


def available_cpu_count() -> int:
    counter = getattr(os, "process_cpu_count", None)
    return (counter() if counter is not None else os.cpu_count()) or 1


def _run_trial_worker(task):
    """Rebuild the arm in the worker and run one trial.

    The variant travels as its full field tuple, not as a stride/detection
    pair: an arm that silently loses `track_weights`, `anchor_pilots` or
    `interleaver_stride` on the way into the pool is compared against itself,
    and every ablation reads as "no difference".
    """
    (fields, model, point, preset, master_seed, point_index, trial) = task
    mode = VariantMode(Variant(*fields), mode_id=SHARED_MODE_ID)
    seed = trial_seed(master_seed, SHARED_MODE_ID, point_index, trial)
    factory = channel_factory(model, point, watterson_preset=preset)
    payload_bytes = framing.AIR_HEADER_BYTES + mode.chunk_size
    return run_frame_trial(mode, factory(seed), seed, trial, "sim",
                           payload_bytes=payload_bytes)


def wilson_interval(passed: int, total: int, z: float = 1.959963984540054):
    proportion = passed / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z / denominator * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total))
    return centre - margin, centre + margin


def summarize(variant: Variant, trials) -> dict:
    total = len(trials)
    delivered = sum(t.decoded for t in trials)
    acquired = sum(t.outcome.value in ("decoded", "payload_failed")
                   for t in trials)
    low, high = wilson_interval(delivered, total)
    return {
        "arm": variant.name,
        "stride": variant.stride,
        "detection": variant.detection,
        "max_payload_bytes": variant.max_payload_bytes,
        "capacity_loss": round(variant.capacity_loss, 4),
        "trials": total,
        "delivered": delivered,
        "delivery_rate": delivered / total,
        "delivery_ci95": [low, high],
        "acquired": acquired,
        "errors": sum(t.outcome.value == "error" for t in trials),
    }


def run_drift_probe(variant: Variant, model: str, point: float, preset: str,
                    frames: int, master_seed: int) -> dict:
    """What the pilots actually see the channel do after the header fit.

    The mechanism this experiment blames is a channel estimate going stale,
    so the estimate's own drift is the evidence for or against it.  Reported
    for one arm at one channel point: the peak phase excursion the pilot
    track removes, and how far each carrier's power wanders from what the
    header measured.
    """
    mode = VariantMode(variant, mode_id=SHARED_MODE_ID)
    phase_drifts, power_ratios = [], []
    for trial in range(1, frames + 1):
        seed = trial_seed(master_seed, SHARED_MODE_ID, 0, trial)
        rng = np.random.default_rng(seed)
        payload = rng.integers(0, 256, variant.max_payload_bytes,
                               dtype=np.uint8).tobytes()
        channel = channel_factory(model, point, watterson_preset=preset)(seed)
        impaired = channel.process(np.asarray(mode.encode(payload), np.float32))
        drained = channel.drain()
        captured = rx_audio.downsample(np.concatenate((
            np.asarray(impaired.audio, np.float32),
            np.asarray(drained.audio, np.float32),
            np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, np.float32))))
        result = mode.decode(captured)
        if result.get("phase_drift_rad") is not None:
            phase_drifts.append(float(result["phase_drift_rad"]))
    return {
        "arm": variant.name,
        "frames": frames,
        "point_db": point,
        "phase_drift_rad": {
            "median": float(np.median(phase_drifts)) if phase_drifts else None,
            "max": float(np.max(phase_drifts)) if phase_drifts else None,
            "samples": len(phase_drifts),
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=("awgn", "watterson"), default="watterson")
    ap.add_argument("--watterson-preset", choices=sorted(WATTERSON_PRESETS),
                    default="mid_latitude_moderate")
    ap.add_argument("--points", type=float, nargs="+", required=True,
                    help="SNR/3 kHz points in dB")
    ap.add_argument("--trials", type=int, default=100)
    ap.add_argument("--arms", nargs="+",
                    default=[variant.name for variant in ARMS],
                    help="arm names; default is every arm")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--workers", type=int, default=available_cpu_count())
    ap.add_argument("--drift-frames", type=int, default=0,
                    help="also probe pilot-observed drift over this many frames")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent / "results.json")
    args = ap.parse_args(argv)

    variants = [arm_by_name(name) for name in args.arms]
    preset = args.watterson_preset if args.model == "watterson" else None
    executor = (concurrent.futures.ProcessPoolExecutor(args.workers)
                if args.workers > 1 else None)

    document = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "watterson_preset": preset,
        "points_db": args.points,
        "trials": args.trials,
        "seed": args.seed,
        "arms": [variant.describe() for variant in variants],
        "results": [],
    }

    try:
        for point_index, point in enumerate(args.points):
            label = channel_point_label(args.model, point,
                                        watterson_preset=preset
                                        or "mid_latitude_moderate")
            print(f"\n{label}")
            print(f"  {'arm':16s} {'payload':>8s} {'delivered':>10s} "
                  f"{'rate':>7s}  95% CI")
            for variant in variants:
                fields = astuple(variant)
                tasks = [(fields, args.model, point, preset, args.seed,
                          point_index, trial)
                         for trial in range(1, args.trials + 1)]
                if executor is None:
                    trials = [_run_trial_worker(task) for task in tasks]
                else:
                    trials = list(executor.map(_run_trial_worker, tasks))
                row = summarize(variant, trials)
                row["point_db"] = point
                row["point_label"] = label
                document["results"].append(row)
                low, high = row["delivery_ci95"]
                print(f"  {row['arm']:16s} {row['max_payload_bytes']:6d} B "
                      f"{row['delivered']:6d}/{row['trials']:<3d} "
                      f"{row['delivery_rate']:7.2f}  "
                      f"[{low:.2f}, {high:.2f}]")
    finally:
        if executor is not None:
            executor.shutdown()

    if args.drift_frames:
        document["drift"] = [
            run_drift_probe(variant, args.model, args.points[0], preset,
                            args.drift_frames, args.seed)
            for variant in variants if variant.stride
        ]
        print("\npilot-observed phase drift after the header fit")
        for row in document["drift"]:
            drift = row["phase_drift_rad"]
            if drift["median"] is None:
                print(f"  {row['arm']:16s} no frames decoded far enough")
                continue
            print(f"  {row['arm']:16s} median {drift['median']:.2f} rad, "
                  f"max {drift['max']:.2f} rad over {drift['samples']} frames")

    args.out.write_text(json.dumps(document, indent=2) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
