"""Real-hardware trial runner for the FEC-extended single-carrier mode
(`sc_fec.py`), which reuses experiments/hf5_8psk_4k/sc.py's carrier/RRC/
preamble/pilot machinery and adds optional IEEE 802.11n QC-LDPC FEC
(experiments/qpsk29/ldpc.py) plus a block interleaver.

Run (from the repository root), e.g. the v1 8PSK baseline re-check
(fec_rate=None must reproduce hf5's numbers exactly):
    python experiments/hf12_sc_fec_v7/hardware_test.py --baud 1500 --bps 3 \
        --packet-bytes 2994 --pilot-interval 150 --trials 5

FEC on top of 8PSK or 16-QAM:
    python experiments/hf12_sc_fec_v7/hardware_test.py --baud 1500 --bps 4 \
        --fec-rate 3/4 --packet-bytes 972 --pilot-interval 150 --trials 5

Every trial reports raw (pre-FEC, hard-decision) BER against the full
coded+interleaved ground-truth bit stream, and residual (post-FEC-decode
when FEC is on) BER in the payload-bit domain -- same convention as
experiments/hf10_ofdm49_v6/hardware_test.py.

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
from experiments.hf12_sc_fec_v7 import sc_fec

DEFAULT_TRIALS = 5
DEFAULT_CAPTURE_TAIL = 1.0
DEFAULT_INTER_TRIAL = 0.5


def _compute_ber(truth_bits: np.ndarray, payload_len: int, rx_bits, *, length_bytes: int) -> dict:
    if rx_bits is None:
        return {"ber": None, "bit_errors": None, "bits_compared": None}
    n = min(len(truth_bits), len(rx_bits))
    if n == 0:
        return {"ber": None, "bit_errors": None, "bits_compared": None}
    lo = length_bytes * 8
    hi = min(n, (length_bytes + payload_len) * 8)
    if hi <= lo:
        lo, hi = 0, n
    truth_slice = truth_bits[lo:hi]
    rx_slice = np.asarray(rx_bits[lo:hi])
    errors = int(np.sum(truth_slice != rx_slice))
    bits_compared = int(hi - lo)
    return {"ber": errors / bits_compared, "bit_errors": errors, "bits_compared": bits_compared}


def run_direction(tx, rx, direction, mode, trials, seed, *, capture_tail, inter_trial):
    payload_bytes = mode.max_payload_bytes
    fec_text = mode.fec_rate or "none"
    print(f"\n  {direction}: {trials} x {payload_bytes} B, baud={mode.baud} "
          f"bps={mode.bits_per_symbol} fec={fec_text} interleave={mode.interleave} "
          f"pilot_interval={mode.pilot_interval} frame={mode.frame_seconds():.3f}s "
          f"net_bps_if_all_decode={mode.net_bps():.1f}")
    records = []
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
        payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
        audio = mode.modulate(payload)
        truth_raw, truth_coded = mode.pack_and_encode_bits(payload)

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

        raw_bits_out = result.get("pre_fec_bits")
        if raw_bits_out is None:
            raw_ber_info = {"ber": None, "bit_errors": None, "bits_compared": None}
        else:
            n = min(len(truth_coded), len(raw_bits_out))
            errors = int(np.sum(truth_coded[:n] != np.asarray(raw_bits_out[:n])))
            raw_ber_info = {"ber": (errors / n) if n else None, "bit_errors": errors, "bits_compared": n}

        post_ber_info = _compute_ber(truth_raw, len(payload), result.get("raw_bits"),
                                      length_bytes=sc_fec.LENGTH_BYTES)

        net_bps = (payload_bytes * 8) / mode.frame_seconds() if decoded else 0.0
        record = {
            "trial": trial, "direction": direction, "payload_bytes": payload_bytes,
            "keyed_seconds": keyed, "rx_samples_12k": len(captured),
            "outcome": outcome, "confidence": result.get("confidence"),
            "freq_offset_hz": result.get("freq_offset_hz"),
            "channel_snr_db": result.get("channel_snr_db"),
            "pilot_blocks": result.get("pilot_blocks"),
            "ldpc_ok": result.get("ldpc_ok"),
            "ldpc_iterations": result.get("ldpc_iterations"),
            "ldpc_codewords_ok": result.get("ldpc_codewords_ok"),
            "ldpc_codewords_total": result.get("ldpc_codewords_total"),
            "net_bps": net_bps,
            "raw_ber": raw_ber_info["ber"], "raw_bit_errors": raw_ber_info["bit_errors"],
            "raw_bits_compared": raw_ber_info["bits_compared"],
            "ber": post_ber_info["ber"], "bit_errors": post_ber_info["bit_errors"],
            "bits_compared": post_ber_info["bits_compared"],
        }
        records.append(record)
        conf = record["confidence"]
        conf_text = "n/a" if conf is None else f"{conf:.3f}"
        snr = record["channel_snr_db"]
        snr_text = "" if snr is None else f" snr={snr:.1f}dB"
        foff = record["freq_offset_hz"]
        foff_text = "" if foff is None else f" foff={foff:.2f}Hz"
        raw_ber = record["raw_ber"]
        raw_ber_text = "raw_ber=n/a" if raw_ber is None else f"raw_ber={raw_ber:.4f}({record['raw_bit_errors']}/{record['raw_bits_compared']})"
        ber = record["ber"]
        ber_text = "ber=n/a" if ber is None else f"ber={ber:.4f}({record['bit_errors']}/{record['bits_compared']})"
        ldpc_text = "" if not mode.fec_rate else (
            f" ldpc_ok={record['ldpc_ok']} cw={record['ldpc_codewords_ok']}/"
            f"{record['ldpc_codewords_total']} iters={record['ldpc_iterations']}")
        print(f"    {trial}/{trials}: keyed={keyed:.2f}s rx={len(captured)} "
              f"conf={conf_text}{snr_text}{foff_text} {raw_ber_text} {ber_text}{ldpc_text} {outcome}")
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
    ap.add_argument("--packet-bytes", type=int, default=294)
    ap.add_argument("--pilot-interval", type=int, default=150)
    ap.add_argument("--fec-rate", choices=("1/2", "2/3", "3/4"), default=None,
                     help="IEEE 802.11n QC-LDPC rate (experiments/qpsk29/ldpc.py); "
                          "default None = no FEC (identical to sc.py's baseline)")
    ap.add_argument("--no-interleave", action="store_true",
                     help="disable the block interleaver (only meaningful with --fec-rate)")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--direction", choices=("ab",), default="ab",
                     help="ic7300(TX) -> ic705(RX) only. ic705 must never "
                          "transmit for this task; ba/both are removed.")
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)

    mode = sc_fec.SingleCarrierFecMode(baud=args.baud, bits_per_symbol=args.bps,
                                        packet_bytes=args.packet_bytes,
                                        pilot_interval=args.pilot_interval,
                                        fec_rate=args.fec_rate,
                                        interleave=not args.no_interleave)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "mode_qualification" / "hf-ssb" / "hf12_sc_fec_v7" / stamp
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"hf12_sc_fec_v7 hardware test {args.label}")
    print(f"radios A={args.a}, B={args.b}; seed={args.seed}; trials={args.trials}")
    print(f"frame: {mode.max_payload_bytes} B payload, {mode.frame_seconds():.3f}s @ "
          f"{mode.baud} baud, {mode.bits_per_symbol} bits/symbol, fec_rate={args.fec_rate} "
          f"interleave={mode.interleave} carrier={sc_fec.CARRIER_HZ} Hz "
          f"net_bps_if_all_decode={mode.net_bps():.1f}")

    records = []
    with pair_factory(args.a, args.b, warmup=3.0) as (transport_a, transport_b):
        records.extend(run_direction(
            transport_a, transport_b, f"A:{args.a}->B:{args.b}", mode,
            args.trials, args.seed,
            capture_tail=args.capture_tail, inter_trial=args.inter_trial))

    decoded = sum(1 for r in records if r["outcome"] == "decoded")
    total = len(records)
    bers = [r["ber"] for r in records if r["ber"] is not None]
    raw_bers = [r["raw_ber"] for r in records if r["raw_ber"] is not None]
    mean_ber = float(np.mean(bers)) if bers else None
    mean_raw_ber = float(np.mean(raw_bers)) if raw_bers else None
    net_bps = mode.net_bps() if decoded == total and total > 0 else None
    print(f"\n== RESULTS == {decoded}/{total} decoded; mean_raw_ber={mean_raw_ber}; "
          f"mean_post_ber={mean_ber}; net_bps={net_bps}")

    out = {
        "note": "Provisional smoke evidence only, not a qualification run unless trials>=10.",
        "label": args.label,
        "channel": {"type": "hardware", "radio_a": args.a, "radio_b": args.b},
        "config": {"baud": args.baud, "bits_per_symbol": args.bps,
                   "packet_bytes": args.packet_bytes, "pilot_interval": args.pilot_interval,
                   "fec_rate": args.fec_rate, "interleave": mode.interleave,
                   "frame_seconds": mode.frame_seconds(),
                   "max_payload_bytes": mode.max_payload_bytes},
        "seed": args.seed, "trials": records,
        "summary": {"decoded": decoded, "total": total, "mean_ber": mean_ber,
                     "mean_raw_ber": mean_raw_ber, "net_bps": net_bps},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if decoded == total else 1


if __name__ == "__main__":
    sys.exit(main())
