"""Capture real over-the-air hf5-baseline frames for fast-sync validation.

Modulates known random payloads with the READ-ONLY hf5 baseline mode
(experiments/hf5_8psk_4k/sc.py, unmodified, qualified config: 8PSK @ 1500
baud, packet_bytes=2994, pilot_interval=150, ~4049 bps), keys the IC-7300,
captures on the IC-705 (receive-only), and saves each raw 12 kHz capture
plus its ground-truth payload to disk as .npz. This produces the raw
material that compare_sync.py replays offline through both the original
sc.SingleCarrierMode.demodulate() and the fast-sync PatchedMode, so the
comparison is against a real captured signal, not synthetic AWGN.

Safety: IC-705 must NEVER transmit or be keyed in this project -- only the
IC-7300 may transmit. This script only ever calls .send() on the ic7300
transport; there is no code path here that calls the ic705 transport's
.send(). Same --direction=ab-only pattern as experiments/hf5_8psk_4k/hardware_test.py.

Run (from the repository root):
    python experiments/hf13_fast_sync_v1/capture_frames.py --trials 10
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
from experiments.hf5_8psk_4k import sc

DEFAULT_TRIALS = 10
DEFAULT_CAPTURE_TAIL = 1.0
DEFAULT_INTER_TRIAL = 1.0

# hf5's qualified operating point (RESULTS.md step 28: ~4049 bps, 5/5).
BAUD = 1500.0
BITS_PER_SYMBOL = 3
PACKET_BYTES = 2994
PILOT_INTERVAL = 150


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300", help="TX radio (must stay ic7300)")
    ap.add_argument("--b", default="ic705", help="RX radio (must stay ic705, never keyed)")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--direction", choices=("ab",), default="ab",
                     help="ic7300(TX) -> ic705(RX) only. ic705 must never "
                          "transmit for this task; ba/both are removed.")
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--output-dir", type=Path,
                     default=Path(__file__).resolve().parent / "captures")
    args = ap.parse_args(argv)

    if args.a != "ic7300" or args.b != "ic705":
        raise SystemExit("refusing: this task requires a=ic7300 (TX), b=ic705 (RX, never keyed)")

    mode = sc.SingleCarrierMode(baud=BAUD, bits_per_symbol=BITS_PER_SYMBOL,
                                 packet_bytes=PACKET_BYTES, pilot_interval=PILOT_INTERVAL)
    payload_bytes = mode.max_payload_bytes

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print("hf13_fast_sync_v1 real-hardware capture")
    print(f"radios A(TX)={args.a}, B(RX)={args.b}; seed={args.seed}; trials={args.trials}")
    print(f"frame: {payload_bytes} B payload, {mode.frame_seconds():.3f}s @ "
          f"{mode.baud} baud, {mode.bits_per_symbol} bits/symbol, pilot_interval={mode.pilot_interval}")

    manifest = []
    # ic705 (transport_b) is receive-only: only transport_a.send() is ever
    # called below. There is deliberately no code path that calls
    # transport_b.send().
    with pair_factory(args.a, args.b, warmup=3.0) as (transport_a, transport_b):
        for trial in range(1, args.trials + 1):
            stale = transport_b.snapshot_rx()
            transport_b.consume_rx(len(stale))

            rng = np.random.default_rng(np.random.SeedSequence([args.seed, trial]))
            payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
            audio = mode.modulate(payload)

            keyed = transport_a.send(audio)
            time.sleep(args.capture_tail)
            captured_12k = transport_b.snapshot_rx()

            capture_path = args.output_dir / f"hf13_capture_{stamp}_{trial:03d}.npz"
            np.savez_compressed(capture_path,
                                 audio_12k=captured_12k.astype(np.float32),
                                 payload=np.frombuffer(payload, dtype=np.uint8))

            entry = {
                "trial": trial, "capture": str(capture_path),
                "keyed_seconds": keyed, "rx_samples_12k": len(captured_12k),
                "payload_bytes": payload_bytes,
                "config": {"baud": BAUD, "bits_per_symbol": BITS_PER_SYMBOL,
                           "packet_bytes": PACKET_BYTES, "pilot_interval": PILOT_INTERVAL},
                "seed": args.seed,
            }
            manifest.append(entry)
            print(f"  trial {trial}/{args.trials}: keyed={keyed:.2f}s "
                  f"rx={len(captured_12k)}samp -> {capture_path.name}")
            if trial != args.trials:
                time.sleep(args.inter_trial)

    manifest_path = args.output_dir / f"manifest_{stamp}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nwrote {manifest_path}")
    print(f"{len(manifest)} captures saved to {args.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
