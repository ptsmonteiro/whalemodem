"""Monte Carlo frame sweep for HC2, run the same way HC0/HC1 were qualified.

HC2 has no on-air mode ID and is not in any `ModeRegistry`
(`experiments/hc2/hc2_mode.py`), so it cannot go through
`scripts/benchmark_simulated_channels.py` directly -- that script resolves
modes out of a channel policy's registry. This reimplements just enough of
its Monte Carlo loop, calling the same `whale.qualification` trial runner
and the same Wilson-interval summary, against `whale.channel.WATTERSON_PRESETS`
and AWGN, so the numbers are directly comparable to
`logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-30/`.

Usage:
    python experiments/hc2/benchmark_hc2.py --out logs/scratch/hc2_sweep.json
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hc2_mode import HC2  # noqa: E402

from whale.qualification import (channel_factory, channel_point_label,  # noqa: E402
                                 run_frame_trials)
from whale.trials import TrialRun  # noqa: E402

POINTS = (-5, 0, 5, 10, 15, 20)
PRESETS = ("mid_latitude_quiet", "mid_latitude_moderate", "mid_latitude_disturbed")
TRIALS = 100
SEED = 20260830


def wilson_interval(passed, total, z=1.959963984540054):
    proportion = passed / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z / denominator * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total))
    return centre - margin, centre + margin


def git_state():
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                                capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], check=True,
                                    capture_output=True, text=True).stdout.strip())
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def run_sweep(model, watterson_preset, out_path):
    records, summaries = [], []
    for point_index, point in enumerate(POINTS):
        factory = channel_factory(model, point, watterson_preset=watterson_preset)
        label = channel_point_label(model, point, watterson_preset=watterson_preset)
        print(f"hc2: {label}, {TRIALS} trials")
        trials = run_frame_trials(HC2, factory, TRIALS, SEED, point_index,
                                  label)
        records.extend(trials)
        passed = sum(t.decoded for t in trials)
        acquired = sum(t.outcome.value in ("decoded", "payload_failed")
                       for t in trials)
        low, high = wilson_interval(passed, len(trials))
        alow, ahigh = wilson_interval(acquired, len(trials))
        row = {"mode_name": "hc2", "point_db": point, "passed": passed,
               "total": len(trials), "rate": passed / len(trials),
               "wilson_95": [low, high],
               "acquisition_rate": acquired / len(trials),
               "acquisition_wilson_95": [alow, ahigh]}
        summaries.append(row)
        print(f"  {passed}/{len(trials)} decoded ({row['rate']*100:.1f}%, "
              f"95% CI {low*100:.1f}-{high*100:.1f}%), "
              f"acquired {acquired}/{len(trials)}")
    commit, dirty = git_state()
    run = TrialRun(
        channel={"type": model, "policy": "hf-ssb", "watterson_preset": watterson_preset,
                 "points_db": list(POINTS)},
        trials=records, seed=SEED,
        metadata={"benchmark": "hc2_experimental_simulated_channel_monte_carlo",
                  "git_commit": commit, "git_dirty": dirty,
                  "trials_per_point": TRIALS,
                  "summary_by_point": summaries,
                  "completed_utc": datetime.now(timezone.utc).isoformat()})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(run.to_dict(), indent=2, allow_nan=False) + "\n")
    print(f"wrote {out_path}")
    return summaries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path,
                    default=Path("logs/mode_qualification/hf-ssb/hc2-experimental/2026-08-30"))
    args = ap.parse_args()
    all_summaries = {}
    run_sweep("awgn", None, args.out_dir / "awgn_frame_monte_carlo.json")
    for preset in PRESETS:
        summaries = run_sweep("watterson", preset,
                              args.out_dir / f"watterson_{preset}_frame_monte_carlo.json")
        all_summaries[preset] = summaries
    return 0


if __name__ == "__main__":
    sys.exit(main())
