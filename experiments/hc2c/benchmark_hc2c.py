"""Paired bounded comparison of 1.521 s QPSK with and without pilots."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from whale.qualification import (channel_factory, channel_point_label,
                                 run_frame_trial, trial_seed)
from whale.trials import TrialRun

from experiments.hc2b.hc2b import MODES as HC2B_MODES
from .hc2c import MODE as PILOT_MODE


MODES = (HC2B_MODES[2], PILOT_MODE)
# Rounded 3 kHz equivalents of the historical 0/5/10/15 dB full-Nyquist
# screen, preserving approximately the same physical noise levels.
POINTS = (9.0, 14.0, 19.0, 24.0)
PRESETS = ("mid_latitude_quiet", "mid_latitude_moderate",
           "mid_latitude_disturbed")


def wilson(passed, total, z=1.959963984540054):
    p = passed / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z / denominator * math.sqrt(
        p * (1 - p) / total + z * z / (4 * total * total))
    return [centre - margin, centre + margin]


def run(args):
    records, summaries = [], []
    point_index = 0
    for preset in args.presets:
        for point in args.points:
            factory = channel_factory("watterson", point,
                                      watterson_preset=preset)
            label = channel_point_label("watterson", point,
                                        watterson_preset=preset)
            for mode in MODES:
                trials = []
                for trial in range(1, args.trials + 1):
                    seed = trial_seed(args.seed, 21_999, point_index, trial)
                    trials.append(run_frame_trial(
                        mode, factory(seed), seed, trial, label))
                records.extend(trials)
                delivered = sum(t.decoded for t in trials)
                nominal_bps = ((mode.chunk_size + 10) * 8
                               / mode.airtime(mode.chunk_size + 10))
                row = {
                    "mode": mode.name, "preset": preset, "snr_3khz_db": point,
                    "payload_bytes": mode.chunk_size + 10,
                    "frame_seconds": mode.airtime(mode.chunk_size + 10),
                    "delivered": delivered, "trials": len(trials),
                    "delivery_wilson_95": wilson(delivered, len(trials)),
                    "nominal_payload_bps": nominal_bps,
                    "screen_goodput_bps": nominal_bps * delivered / len(trials),
                }
                summaries.append(row)
                print(f"{preset} {point:g} dB {mode.name}: "
                      f"{delivered}/{len(trials)}, "
                      f"{row['screen_goodput_bps']:.0f} observed bit/s")
            point_index += 1
    serialized = TrialRun(
        channel={"type": "watterson", "presets": list(args.presets),
                 "snr_kind": "passband_3khz", "points_db": list(args.points)},
        trials=records, seed=args.seed,
        metadata={"benchmark": "hc2c_paired_pilot_screen"},
    ).to_dict()["trials"]
    artifact = {
        "schema": "whalemodem.hc2c-paired-screen.v1",
        "qualification_evidence": False,
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed, "trials_per_point": args.trials,
        "summaries": summaries, "trials": serialized,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path,
                        default=Path("logs/scratch/hc2c_paired_screen.json"))
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument("--points", type=float, nargs="+", default=POINTS)
    parser.add_argument("--presets", nargs="+", default=PRESETS)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
