"""Direct radio-to-radio smoke test for HF4 (bypasses the mode registry).

Quick real-hardware probe: is HF4's 16-QAM/75-carrier design decodable at
all over an IC-7300 -> IC-705 audio path? Not a qualification run.

Run (from the repository root):
    python experiments/hf4/hardware_test.py --a ic7300 --b ic705 --trials 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import numpy as np

import bench
from experiments.hf4 import hf4

DEFAULT_TRIALS = 3
DEFAULT_CAPTURE_TAIL = 1.0
DEFAULT_INTER_TRIAL = 0.5


def run_direction(tx, rx, direction, trials, seed, *, capture_tail, inter_trial):
    payload_bytes = hf4.MAX_PAYLOAD_BYTES
    print(f"\n  {direction}: {trials} x {payload_bytes} B")
    records = []
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
        payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
        audio = hf4.modulate(payload)

        keyed = tx.send(audio)
        time.sleep(capture_tail)
        captured = rx.snapshot_rx()

        result = hf4.demodulate(captured)
        decoded_payload = result.get("payload")
        decoded = decoded_payload == payload
        if decoded:
            outcome = "decoded"
        elif not result.get("synced"):
            outcome = "no_sync"
        elif not result.get("crc_ok"):
            outcome = "crc_fail"
        else:
            outcome = "payload_mismatch"

        record = {
            "trial": trial, "direction": direction, "payload_bytes": payload_bytes,
            "keyed_seconds": keyed, "rx_samples_12k": len(captured),
            "outcome": outcome, "confidence": result.get("confidence"),
            "freq_offset_hz": result.get("freq_offset_hz"),
            "carrier_snr_db": result.get("carrier_snr_db"),
        }
        records.append(record)
        conf = record["confidence"]
        conf_text = "n/a" if conf is None else f"{conf:.3f}"
        snr = record["carrier_snr_db"]
        snr_text = ""
        if snr:
            arr = np.array(snr, dtype=float)
            snr_text = f" snr_min={arr.min():.1f} snr_med={np.median(arr):.1f}"
        print(f"    {trial}/{trials}: keyed={keyed:.2f}s rx={len(captured)} "
              f"conf={conf_text}{snr_text} {outcome}")
        if trial != trials:
            time.sleep(inter_trial)
    return records


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--direction", choices=("both", "ab", "ba"), default="ab")
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--output-dir", type=Path)
    args = ap.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "mode_sweeps" / f"hf4-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("HF4 smoke test (undeclared/unregistered mode)")
    print(f"radios A={args.a}, B={args.b}; seed={args.seed}; trials={args.trials}")
    print(f"frame: {hf4.MAX_PAYLOAD_BYTES} B payload, {hf4.FRAME_SECONDS:.3f}s @ "
          f"{hf4.RX_SAMPLE_RATE} Hz rx-native")

    records = []
    with pair_factory(args.a, args.b, warmup=3.0) as (transport_a, transport_b):
        if args.direction in ("both", "ab"):
            records.extend(run_direction(
                transport_a, transport_b, f"A:{args.a}->B:{args.b}",
                args.trials, args.seed,
                capture_tail=args.capture_tail, inter_trial=args.inter_trial))
        if args.direction in ("both", "ba"):
            records.extend(run_direction(
                transport_b, transport_a, f"B:{args.b}->A:{args.a}",
                args.trials, args.seed,
                capture_tail=args.capture_tail, inter_trial=args.inter_trial))

    decoded = sum(1 for r in records if r["outcome"] == "decoded")
    total = len(records)
    print(f"\n== RESULTS == {decoded}/{total} decoded")

    out = {
        "note": "Provisional smoke evidence only, not a qualification run.",
        "channel": {"type": "hardware", "radio_a": args.a, "radio_b": args.b},
        "seed": args.seed, "trials": records,
        "summary": {"decoded": decoded, "total": total},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if decoded == total else 1


if __name__ == "__main__":
    sys.exit(main())
