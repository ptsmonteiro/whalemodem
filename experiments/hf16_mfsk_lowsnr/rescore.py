"""Re-run the receiver over retained captures with different receiver options.

No radio, no new airtime. Only *receiver-side* choices can be evaluated this
way -- the soft metric, acquisition settings -- because anything that changes
the coded bits changes what was transmitted, and that needs a new keying. The
code rate, tone count, repetition and frame length are therefore not
sweepable here; `hardware_test.py` is the only thing that can answer those.

The whole receiver runs, its own acquisition included, so a decode counted
here is what the mode would have delivered live on that frame.

**This is a selection experiment on a fixed set of recordings.** Picking the
best of several receivers on the same captures overfits them, and the margin
it reports is optimistic by an unknown amount. The result is a candidate to
confirm on fresh hardware trials, never the confirmation itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

from experiments.hf16_mfsk_lowsnr.mfsk_mode import mode_for
from experiments.hf16_mfsk_lowsnr.replay import trial_payload
from experiments.hf16_mfsk_lowsnr.summarise import wilson

METRICS = ("normalized", "raw", "snr")


def build(cfg, soft_metric):
    return mode_for(tone_count=cfg["tone_count"], repeat=cfg["repeat"],
                    constraint=cfg["constraint"],
                    payload_symbols=cfg["payload_symbols"],
                    sync_seconds=cfg["sync_symbols"] * cfg["symbol_seconds"],
                    soft_metric=soft_metric)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--metrics", nargs="+", default=list(METRICS))
    ap.add_argument("--label", action="append", dest="labels")
    args = ap.parse_args(argv)

    tally = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    live = defaultdict(lambda: [0, 0])
    net_bps = {}

    for run_dir in args.run_dirs:
        meta = json.loads((run_dir / "result.json").read_text())
        seed = meta["seed"]
        captures = run_dir / "captures"
        for res in meta["results"]:
            label = res["label"]
            if args.labels and label not in args.labels:
                continue
            cfg = next(c for c in meta["configs"] if c["label"] == label)
            net_bps[label] = cfg["net_bit_rate"]
            modes = {m: build(cfg, m) for m in args.metrics}
            for t in res["trials"]:
                name = t.get("capture_file")
                if not name or not (captures / name).exists():
                    continue
                cap = np.load(captures / name).astype(np.float64)
                live[label][1] += 1
                live[label][0] += bool(t["decoded"])
                for metric, mode in modes.items():
                    payload = trial_payload(mode, seed, t["trial"])
                    got = mode.demodulate(cap)["payload"]
                    tally[label][metric][1] += 1
                    tally[label][metric][0] += (got == payload)

    header = f"{'config':30s} {'live':>9s} " + " ".join(
        f"{m:>12s}" for m in args.metrics) + f" {'net bps':>8s}"
    print(header)
    print("-" * len(header))
    for label in sorted(tally, key=lambda k: -net_bps[k]):
        ok, total = live[label]
        cells = []
        for metric in args.metrics:
            a, b = tally[label][metric]
            cells.append(f"{a:3d}/{b:<3d}{'':4s}"[:12])
        print(f"{label:30s} {ok:3d}/{total:<3d}   " + " ".join(cells)
              + f" {net_bps[label]:8.1f}")

    print("\ntotals across configs (same recordings, so these are paired):")
    for metric in args.metrics:
        a = sum(tally[l][metric][0] for l in tally)
        b = sum(tally[l][metric][1] for l in tally)
        lo, hi = wilson(a, b)
        print(f"  {metric:11s} {a:3d}/{b:<3d}  rate {a / b if b else 0:.3f} "
              f"[95% {lo:.3f}, {hi:.3f}]")
    print("\nSelection on fixed recordings: confirm any winner on fresh "
          "hardware trials before believing the margin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
