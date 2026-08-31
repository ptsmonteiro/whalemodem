"""Bounded screen of the HC2b QPSK frame-length matrix.

This is candidate selection, not mode qualification.  It uses the canonical
frame trial/channel code, 20 trials per point by default, and records every
trial so interesting boundaries can be rerun at promotion-sized counts.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from whale.qualification import (channel_factory, channel_point_label,
                                 run_frame_trial, trial_seed)
from whale.trials import TrialRun

from .hc2b import MODES


DEFAULT_POINTS = (9.0, 14.0, 19.0, 24.0)
DEFAULT_PRESETS = ("mid_latitude_quiet", "mid_latitude_moderate",
                   "mid_latitude_disturbed")


def wilson(passed, total, z=1.959963984540054):
    p = passed / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z / denominator * math.sqrt(
        p * (1 - p) / total + z * z / (4 * total * total))
    return [centre - margin, centre + margin]


def run(args):
    records = []
    summaries = []
    point_index = 0
    for preset in args.presets:
        for point in args.points:
            factory = channel_factory("watterson", point,
                                      watterson_preset=preset)
            for mode in MODES:
                label = channel_point_label(
                    "watterson", point, watterson_preset=preset)
                # Candidate comparison is paired: every length sees the
                # same channel seed for a given point/trial.  Qualification
                # normally includes mode_id in this derivation to make mode
                # campaigns independent; that is the opposite of what this
                # controlled experiment needs.
                trials = []
                for trial in range(1, args.trials + 1):
                    seed = trial_seed(args.seed, 19_999, point_index, trial)
                    trials.append(run_frame_trial(
                        mode, factory(seed), seed, trial, label))
                records.extend(trials)
                delivered = sum(trial.decoded for trial in trials)
                acquired = sum(trial.outcome.value in
                               ("decoded", "payload_failed") for trial in trials)
                variant = mode.variant
                row = {
                    "mode": mode.name, "preset": preset, "snr_db": point,
                    "payload_symbols": variant.payload_symbols,
                    "payload_bytes": variant.max_payload_bytes,
                    "frame_seconds": variant.frame_seconds,
                    "nominal_payload_bps": (variant.max_payload_bytes * 8
                                            / variant.frame_seconds),
                    "delivered": delivered, "acquired": acquired,
                    "trials": len(trials),
                    "delivery_wilson_95": wilson(delivered, len(trials)),
                }
                row["screen_goodput_bps"] = (
                    row["nominal_payload_bps"] * delivered / len(trials))
                summaries.append(row)
                print(f"{preset} {point:g} dB {mode.name}: "
                      f"{delivered}/{len(trials)}, "
                      f"{row['nominal_payload_bps']:.0f} payload bit/s")
            point_index += 1
    serialized_trials = TrialRun(
        channel={"type": "watterson", "presets": list(args.presets),
                 "points_db": list(args.points)},
        trials=records, seed=args.seed,
        metadata={"benchmark": "hc2b_bounded_candidate_screen"},
    ).to_dict()["trials"]
    artifact = {
        "schema": "whalemodem.hc2b-screen.v1",
        "qualification_evidence": False,
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed, "trials_per_point": args.trials,
        "summaries": summaries, "trials": serialized_trials,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2, allow_nan=False) + "\n")
    return summaries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path,
                        default=Path("logs/scratch/hc2b_screen.json"))
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument("--points", type=float, nargs="+", default=DEFAULT_POINTS)
    parser.add_argument("--presets", nargs="+", default=DEFAULT_PRESETS)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
