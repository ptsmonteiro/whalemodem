"""Real-hardware trial runner for the from-scratch true-OFDM mode (v3).

Run (from the repository root):
    python experiments/hf7_ofdm_v3/hardware_test.py --fft-size 64 --cp-len 16 \
        --active-bins 5,6,7,8,9,10 --bps 2 --packet-bytes 20 --trials 3

Each trial: build a random payload, modulate, key the real TX radio,
capture on the real RX radio, demodulate, compare payload bytes and CRC.
No framing/ARQ layer -- direct probe of the PHY, same pattern as
experiments/hf5_8psk_4k/hardware_test.py and hf6_multicarrier_v2/hardware_test.py.

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
from experiments.hf7_ofdm_v3 import ofdm

DEFAULT_TRIALS = 3
DEFAULT_CAPTURE_TAIL = 1.0
DEFAULT_INTER_TRIAL = 0.5


def run_direction(tx, rx, direction, mode, trials, seed, *, capture_tail, inter_trial):
    payload_bytes = mode.max_payload_bytes
    print(f"\n  {direction}: {trials} x {payload_bytes} B, fft={mode.fft_size} "
          f"cp={mode.cp_len} n_active={mode.n_active} bps={mode.bits_per_symbol} "
          f"pilot_interval={mode.pilot_interval} frame={mode.frame_seconds():.3f}s "
          f"crest={mode.crest_factor_db():.1f}dB")
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
            "pilot_symbols": result.get("pilot_symbols"),
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


def _parse_bins(s: str) -> tuple[int, ...]:
    return tuple(int(b) for b in s.split(","))


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--fft-size", type=int, default=64)
    ap.add_argument("--cp-len", type=int, default=16)
    ap.add_argument("--active-bins", type=_parse_bins, default=None,
                     help="comma-separated FFT bin indices; default = all "
                          "in-band bins for --fft-size")
    ap.add_argument("--n-active", type=int, default=None,
                     help="if --active-bins omitted, pick this many centre "
                          "in-band bins instead of all of them")
    ap.add_argument("--bps", type=int, default=1, help="bits per subcarrier symbol: 1=BPSK 2=QPSK 3=8PSK 4=16QAM")
    ap.add_argument("--packet-bytes", type=int, default=16)
    ap.add_argument("--pilot-interval", type=int, default=0,
                     help="data OFDM symbols between pilot OFDM symbols; 0 disables")
    ap.add_argument("--n-preamble-symbols", type=int, default=2)
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--direction", choices=("ab",), default="ab",
                     help="ic7300(TX) -> ic705(RX) only. ic705 must never "
                          "transmit for this task; ba/both are removed.")
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--output-dir", type=Path)
    args = ap.parse_args(argv)

    in_band = ofdm.bins_in_band(args.fft_size)
    if args.active_bins is not None:
        active_bins = args.active_bins
    elif args.n_active is not None:
        # pick n_active centre-most in-band bins
        mid = len(in_band) // 2
        half = args.n_active // 2
        lo = max(0, mid - half)
        active_bins = tuple(in_band[lo:lo + args.n_active])
    else:
        active_bins = tuple(in_band)

    mode = ofdm.OFDMMode(fft_size=args.fft_size, cp_len=args.cp_len,
                          active_bins=active_bins, bits_per_symbol=args.bps,
                          packet_bytes=args.packet_bytes,
                          pilot_interval=args.pilot_interval,
                          n_preamble_symbols=args.n_preamble_symbols)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "mode_sweeps" / f"hf7_ofdm_v3-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("hf7_ofdm_v3 smoke test (from-scratch true OFDM, unregistered mode)")
    print(f"radios A={args.a}, B={args.b}; seed={args.seed}; trials={args.trials}")
    bin_hz = ofdm.DESIGN_RATE / args.fft_size
    active_hz = [round(b * bin_hz, 1) for b in mode.active_bins]
    print(f"fft_size={args.fft_size} (bin spacing {bin_hz:.1f} Hz) cp_len={args.cp_len} "
          f"active_bins={mode.active_bins} ({active_hz} Hz) bps={args.bps} "
          f"pilot_interval={args.pilot_interval}")
    print(f"frame: {mode.max_payload_bytes} B payload, {mode.frame_seconds():.3f}s, "
          f"crest factor {mode.crest_factor_db():.1f} dB")

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
        "config": {"fft_size": args.fft_size, "cp_len": args.cp_len,
                   "active_bins": list(mode.active_bins), "bits_per_symbol": args.bps,
                   "packet_bytes": args.packet_bytes, "pilot_interval": args.pilot_interval,
                   "n_preamble_symbols": args.n_preamble_symbols,
                   "crest_factor_db": mode.crest_factor_db()},
        "seed": args.seed, "trials": records,
        "summary": {"decoded": decoded, "total": total},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if decoded == total else 1


if __name__ == "__main__":
    sys.exit(main())
