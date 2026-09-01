"""Real-hardware trial runner for the 49-subcarrier (and configurable)
true-OFDM mode (v5), extending experiments/hf7_ofdm_v3/hardware_test.py.

Run (from the repository root):
    python experiments/hf9_ofdm49_v5/hardware_test.py --fft-size 240 --cp-len 60 \
        --active-bins 6-54 --bps 1 --packet-bytes 20 --trials 3

NEW vs hf7's hardware_test.py: every trial additionally reports BER
(bit error rate) computed against the ground-truth payload bits,
whenever the demodulator produced ANY recovered bit sequence (even on
a crc_fail/no_sync-with-partial-bits case) -- not just full decodes.
If there is truly no bit-level output (total sync failure), BER is
recorded as null.

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
from experiments.hf9_ofdm49_v5 import ofdm49

DEFAULT_TRIALS = 3
DEFAULT_CAPTURE_TAIL = 1.0
DEFAULT_INTER_TRIAL = 0.5


def _ground_truth_packet_bits(mode, payload: bytes) -> np.ndarray:
    packet = ofdm49._pack_packet(payload, mode.packet_bytes)
    return np.unpackbits(np.frombuffer(packet, dtype=np.uint8))


def _compute_ber(mode, payload: bytes, raw_bits) -> dict:
    """BER over the payload-bit region only, using ground-truth framing
    (so a corrupted length field in the recovered packet can't misalign
    the comparison). Returns {"ber": float|None, "bit_errors": int|None,
    "bits_compared": int|None}."""
    if raw_bits is None:
        return {"ber": None, "bit_errors": None, "bits_compared": None}
    truth = _ground_truth_packet_bits(mode, payload)
    n = min(len(truth), len(raw_bits))
    if n == 0:
        return {"ber": None, "bit_errors": None, "bits_compared": None}
    lo = ofdm49.LENGTH_BYTES * 8
    hi = min(n, (ofdm49.LENGTH_BYTES + len(payload)) * 8)
    if hi <= lo:
        # payload region not even fully transmitted/recovered (very short
        # capture) -- fall back to comparing whatever whole-packet bits we
        # do have so a number is still reported, but flag it via bits_compared
        lo, hi = 0, n
    truth_slice = truth[lo:hi]
    rx_slice = np.asarray(raw_bits[lo:hi])
    errors = int(np.sum(truth_slice != rx_slice))
    bits_compared = int(hi - lo)
    return {"ber": errors / bits_compared, "bit_errors": errors, "bits_compared": bits_compared}


def run_direction(tx, rx, direction, mode, trials, seed, *, capture_tail, inter_trial):
    payload_bytes = mode.max_payload_bytes
    print(f"\n  {direction}: {trials} x {payload_bytes} B, fft={mode.fft_size} "
          f"cp={mode.cp_len} n_active={mode.n_active} n_comb_pilot={mode.n_comb()} "
          f"bps={mode.bits_per_symbol} pilot_interval={mode.pilot_interval} "
          f"equalizer={mode.equalizer} frame={mode.frame_seconds():.3f}s "
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

        ber_info = _compute_ber(mode, payload, result.get("raw_bits"))

        net_bps = (payload_bytes * 8) / mode.frame_seconds() if decoded else 0.0
        record = {
            "trial": trial, "direction": direction, "payload_bytes": payload_bytes,
            "keyed_seconds": keyed, "rx_samples_12k": len(captured),
            "outcome": outcome, "confidence": result.get("confidence"),
            "freq_offset_hz": result.get("freq_offset_hz"),
            "channel_snr_db": result.get("channel_snr_db"),
            "pilot_symbols": result.get("pilot_symbols"),
            "phase_slope_rad_per_bin": result.get("phase_slope_rad_per_bin"),
            "net_bps": net_bps,
            "ber": ber_info["ber"], "bit_errors": ber_info["bit_errors"],
            "bits_compared": ber_info["bits_compared"],
        }
        records.append(record)
        conf = record["confidence"]
        conf_text = "n/a" if conf is None else f"{conf:.3f}"
        snr = record["channel_snr_db"]
        snr_text = "" if snr is None else f" snr={snr:.1f}dB"
        foff = record["freq_offset_hz"]
        foff_text = "" if foff is None else f" foff={foff:.2f}Hz"
        ber = record["ber"]
        ber_text = "ber=n/a" if ber is None else f"ber={ber:.4f}({record['bit_errors']}/{record['bits_compared']})"
        print(f"    {trial}/{trials}: keyed={keyed:.2f}s rx={len(captured)} "
              f"conf={conf_text}{snr_text}{foff_text} {ber_text} {outcome}")
        if trial != trials:
            time.sleep(inter_trial)
    return records


def _parse_bins(s: str) -> tuple[int, ...]:
    if "-" in s and "," not in s:
        lo, hi = s.split("-")
        return tuple(range(int(lo), int(hi) + 1))
    return tuple(int(b) for b in s.split(","))


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--fft-size", type=int, default=240)
    ap.add_argument("--cp-len", type=int, default=60)
    ap.add_argument("--active-bins", type=_parse_bins, default=None,
                     help="comma-separated bin indices, or lo-hi range; "
                          "default = all in-band bins for --fft-size")
    ap.add_argument("--n-active", type=int, default=None)
    ap.add_argument("--bps", type=int, default=1, help="bits per subcarrier symbol")
    ap.add_argument("--packet-bytes", type=int, default=16)
    ap.add_argument("--pilot-interval", type=int, default=0)
    ap.add_argument("--pilot-comb-stride", type=int, default=0)
    ap.add_argument("--equalizer", choices=("gain", "phase_slope"), default="gain")
    ap.add_argument("--edge-guard-bins", type=int, default=0)
    ap.add_argument("--edge-taper", type=int, default=0)
    ap.add_argument("--n-preamble-symbols", type=int, default=2)
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

    in_band = ofdm49.bins_in_band(args.fft_size)
    if args.active_bins is not None:
        active_bins = args.active_bins
    elif args.n_active is not None:
        mid = len(in_band) // 2
        half = args.n_active // 2
        lo = max(0, mid - half)
        active_bins = tuple(in_band[lo:lo + args.n_active])
    else:
        active_bins = tuple(in_band)

    mode = ofdm49.OFDM49Mode(fft_size=args.fft_size, cp_len=args.cp_len,
                              active_bins=active_bins, bits_per_symbol=args.bps,
                              packet_bytes=args.packet_bytes,
                              pilot_interval=args.pilot_interval,
                              n_preamble_symbols=args.n_preamble_symbols,
                              equalizer=args.equalizer,
                              pilot_comb_stride=args.pilot_comb_stride,
                              edge_guard_bins=args.edge_guard_bins,
                              edge_taper=args.edge_taper)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "mode_sweeps" / f"hf9_ofdm49_v5-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"hf9_ofdm49_v5 hardware test {args.label}")
    print(f"radios A={args.a}, B={args.b}; seed={args.seed}; trials={args.trials}")
    bin_hz = ofdm49.DESIGN_RATE / args.fft_size
    active_hz = [round(b * bin_hz, 1) for b in mode.active_bins]
    print(f"fft_size={args.fft_size} (bin spacing {bin_hz:.1f} Hz) cp_len={args.cp_len} "
          f"n_active={mode.n_active} n_comb={mode.n_comb()} range=[{active_hz[0]},{active_hz[-1]}]Hz "
          f"bps={args.bps} pilot_interval={args.pilot_interval} equalizer={args.equalizer}")
    print(f"frame: {mode.max_payload_bytes} B payload, {mode.frame_seconds():.3f}s, "
          f"crest factor {mode.crest_factor_db():.1f} dB")

    records = []
    with pair_factory(args.a, args.b, warmup=3.0) as (transport_a, transport_b):
        records.extend(run_direction(
            transport_a, transport_b, f"A:{args.a}->B:{args.b}", mode,
            args.trials, args.seed,
            capture_tail=args.capture_tail, inter_trial=args.inter_trial))

    decoded = sum(1 for r in records if r["outcome"] == "decoded")
    total = len(records)
    bers = [r["ber"] for r in records if r["ber"] is not None]
    mean_ber = float(np.mean(bers)) if bers else None
    print(f"\n== RESULTS == {decoded}/{total} decoded; mean_ber={mean_ber}")

    out = {
        "note": "Provisional smoke evidence only, not a qualification run.",
        "label": args.label,
        "channel": {"type": "hardware", "radio_a": args.a, "radio_b": args.b},
        "config": {"fft_size": args.fft_size, "cp_len": args.cp_len,
                   "active_bins": list(mode.active_bins), "bits_per_symbol": args.bps,
                   "packet_bytes": args.packet_bytes, "pilot_interval": args.pilot_interval,
                   "pilot_comb_stride": args.pilot_comb_stride, "equalizer": args.equalizer,
                   "edge_guard_bins": args.edge_guard_bins, "edge_taper": args.edge_taper,
                   "n_preamble_symbols": args.n_preamble_symbols,
                   "crest_factor_db": mode.crest_factor_db()},
        "seed": args.seed, "trials": records,
        "summary": {"decoded": decoded, "total": total, "mean_ber": mean_ber},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if decoded == total else 1


if __name__ == "__main__":
    sys.exit(main())
