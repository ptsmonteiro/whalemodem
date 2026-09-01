"""Real-hardware trial runner for the from-scratch single-carrier mode.

Run (from the repository root):
    python experiments/hf5_8psk_4k/hardware_test.py --baud 100 --bps 1 \
        --packet-bytes 16 --trials 5

Each trial: build a random payload, modulate, key the real TX radio,
capture on the real RX radio, demodulate, compare payload bytes and CRC.
No framing/ARQ layer -- this is a direct probe of the PHY, same pattern as
experiments/hf4/hardware_test.py.
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

DEFAULT_TRIALS = 5
DEFAULT_CAPTURE_TAIL = 1.0
DEFAULT_INTER_TRIAL = 0.5


def run_direction(tx, rx, direction, mode, trials, seed, *, capture_tail, inter_trial):
    payload_bytes = mode.max_payload_bytes
    print(f"\n  {direction}: {trials} x {payload_bytes} B, baud={mode.baud} "
          f"bps={mode.bits_per_symbol} frame={mode.frame_seconds():.2f}s")
    records = []
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
        payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
        audio = mode.modulate(payload)

        keyed = tx.send(audio)
        time.sleep(capture_tail)
        captured = rx.snapshot_rx()

        result = mode.demodulate(captured)
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

        net_bps = (payload_bytes * 8) / mode.frame_seconds() if decoded else 0.0
        record = {
            "trial": trial, "direction": direction, "payload_bytes": payload_bytes,
            "keyed_seconds": keyed, "rx_samples_12k": len(captured),
            "outcome": outcome, "confidence": result.get("confidence"),
            "freq_offset_hz": result.get("freq_offset_hz"),
            "channel_snr_db": result.get("channel_snr_db"),
            "net_bps": net_bps,
        }
        records.append(record)
        conf = record["confidence"]
        conf_text = "n/a" if conf is None else f"{conf:.3f}"
        snr = record["channel_snr_db"]
        snr_text = "" if snr is None else f" snr={snr:.1f}dB"
        foff = record["freq_offset_hz"]
        foff_text = "" if foff is None else f" foff={foff:.2f}Hz"
        print(f"    {trial}/{trials}: keyed={keyed:.2f}s rx={len(captured)} "
              f"conf={conf_text}{snr_text}{foff_text} {outcome}")
        if trial != trials:
            time.sleep(inter_trial)
    return records


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--baud", type=float, default=100.0)
    ap.add_argument("--bps", type=int, default=1, help="bits per symbol: 1=BPSK 2=QPSK 3=8PSK 4=16QAM")
    ap.add_argument("--packet-bytes", type=int, default=16)
    ap.add_argument("--pilot-interval", type=int, default=0,
                     help="data symbols between mid-frame pilot blocks; 0 disables pilot tracking")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--direction", choices=("ab",), default="ab",
                     help="ic7300(TX) -> ic705(RX) only. ic705 must never "
                          "transmit for this task; ba/both are removed.")
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--output-dir", type=Path)
    args = ap.parse_args(argv)

    mode = sc.SingleCarrierMode(baud=args.baud, bits_per_symbol=args.bps,
                                 packet_bytes=args.packet_bytes,
                                 pilot_interval=args.pilot_interval)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "mode_sweeps" / f"hf5_8psk_4k-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("hf5_8psk_4k smoke test (from-scratch, unregistered mode)")
    print(f"radios A={args.a}, B={args.b}; seed={args.seed}; trials={args.trials}")
    print(f"frame: {mode.max_payload_bytes} B payload, {mode.frame_seconds():.3f}s @ "
          f"{mode.baud} baud, {mode.bits_per_symbol} bits/symbol, carrier={sc.CARRIER_HZ} Hz")

    # ic705 must never transmit for this task -- only ic7300(a) is ever
    # keyed via transport_a.send(). transport_b (ic705) is receive-only;
    # there is deliberately no code path here that calls transport_b.send().
    records = []
    with pair_factory(args.a, args.b, warmup=3.0) as (transport_a, transport_b):
        records.extend(run_direction(
            transport_a, transport_b, f"A:{args.a}->B:{args.b}", mode,
            args.trials, args.seed,
            capture_tail=args.capture_tail, inter_trial=args.inter_trial))

    decoded = sum(1 for r in records if r["outcome"] == "decoded")
    total = len(records)
    print(f"\n== RESULTS == {decoded}/{total} decoded")

    out = {
        "note": "Provisional smoke evidence only, not a qualification run.",
        "channel": {"type": "hardware", "radio_a": args.a, "radio_b": args.b},
        "config": {"baud": args.baud, "bits_per_symbol": args.bps,
                   "packet_bytes": args.packet_bytes},
        "seed": args.seed, "trials": records,
        "summary": {"decoded": decoded, "total": total},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if decoded == total else 1


if __name__ == "__main__":
    sys.exit(main())
