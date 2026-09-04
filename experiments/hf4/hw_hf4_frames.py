"""HF4 frames over the real radio pair: one modulate -> TX -> capture ->
demodulate per trial, no ARQ and no sockets.

HF4 has no `whale.mode_qualification.MANIFEST` entry (see
`whale/modes/hf4_mode.py` and `experiments/hf4/RESULTS.md`), so it is not
reachable through `scripts/sweep_modes.py`'s registry-based mode selection.
This is a bench-only hardware harness that drives `experiments.hf4.hf4`'s
raw `modulate`/`demodulate` directly, mirroring `scripts/hw_hf_frames.py`'s
method and reusing `scripts/bench.py`'s radio_pair open/warm-up/close dance,
without touching the permanent MANIFEST -- the same "no manifest, no
registry" spirit as `benchmark_hf4.py`'s mode_id=244 simulation adapter.

Run (from the repository root, so `scripts/` and `experiments/` both
resolve):

    python experiments/hf4/hw_hf4_frames.py --trials 1
    python experiments/hf4/hw_hf4_frames.py --trials 5 --direction ab
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
for path in (REPOSITORY_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import bench  # noqa: E402  (scripts/bench.py)
from experiments.hf4 import hf4  # noqa: E402

CAPTURE_TAIL = 9.0
INTER_TRIAL = 0.5


def _payload(size, rng):
    return bytes(rng.integers(0, 256, size, dtype=np.uint8))


def one_trial(tx, rx, payload, label, capture_tail=CAPTURE_TAIL):
    stale = rx.snapshot_rx()
    rx.consume_rx(len(stale))

    audio = hf4.modulate(payload)
    keyed = tx.send(audio)
    time.sleep(capture_tail)
    captured = rx.snapshot_rx()

    result = hf4.demodulate(captured)
    ok = result.get("payload") == payload
    rms = float(np.sqrt(np.mean(np.asarray(captured, float) ** 2))) if len(captured) else 0.0
    peak = float(np.max(np.abs(captured))) if len(captured) else 0.0

    print(f"  [{label}] keyed={keyed:.2f}s level rms={rms:.3f} peak={peak:.3f} "
          f"conf={result.get('confidence', 0.0):.3f} "
          f"start={result.get('start_index')} synced={result.get('synced')} "
          f"decoded={ok}")
    if not ok:
        print(f"      result keys: { {k: v for k, v in result.items() if k not in ('payload',)} }")
    return ok, result, captured


def run_direction(tx, rx, label, trials, size, rng, capture_dir=None):
    print(f"\n== {label} ==")
    ok_count = 0
    records = []
    for i in range(1, trials + 1):
        payload = _payload(size, rng)
        ok, result, captured = one_trial(tx, rx, payload, f"{label} {i}/{trials}")
        ok_count += int(ok)
        records.append({
            "trial": i, "decoded": bool(ok),
            "confidence": float(result.get("confidence", 0.0)),
            "start_index": result.get("start_index"),
            "synced": bool(result.get("synced", False)),
        })
        if capture_dir is not None:
            safe = label.replace(" ", "").replace("->", "_to_")
            stem = Path(capture_dir) / f"{safe}_{i:02d}"
            np.save(stem.with_suffix(".npy"), np.asarray(captured, np.float32))
            stem.with_suffix(".bin").write_bytes(payload)
        time.sleep(INTER_TRIAL)
    print(f"  [{label}] => {ok_count}/{trials} decoded byte-for-byte")
    return ok_count, records


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300", help="station A radio name")
    ap.add_argument("--b", default="ic705", help="station B radio name")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--payload", type=int, default=None,
                    help="payload bytes per frame (default: hf4's maximum)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--direction", default="both", choices=("both", "ab", "ba"))
    ap.add_argument("--capture-dir", default=None,
                    help="save each capture and its payload here")
    ap.add_argument("--out", default=None, help="write a JSON summary here")
    args = ap.parse_args()

    if args.payload is None:
        args.payload = hf4.MAX_PAYLOAD_BYTES
    if args.payload > hf4.MAX_PAYLOAD_BYTES:
        ap.error(f"hf4 carries at most {hf4.MAX_PAYLOAD_BYTES} bytes")
    if args.capture_dir:
        Path(args.capture_dir).mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(args.seed)
    print(f"hf4: {hf4.MAX_PAYLOAD_BYTES} B/frame, {hf4.FRAME_SECONDS:.3f} s/frame, "
          f"tx={hf4.SAMPLE_RATE} Hz rx={hf4.RX_SAMPLE_RATE} Hz")
    print(f"payload {args.payload} B, {args.trials} trials each direction")

    ok_ab = ok_ba = None
    rec_ab = rec_ba = []
    with bench.radio_pair(args.a, args.b, warmup=3.0) as (ta, tb):
        if args.direction in ("both", "ab"):
            ok_ab, rec_ab = run_direction(ta, tb, f"{args.a}->{args.b}",
                                          args.trials, args.payload, rng,
                                          args.capture_dir)
        if args.direction in ("both", "ba"):
            ok_ba, rec_ba = run_direction(tb, ta, f"{args.b}->{args.a}",
                                          args.trials, args.payload, rng,
                                          args.capture_dir)

    print("\n== RESULTS ==")
    if ok_ab is not None:
        print(f"{args.a} -> {args.b}: {ok_ab}/{args.trials}")
    if ok_ba is not None:
        print(f"{args.b} -> {args.a}: {ok_ba}/{args.trials}")
    worst = min(v for v in (ok_ab, ok_ba) if v is not None)
    print(f"worst direction run: {worst}/{args.trials}")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "mode": "hf4", "payload_bytes": args.payload,
            "trials": args.trials, "a": args.a, "b": args.b,
            f"{args.a}->{args.b}": rec_ab, f"{args.b}->{args.a}": rec_ba,
        }, indent=2))
        print(f"wrote {args.out}")
    return 0 if worst == args.trials else 1


if __name__ == "__main__":
    sys.exit(main())
