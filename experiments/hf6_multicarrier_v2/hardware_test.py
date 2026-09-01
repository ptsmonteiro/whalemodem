"""Real-hardware trial runner for the small-N multicarrier extension (mc.py).

Run (from the repository root), e.g. the starting 2-carrier config:
    python experiments/hf6_multicarrier_v2/hardware_test.py \
        --carrier-hz 800 2000 --baud 500 --bps 2 --packet-bytes 16 --trials 3

Each trial: build one random payload per carrier, modulate the composite
multicarrier signal, key the real TX radio once, capture once on the real
RX radio, demodulate each carrier independently, compare payload bytes and
CRC per carrier. No framing/ARQ layer -- same direct-PHY-probe pattern as
experiments/hf5_8psk_4k/hardware_test.py.

SAFETY: the IC-705 must never transmit for this task. --direction is
hardcoded to "ab" (IC-7300 TX -> IC-705 RX) with no other choice, exactly
like hf5_8psk_4k/hardware_test.py; there is no code path here that ever
calls the IC-705 transport's .send().
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
from experiments.hf6_multicarrier_v2 import mc

DEFAULT_TRIALS = 3
DEFAULT_CAPTURE_TAIL = 1.0
DEFAULT_INTER_TRIAL = 0.5

# Distinct PN seeds per carrier index so preambles/pilots don't
# cross-correlate. Arbitrary but fixed, mirroring sc.py's single fixed
# preamble/pilot seed pair.
PREAMBLE_SEEDS = [0x2E, 0x31, 0x0D, 0x3A]
PILOT_SEEDS = [0x15, 0x27, 0x09, 0x3C]


def build_mode(args) -> mc.MultiCarrierMode:
    n = len(args.carrier_hz)
    carriers = []
    for i, hz in enumerate(args.carrier_hz):
        carriers.append(mc.CarrierSpec(
            carrier_hz=hz,
            baud=args.baud,
            bits_per_symbol=args.bps,
            packet_bytes=args.packet_bytes,
            preamble_seed=PREAMBLE_SEEDS[i % len(PREAMBLE_SEEDS)],
            pilot_seed=PILOT_SEEDS[i % len(PILOT_SEEDS)],
            pilot_interval=args.pilot_interval,
        ))
    return mc.MultiCarrierMode(carriers=tuple(carriers))


def run_direction(tx, rx, direction, mode, trials, seed, *, capture_tail, inter_trial):
    payload_sizes = [c.max_payload_bytes for c in mode.carriers]
    print(f"\n  {direction}: {trials} trials, {mode.n_carriers} carriers at "
          f"{[c.carrier_hz for c in mode.carriers]} Hz, baud={mode.carriers[0].baud} "
          f"bps={mode.carriers[0].bits_per_symbol} frame={mode.frame_seconds():.2f}s "
          f"payload/carrier={payload_sizes} B")
    records = []
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
        payloads = [rng.integers(0, 256, sz, dtype=np.uint8).tobytes() for sz in payload_sizes]
        audio = mode.modulate(payloads)

        keyed = tx.send(audio)
        time.sleep(capture_tail)
        captured = rx.snapshot_rx()

        results = mode.demodulate(captured)
        per_carrier = []
        n_decoded = 0
        for c, payload, result in zip(mode.carriers, payloads, results):
            decoded_payload = result.get("payload")
            decoded = decoded_payload == payload
            if decoded:
                outcome = "decoded"
                n_decoded += 1
            elif not result.get("synced"):
                outcome = "no_sync"
            elif not result.get("crc_ok"):
                outcome = "crc_fail"
            else:
                outcome = "payload_mismatch"
            per_carrier.append({
                "carrier_hz": c.carrier_hz, "outcome": outcome,
                "confidence": result.get("confidence"),
                "freq_offset_hz": result.get("freq_offset_hz"),
                "channel_snr_db": result.get("channel_snr_db"),
            })

        total_payload_bits = sum(len(p) * 8 for p in payloads)
        net_bps = (total_payload_bits / mode.frame_seconds()
                   if n_decoded == mode.n_carriers else 0.0)
        record = {
            "trial": trial, "direction": direction,
            "keyed_seconds": keyed, "rx_samples_12k": len(captured),
            "n_carriers": mode.n_carriers, "n_decoded": n_decoded,
            "per_carrier": per_carrier, "net_bps": net_bps,
        }
        records.append(record)
        def _pc_text(pc):
            conf = "n/a" if pc["confidence"] is None else f"{pc['confidence']:.3f}"
            return f"{pc['carrier_hz']:.0f}Hz:{pc['outcome']}(conf={conf})"
        summary = " | ".join(_pc_text(pc) for pc in per_carrier)
        snr_txt = " ".join(
            f"snr{pc['carrier_hz']:.0f}={pc['channel_snr_db']:.1f}dB"
            for pc in per_carrier if pc["channel_snr_db"] is not None)
        print(f"    {trial}/{trials}: keyed={keyed:.2f}s rx={len(captured)} "
              f"{n_decoded}/{mode.n_carriers} decoded | {summary} | {snr_txt}")
        if trial != trials:
            time.sleep(inter_trial)
    return records


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--carrier-hz", type=float, nargs="+", default=[800.0, 2000.0],
                     help="centre frequency (Hz) for each carrier; number of "
                          "values sets the carrier count")
    ap.add_argument("--baud", type=float, default=500.0)
    ap.add_argument("--bps", type=int, default=2, help="bits per symbol: 1=BPSK 2=QPSK 3=8PSK 4=16QAM")
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

    mode = build_mode(args)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "mode_sweeps" / f"hf6_multicarrier_v2-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("hf6_multicarrier_v2 smoke test (from-scratch, unregistered mode)")
    print(f"radios A={args.a}, B={args.b}; seed={args.seed}; trials={args.trials}")
    print(f"frame: {mode.n_carriers} carriers at {args.carrier_hz} Hz, "
          f"{mode.frame_seconds():.3f}s @ {args.baud} baud, {args.bps} bits/symbol")

    # ic705 must never transmit for this task -- only ic7300(a) is ever
    # keyed via transport_a.send(). transport_b (ic705) is receive-only;
    # there is deliberately no code path here that calls transport_b.send().
    records = []
    with pair_factory(args.a, args.b, warmup=3.0) as (transport_a, transport_b):
        records.extend(run_direction(
            transport_a, transport_b, f"A:{args.a}->B:{args.b}", mode,
            args.trials, args.seed,
            capture_tail=args.capture_tail, inter_trial=args.inter_trial))

    fully_decoded = sum(1 for r in records if r["n_decoded"] == r["n_carriers"])
    total = len(records)
    print(f"\n== RESULTS == {fully_decoded}/{total} trials fully decoded (all carriers)")

    out = {
        "note": "Provisional smoke evidence only, not a qualification run.",
        "channel": {"type": "hardware", "radio_a": args.a, "radio_b": args.b},
        "config": {"carrier_hz": args.carrier_hz, "baud": args.baud, "bits_per_symbol": args.bps,
                   "packet_bytes": args.packet_bytes, "pilot_interval": args.pilot_interval},
        "seed": args.seed, "trials": records,
        "summary": {"fully_decoded": fully_decoded, "total": total},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if fully_decoded == total else 1


if __name__ == "__main__":
    sys.exit(main())
