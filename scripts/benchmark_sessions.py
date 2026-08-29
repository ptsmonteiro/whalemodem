"""Monte Carlo full-session qualification over the simulated channel models.

This drives connect, bidirectional ARQ transfer, adaptation, and disconnect
through the same channel factories as benchmark_simulated_channels.py.  Times
are simulated keyed-audio seconds, never host wall-clock time.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# The session harness intentionally remains test-only infrastructure; make the
# repository package importable when this file is executed by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from support.audio_link import DirectionalAudioLink, run_audio_session
from whale import policy
from whale.channel import WATTERSON_PRESETS
from whale.fm_channel import FM_RADIO_PRESETS
from whale.qualification import (channel_factory, channel_point_label,
                                 trial_seed)

from benchmark_simulated_channels import DEFAULT_SEED, git_state, wilson_interval


def _payload(length, seed):
    import numpy as np
    return np.random.default_rng(seed).integers(0, 256, length,
                                                dtype=np.uint8).tobytes()


def _rate(successes, total):
    low, high = wilson_interval(successes, total)
    return {"count": successes, "total": total, "rate": successes / total,
            "wilson_95": [low, high]}


def _metrics(link_a, link_b):
    a, b = link_a.qualification_metrics, link_b.qualification_metrics
    return {
        "A->B": {**a, "lost_acknowledgements": b["duplicate_data"]},
        "B->A": {**b, "lost_acknowledgements": a["duplicate_data"]},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", choices=("awgn", "watterson", "fm"), required=True)
    ap.add_argument("--policy", choices=sorted(policy.CHANNELS), required=True)
    ap.add_argument("--points", type=float, nargs="+", required=True)
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--bytes", type=int, default=1_000, dest="payload_bytes")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--watterson-preset", choices=sorted(WATTERSON_PRESETS),
                    default="mid_latitude_moderate")
    ap.add_argument("--fm-preset", choices=sorted(FM_RADIO_PRESETS),
                    default="vhf_bench_conservative")
    ap.add_argument("--reverse-model", choices=("awgn", "watterson", "fm"),
                    help="optional asymmetric B-to-A model")
    ap.add_argument("--reverse-points", type=float, nargs="+",
                    help="one B-to-A point per --points entry")
    ap.add_argument("--out", type=Path,
                    default=Path("logs") / "session_benchmark.json")
    args = ap.parse_args(argv)
    if args.trials < 1 or args.payload_bytes < 0:
        ap.error("--trials must be positive and --bytes non-negative")
    if args.reverse_points and len(args.reverse_points) != len(args.points):
        ap.error("--reverse-points must have the same length as --points")
    if args.model == "watterson" and args.policy != "hf-ssb":
        ap.error("the Watterson benchmark requires --policy hf-ssb")
    if args.model == "fm" and args.policy != "vhf-fm":
        ap.error("the FM benchmark requires --policy vhf-fm")

    selected_policy = policy.by_name(args.policy)
    records = []
    for point_index, point in enumerate(args.points):
        reverse_model = args.reverse_model or args.model
        reverse_point = (args.reverse_points[point_index]
                         if args.reverse_points else point)
        successes = 0
        for trial in range(1, args.trials + 1):
            base = trial_seed(args.seed, 0, point_index, trial)
            factory_ab = channel_factory(args.model, point,
                watterson_preset=args.watterson_preset, fm_preset=args.fm_preset)
            factory_ba = channel_factory(reverse_model, reverse_point,
                watterson_preset=args.watterson_preset, fm_preset=args.fm_preset)
            pair = DirectionalAudioLink(factory_ab(base), factory_ba(base ^ 0xB2A))
            row = {"trial": trial, "seed": base,
                   "connection_success": False, "transfer_completion": False,
                   "disconnect_success": False,
                   "directions": {"A->B": {"offered_bytes": args.payload_bytes},
                                  "B->A": {"offered_bytes": args.payload_bytes}}}
            def phase(name, airtime):
                if name == "connected":
                    row["connection_success"] = True
                    row["setup_time_simulated_seconds"] = airtime
                elif name == "transferred":
                    row["transfer_completion"] = True
                elif name == "disconnected":
                    row["disconnect_success"] = True
            try:
                result = run_audio_session(_payload(args.payload_bytes, base),
                    _payload(args.payload_bytes, base ^ 0xB2A),
                    policy=selected_policy, audio_link=pair, on_phase=phase)
                row.update(connection_success=True, transfer_completion=True,
                           disconnect_success=(result.link_a.state == "IDLE" and
                                               result.link_b.state == "IDLE"),
                           setup_time_simulated_seconds=result.setup_airtime,
                           transfer_time_simulated_seconds=result.transfer_airtime,
                           disconnect_time_simulated_seconds=result.disconnect_airtime,
                           simulated_seconds=pair.airtime,
                           useful_bytes_per_simulated_second=(
                               2 * args.payload_bytes / pair.airtime if pair.airtime else None),
                           link_metrics=_metrics(result.link_a, result.link_b))
                row["channel_measurements"] = {
                    "A->B": [dict(r.result.measurements) for r in pair.records
                             if r.direction == "A->B"],
                    "B->A": [dict(r.result.measurements) for r in pair.records
                             if r.direction == "B->A"]}
                for direction in row["directions"].values():
                    direction["delivered_bytes"] = args.payload_bytes
                    direction["completion"] = True
                successes += 1
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                row["simulated_seconds"] = pair.airtime
                row["channel_measurements"] = {
                    "A->B": [dict(r.result.measurements) for r in pair.records
                             if r.direction == "A->B"],
                    "B->A": [dict(r.result.measurements) for r in pair.records
                             if r.direction == "B->A"]}
            records.append(row)
        summary = _rate(successes, args.trials)
        print(f"{channel_point_label(args.model, point, fm_preset=args.fm_preset, watterson_preset=args.watterson_preset)}: "
              f"{successes}/{args.trials}, 95% CI {summary['wilson_95'][0]:.3f}-{summary['wilson_95'][1]:.3f}")

    commit, dirty = git_state()
    document = {"schema_version": 1, "benchmark": "full_stack_sessions",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "seed": args.seed, "git_commit": commit, "git_dirty": dirty,
                "configuration": vars(args) | {"out": str(args.out)},
                "trials": records,
                "summary": {
                    "connection_success": _rate(sum(
                        r["connection_success"] for r in records), len(records)),
                    "transfer_completion": _rate(sum(
                        r["transfer_completion"] for r in records), len(records)),
                    "disconnect_success": _rate(sum(
                        r["disconnect_success"] for r in records), len(records)),
                    "directional_delivery": {
                        direction: _rate(sum(
                            r["directions"][direction].get("completion", False)
                            for r in records), len(records))
                        for direction in ("A->B", "B->A")},
                }}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
