"""Real-hardware qualification-style batch for sc_fast.SingleCarrierMode,
the fused-FFT-sync drop-in for experiments/hf5_8psk_4k/sc.py.

sc_fast is a pure CPU optimization of the sync-search stage (see
RESULTS.md) -- same PHY as hf5's qualified 8PSK@1500baud/~4049bps
operating point, no FEC. Each trial reports raw (pre-CRC, hard-decision)
BER against the full packet (length+payload+CRC) ground-truth bit stream;
post-FEC BER is always None/null since this mode has no FEC (never
fabricated).

Run (from the repository root):
    python experiments/hf13_fast_sync_v1/hardware_test.py --trials 12

SAFETY: IC-705 must never transmit. --direction is hardcoded to "ab" (no
"ba"/"both" choice exists) and only transport_a (ic7300 by default) is ever
keyed via .send(). There is deliberately no code path here that calls
transport_b.send().
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
from experiments.hf13_fast_sync_v1 import sc_fast

DEFAULT_TRIALS = 12
DEFAULT_CAPTURE_TAIL = 1.0
DEFAULT_INTER_TRIAL = 0.5


def _ground_truth_bits(mode, payload: bytes) -> np.ndarray:
    """Reproduces the plain (pre-whitening) packet bit stream --
    length+payload+CRC -- so BER can be measured against
    demodulate()'s result["raw_bits"]. sc.py/sc_fast.py's RX side
    dewhitens data_bits back to this same plain packet bit stream
    (raw_bits = data_bits ^ whitener, where data_bits is what was
    actually transmitted) before packing/CRC-checking it, so the
    ground truth here must NOT be re-XORed with the whitener again --
    only unpackbits(packet), matching TX's own `raw_bits` in
    sc.py's modulate(). Uses sc.py's own packing -- read-only reuse."""
    packet = sc._pack_packet(payload, mode.packet_bytes)
    return np.unpackbits(np.frombuffer(packet, dtype=np.uint8))


def _compute_raw_ber(truth_bits: np.ndarray, rx_bits) -> dict:
    if rx_bits is None:
        return {"ber": None, "bit_errors": None, "bits_compared": None}
    rx_bits = np.asarray(rx_bits)
    n = min(len(truth_bits), len(rx_bits))
    if n == 0:
        return {"ber": None, "bit_errors": None, "bits_compared": None}
    errors = int(np.sum(truth_bits[:n] != rx_bits[:n]))
    return {"ber": errors / n, "bit_errors": errors, "bits_compared": n}


def run_direction(tx, rx, direction, mode, trials, seed, *, capture_tail, inter_trial):
    payload_bytes = mode.max_payload_bytes
    print(f"\n  {direction}: {trials} x {payload_bytes} B, baud={mode.baud} "
          f"bps={mode.bits_per_symbol} pilot_interval={mode.pilot_interval} "
          f"frame={mode.frame_seconds():.3f}s")
    records = []
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
        payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
        audio = mode.modulate(payload)
        truth_bits = _ground_truth_bits(mode, payload)

        keyed = tx.send(audio)
        time.sleep(capture_tail)
        captured = rx.snapshot_rx()

        t0 = time.perf_counter()
        result = mode.demodulate(captured)
        demod_seconds = time.perf_counter() - t0

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

        raw_ber_info = _compute_raw_ber(truth_bits, result.get("raw_bits"))

        net_bps = (payload_bytes * 8) / mode.frame_seconds() if decoded else 0.0
        record = {
            "trial": trial, "direction": direction, "payload_bytes": payload_bytes,
            "keyed_seconds": keyed, "rx_samples_12k": len(captured),
            "demod_seconds": demod_seconds,
            "outcome": outcome, "confidence": result.get("confidence"),
            "freq_offset_hz": result.get("freq_offset_hz"),
            "channel_snr_db": result.get("channel_snr_db"),
            "pilot_blocks": result.get("pilot_blocks"),
            "net_bps": net_bps,
            "raw_ber": raw_ber_info["ber"], "raw_bit_errors": raw_ber_info["bit_errors"],
            "raw_bits_compared": raw_ber_info["bits_compared"],
            # No FEC in this mode: never fabricate a post-FEC BER value.
            "post_fec_ber": None,
        }
        records.append(record)
        conf = record["confidence"]
        conf_text = "n/a" if conf is None else f"{conf:.3f}"
        snr = record["channel_snr_db"]
        snr_text = "" if snr is None else f" snr={snr:.1f}dB"
        foff = record["freq_offset_hz"]
        foff_text = "" if foff is None else f" foff={foff:.2f}Hz"
        raw_ber = record["raw_ber"]
        raw_ber_text = ("raw_ber=n/a" if raw_ber is None
                         else f"raw_ber={raw_ber:.5f}({record['raw_bit_errors']}/{record['raw_bits_compared']})")
        print(f"    {trial}/{trials}: keyed={keyed:.2f}s rx={len(captured)} "
              f"demod={demod_seconds*1000:.0f}ms conf={conf_text}{snr_text}{foff_text} "
              f"{raw_ber_text} post_fec_ber=null {outcome}")
        if trial != trials:
            time.sleep(inter_trial)
    return records


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--baud", type=float, default=1500.0)
    ap.add_argument("--bps", type=int, default=3, help="bits per symbol: 1=BPSK 2=QPSK 3=8PSK 4=16QAM")
    ap.add_argument("--packet-bytes", type=int, default=2994)
    ap.add_argument("--pilot-interval", type=int, default=150)
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--direction", choices=("ab",), default="ab",
                     help="ic7300(TX) -> ic705(RX) only. ic705 must never "
                          "transmit for this task; ba/both are removed.")
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--output-dir", type=Path)
    args = ap.parse_args(argv)

    if args.a != "ic7300" or args.b != "ic705":
        raise SystemExit("refusing: this task requires a=ic7300 (TX), b=ic705 (RX, never keyed)")

    mode = sc_fast.SingleCarrierMode(baud=args.baud, bits_per_symbol=args.bps,
                                      packet_bytes=args.packet_bytes,
                                      pilot_interval=args.pilot_interval)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "mode_qualification" / "hf-ssb" / "hf13_fast_sync_v1" / stamp
    output_dir.mkdir(parents=True, exist_ok=True)

    print("hf13_fast_sync_v1 qualification-style batch (fused-FFT sync, drop-in for hf5 sc.py)")
    print(f"radios A={args.a}, B={args.b}; seed={args.seed}; trials={args.trials}")
    print(f"frame: {mode.max_payload_bytes} B payload, {mode.frame_seconds():.3f}s @ "
          f"{mode.baud} baud, {mode.bits_per_symbol} bits/symbol, carrier={sc.CARRIER_HZ} Hz")

    records = []
    with pair_factory(args.a, args.b, warmup=3.0) as (transport_a, transport_b):
        records.extend(run_direction(
            transport_a, transport_b, f"A:{args.a}->B:{args.b}", mode,
            args.trials, args.seed,
            capture_tail=args.capture_tail, inter_trial=args.inter_trial))

    decoded = sum(1 for r in records if r["outcome"] == "decoded")
    total = len(records)
    raw_bers = [r["raw_ber"] for r in records if r["raw_ber"] is not None]
    mean_raw_ber = float(np.mean(raw_bers)) if raw_bers else None
    demod_times = [r["demod_seconds"] for r in records]
    mean_demod_ms = float(np.mean(demod_times) * 1000.0)
    net_bps_decoded = [r["net_bps"] for r in records if r["outcome"] == "decoded"]
    mean_net_bps = float(np.mean(net_bps_decoded)) if net_bps_decoded else 0.0

    print(f"\n== RESULTS == {decoded}/{total} decoded; mean_raw_ber={mean_raw_ber}; "
          f"mean_demod_ms={mean_demod_ms:.1f}; mean_net_bps(decoded only)={mean_net_bps:.1f}")

    out = {
        "note": "hf13_fast_sync_v1 qualification-style batch: sc_fast.py, "
                "fused-FFT sync drop-in for hf5_8psk_4k/sc.py. No FEC in this "
                "mode: post_fec_ber is always null, never fabricated.",
        "channel": {"type": "hardware", "radio_a": args.a, "radio_b": args.b},
        "config": {"baud": args.baud, "bits_per_symbol": args.bps,
                   "packet_bytes": args.packet_bytes, "pilot_interval": args.pilot_interval},
        "seed": args.seed, "trials": records,
        "summary": {"decoded": decoded, "total": total,
                    "mean_raw_ber": mean_raw_ber,
                    "post_fec_ber": None,
                    "mean_demod_ms": mean_demod_ms,
                    "mean_net_bps_decoded_only": mean_net_bps},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if decoded == total else 1


if __name__ == "__main__":
    sys.exit(main())
