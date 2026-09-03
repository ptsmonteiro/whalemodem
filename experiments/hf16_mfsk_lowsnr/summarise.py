"""Rank HF16 hardware runs by net throughput at a decode-rate target.

The question this campaign exists to answer is "what is the highest net bit
rate that still decodes 95% of the time on this path", so the ranking is:
keep every configuration whose decode rate clears the target, and among those
take the fastest. Two decode rates are reported and they mean different
things:

  point estimate   decoded/total, which is what the run literally observed;
  Wilson 95% LB    the lower end of a 95% confidence interval on the true
                   rate, which is what may be claimed.

At 8 trials even 8/8 has a Wilson lower bound of only 0.63, so a screening
run can rank configurations but cannot establish a 95% claim about any of
them; that needs roughly 60 consecutive successes. The table prints both so
the difference is impossible to miss, and `--target` gates on whichever the
caller asks for via `--gate-on`.

Several run directories may be given; trials for the same config label are
pooled, which is only legitimate because the harness round-robins configs
within a run, so pooling does not mix one config's good period with
another's bad one.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

Z_95 = 1.959963984540054


def wilson(successes: int, total: int, z: float = Z_95) -> tuple[float, float]:
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def load(run_dirs):
    configs, trials = {}, defaultdict(list)
    for d in run_dirs:
        meta = json.loads((Path(d) / "result.json").read_text())
        for cfg in meta["configs"]:
            configs.setdefault(cfg["label"], cfg)
        for res in meta["results"]:
            trials[res["label"]].extend(res["trials"])
    return configs, trials


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--target", type=float, default=0.95)
    ap.add_argument("--gate-on", choices=("point", "wilson"), default="point")
    args = ap.parse_args(argv)

    configs, trials = load(args.run_dirs)
    rows = []
    for label, recs in trials.items():
        cfg = configs[label]
        total = len(recs)
        ok = sum(1 for r in recs if r["decoded"])
        lo, hi = wilson(ok, total)
        outcomes = defaultdict(int)
        for r in recs:
            outcomes[r["outcome"]] += 1
        rows.append({
            "label": label, "decoded": ok, "total": total,
            "rate": ok / total if total else 0.0, "wilson_lo": lo,
            "wilson_hi": hi, "net_bps": cfg["net_bit_rate"],
            "payload_bytes": cfg["max_payload_bytes"],
            "frame_seconds": cfg["frame_seconds"],
            "tone_count": cfg["tone_count"], "repeat": cfg["repeat"],
            "no_sync": outcomes["no_sync"], "crc_fail": outcomes["crc_fail"]})

    key = "rate" if args.gate_on == "point" else "wilson_lo"
    rows.sort(key=lambda r: (-r["net_bps"],))

    print(f"{'config':30s} {'dec':>7s} {'rate':>6s} {'95% LB':>7s} "
          f"{'net bps':>8s} {'bytes':>6s} {'frame':>6s} {'nosync':>7s}")
    for r in rows:
        print(f"{r['label']:30s} {r['decoded']:3d}/{r['total']:<3d} "
              f"{r['rate']:6.3f} {r['wilson_lo']:7.3f} {r['net_bps']:8.1f} "
              f"{r['payload_bytes']:6d} {r['frame_seconds']:6.1f} "
              f"{r['no_sync']:7d}")

    passing = [r for r in rows if r[key] >= args.target]
    print(f"\ngate: {args.gate_on} decode rate >= {args.target:.2f}")
    if passing:
        best = max(passing, key=lambda r: r["net_bps"])
        print(f"  {len(passing)} config(s) pass; fastest is {best['label']} "
              f"at {best['net_bps']:.1f} bit/s "
              f"({best['decoded']}/{best['total']}, 95% LB {best['wilson_lo']:.3f})")
    else:
        print("  no configuration passes at this trial count")
        best_seen = max(rows, key=lambda r: (r[key], r["net_bps"]))
        print(f"  best observed: {best_seen['label']} "
              f"{best_seen['decoded']}/{best_seen['total']} "
              f"at {best_seen['net_bps']:.1f} bit/s")
    need = math.ceil(math.log(0.05) / math.log(args.target)) if args.target < 1 else 0
    print(f"  note: a Wilson lower bound of {args.target:.2f} needs about "
          f"{need} consecutive successes with no failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
