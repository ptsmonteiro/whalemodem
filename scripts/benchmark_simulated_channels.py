"""Explicit Monte Carlo frame sweeps over simulated AWGN, HF, or FM paths.

Unlike tests/test_channel_regressions.py this command is intentionally
unbounded by CI runtime expectations. Increase --trials until the confidence
interval is useful, retain result.json, and quote every channel parameter.

Examples:
    python scripts/benchmark_simulated_channels.py --model fm --policy vhf-fm \
        --points 5 10 15 20 25 30 --trials 100
    python scripts/benchmark_simulated_channels.py --model watterson \
        --policy hf-ssb --watterson-preset mid_latitude_moderate \
        --points -5 0 5 10 15 --trials 100
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from whale import policy
from whale.channel import WATTERSON_PRESETS
from whale.fm_channel import FM_RADIO_PRESETS
from whale.qualification import (channel_factory as make_channel_factory,
                                 channel_point_label, run_frame_trials)
from whale.trials import TrialRun


DEFAULT_SEED = 20260829


def wilson_interval(passed, total, z=1.959963984540054):
    proportion = passed / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z / denominator * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total))
    return centre - margin, centre + margin


def channel_factory(args, point):
    return make_channel_factory(args.model, point,
                                watterson_preset=args.watterson_preset,
                                fm_preset=args.fm_preset)


def point_label(args, point):
    return channel_point_label(args.model, point,
                               watterson_preset=args.watterson_preset,
                               fm_preset=args.fm_preset)


def summarize_trials(trials):
    total = len(trials)
    acquired = sum(t.outcome.value in ("decoded", "payload_failed") for t in trials)
    delivered = sum(t.decoded for t in trials)
    payload_total = sum(t.payload_bytes for t in trials)
    payload_delivered = sum(t.payload_bytes for t in trials if t.decoded)
    ber_values = [float(t.decoder_metrics["ber"]) for t in trials
                  if t.decoder_metrics.get("ber") is not None]
    def rate(count, denominator=total):
        low, high = wilson_interval(count, denominator)
        return {"count": count, "total": denominator, "rate": count / denominator,
                "wilson_95": [low, high]}
    return {
        "acquisition_probability": rate(acquired),
        "frame_error_rate": rate(total - delivered),
        "payload_delivery_rate": {
            **rate(payload_delivered, payload_total),
            "delivered_bytes": payload_delivered, "offered_bytes": payload_total,
        },
        # BER is only reported when a decoder actually exposes bit evidence.
        "ber": ({"mean": sum(ber_values) / len(ber_values),
                 "evidence_frames": len(ber_values)} if ber_values else None),
    }


def select_modes(registry, requested):
    if not requested:
        return tuple(registry.modes)
    lookup = {mode.name: mode for mode in registry.modes}
    lookup.update({str(mode.mode_id): mode for mode in registry.modes})
    try:
        return tuple(dict.fromkeys(lookup[value] for value in requested))
    except KeyError as exc:
        raise ValueError(f"mode {exc.args[0]!r} is not in the selected policy") from None


def git_state():
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True,
            text=True).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], check=True, capture_output=True,
            text=True).stdout.strip())
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=("awgn", "watterson", "fm"), required=True)
    ap.add_argument("--policy", choices=sorted(policy.CHANNELS), required=True)
    ap.add_argument("--points", type=float, nargs="+", required=True,
                    help="waveform SNR dB, or RF C/N dB for FM")
    ap.add_argument("--trials", type=int, default=100)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--modes", nargs="+", help="mode names or IDs")
    ap.add_argument("--watterson-preset", choices=sorted(WATTERSON_PRESETS),
                    default="mid_latitude_moderate")
    ap.add_argument("--fm-preset", choices=sorted(FM_RADIO_PRESETS),
                    default="vhf_bench_conservative")
    ap.add_argument("--out", type=Path,
                    default=Path("logs") / "simulated_channel_benchmark.json")
    args = ap.parse_args(argv)
    if args.trials < 1:
        ap.error("--trials must be positive")
    if args.model == "watterson" and args.policy != "hf-ssb":
        ap.error("the Watterson benchmark requires --policy hf-ssb")
    if args.model == "fm" and args.policy != "vhf-fm":
        ap.error("the FM benchmark requires --policy vhf-fm")
    selected_policy = policy.by_name(args.policy)
    registry = selected_policy.mode_ladder(selected_policy.max_useful_frame_seconds)
    try:
        selected_modes = select_modes(registry, args.modes)
    except ValueError as exc:
        ap.error(str(exc))

    records, summaries = [], []
    for mode in selected_modes:
        for point_index, point in enumerate(args.points):
            label = point_label(args, point)
            print(f"{mode.name}: {label}, {args.trials} trials")
            trials = run_frame_trials(
                mode, channel_factory(args, point), args.trials, args.seed,
                point_index, label)
            records.extend(trials)
            passed = sum(trial.decoded for trial in trials)
            low, high = wilson_interval(passed, len(trials))
            row = {"mode_id": mode.mode_id, "mode_name": mode.name,
                   "point_db": point, "passed": passed, "total": len(trials),
                   "rate": passed / len(trials), "wilson_95": [low, high],
                   **summarize_trials(trials)}
            summaries.append(row)
            print(f"  {passed}/{len(trials)} ({row['rate'] * 100:.1f}%), "
                  f"95% CI {low * 100:.1f}-{high * 100:.1f}%")

    commit, dirty = git_state()
    descriptions = [dict(channel_factory(args, point)(
        int(args.seed)).describe()) for point in args.points]
    run = TrialRun(
        channel={"type": args.model, "policy": args.policy,
                 "watterson_preset": (args.watterson_preset
                                       if args.model == "watterson" else None),
                 "fm_preset": args.fm_preset if args.model == "fm" else None,
                 "points_db": args.points},
        trials=records, seed=args.seed,
        metadata={"benchmark": "simulated_channel_monte_carlo",
                  "git_commit": commit, "git_dirty": dirty,
                  "trials_per_point": args.trials,
                  "selected_mode_ids": [mode.mode_id for mode in selected_modes],
                  "channel_descriptions_by_point": descriptions,
                  "summary_by_mode_point": summaries,
                  "completed_utc": datetime.now(timezone.utc).isoformat()})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(run.to_dict(), indent=2, allow_nan=False) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
