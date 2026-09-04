"""Real-hardware trial runner for the 49-subcarrier true-OFDM mode (v6),
extending experiments/hf9_ofdm49_v5/hardware_test.py with higher-order
modulation (already-latent 16-QAM support in the shared symbol mapper)
and optional LDPC FEC (experiments/qpsk29/ldpc.py, reused read-only).

Run (from the repository root), e.g. v5's exact winning baseline:
    python experiments/hf10_ofdm49_v6/hardware_test.py --fft-size 240 --cp-len 60 \
        --bps 3 --packet-bytes 144 --pilot-interval 20 --trials 5

16-QAM re-test of the project's known real-hardware fragility, on THIS
49-bin design specifically:
    python experiments/hf10_ofdm49_v6/hardware_test.py --bps 4 --packet-bytes 144 \
        --pilot-interval 20 --trials 3

FEC (rate-3/4 LDPC) on top of either modulation:
    python experiments/hf10_ofdm49_v6/hardware_test.py --bps 4 --fec-rate 3/4 \
        --packet-bytes 182 --pilot-interval 20 --trials 3

NEW vs v5's hardware_test.py: every trial reports BOTH raw (pre-FEC,
hard-decision) BER and, when --fec-rate is set, residual (post-FEC-decode)
BER -- the informative FEC diagnostic the task asks for. When FEC is off,
the two numbers are computed against the same bit stream and should
match exactly (kept as a live consistency check on the harness itself).

SAFETY: the normal direction is IC-7300 -> IC-705 and opens the IC-705
receive-only. The reverse direction exists only behind the explicit
``--allow-ic705-tx`` acknowledgement; it opens the IC-7300 receive-only and
keys the IC-705. There is no bidirectional choice.
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
from experiments.hf10_ofdm49_v6 import ofdm49_v6 as ofdm49

DEFAULT_TRIALS = 3
DEFAULT_CAPTURE_TAIL = 1.0
DEFAULT_INTER_TRIAL = 0.5


def _compute_ber(truth_bits: np.ndarray, payload_len: int, rx_bits, *, length_bytes: int) -> dict:
    """BER over the payload-bit region only, using ground-truth framing
    (so a corrupted length field in the recovered packet can't misalign
    the comparison). Returns {"ber": float|None, "bit_errors": int|None,
    "bits_compared": int|None}."""
    if rx_bits is None:
        return {"ber": None, "bit_errors": None, "bits_compared": None}
    n = min(len(truth_bits), len(rx_bits))
    if n == 0:
        return {"ber": None, "bit_errors": None, "bits_compared": None}
    lo = length_bytes * 8
    hi = min(n, (length_bytes + payload_len) * 8)
    if hi <= lo:
        # payload region not even fully transmitted/recovered (very short
        # capture) -- fall back to comparing whatever whole bits we do
        # have so a number is still reported, but bits_compared flags it
        lo, hi = 0, n
    truth_slice = truth_bits[lo:hi]
    rx_slice = np.asarray(rx_bits[lo:hi])
    errors = int(np.sum(truth_slice != rx_slice))
    bits_compared = int(hi - lo)
    return {"ber": errors / bits_compared, "bit_errors": errors, "bits_compared": bits_compared}


def run_direction(tx, rx, direction, mode, trials, seed, *, capture_tail, inter_trial,
                  capture_dir=None):
    payload_bytes = mode.max_payload_bytes
    fec_text = mode.fec_rate or "none"
    print(f"\n  {direction}: {trials} x {payload_bytes} B, fft={mode.fft_size} "
          f"cp={mode.cp_len} n_active={mode.n_active} n_comb_pilot={mode.n_comb()} "
          f"bps={mode.bits_per_symbol} fec={fec_text} pilot_interval={mode.pilot_interval} "
          f"equalizer={mode.equalizer} frame={mode.frame_seconds():.3f}s "
          f"crest={mode.crest_factor_db():.1f}dB")
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

        # Capture level, before anything looks at the bits. An OFDM signal
        # with a ~10 dB crest factor dies fast when the receive audio chain
        # clips, and a clipped capture otherwise presents as an ordinary
        # decode failure. peak at/above 1.0 means the ADC or the float path
        # ran out of headroom; RMS at the noise floor means nothing arrived.
        cap = np.asarray(captured, dtype=np.float64)
        cap_rms = float(np.sqrt(np.mean(cap ** 2))) if cap.size else None
        cap_peak = float(np.max(np.abs(cap))) if cap.size else None
        cap_clipped = int(np.sum(np.abs(cap) >= 0.999)) if cap.size else None

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

        # Raw (pre-FEC, hard-decision) BER: compared against the full
        # ground-truth CODED bit stream (not payload-windowed -- a
        # systematic LDPC codeword interleaves info/parity bit *positions*
        # across concatenated codewords, so a length/payload-offset window
        # is only meaningful in the uncoded case). This is the informative
        # "how noisy was the raw channel" number FEC diagnostics need.
        raw_bits_out = result.get("pre_fec_bits")
        if raw_bits_out is None:
            raw_ber_info = {"ber": None, "bit_errors": None, "bits_compared": None}
        else:
            n = min(len(truth_coded), len(raw_bits_out))
            errors = int(np.sum(truth_coded[:n] != np.asarray(raw_bits_out[:n])))
            raw_ber_info = {"ber": (errors / n) if n else None, "bit_errors": errors, "bits_compared": n}

        # Residual (post-FEC-decode when FEC is on; identical to raw_ber
        # when FEC is off) BER, in the payload-bit domain -- v5's original
        # metric, preserved.
        post_ber_info = _compute_ber(truth_raw, len(payload), result.get("raw_bits"),
                                      length_bytes=ofdm49.LENGTH_BYTES)

        net_bps = (payload_bytes * 8) / mode.frame_seconds() if decoded else 0.0
        record = {
            "trial": trial, "direction": direction, "payload_bytes": payload_bytes,
            "keyed_seconds": keyed, "rx_samples_12k": len(captured),
            "outcome": outcome, "confidence": result.get("confidence"),
            "freq_offset_hz": result.get("freq_offset_hz"),
            "channel_snr_db": result.get("channel_snr_db"),
            "per_bin_snr_db_min": result.get("per_bin_snr_db_min"),
            "per_bin_snr_db_median": result.get("per_bin_snr_db_median"),
            "per_bin_snr_db_max": result.get("per_bin_snr_db_max"),
            "per_bin_snr_db": result.get("per_bin_snr_db"),
            "capture_rms": cap_rms, "capture_peak": cap_peak,
            "capture_clipped_samples": cap_clipped,
            "pilot_symbols": result.get("pilot_symbols"),
            "phase_slope_rad_per_bin": result.get("phase_slope_rad_per_bin"),
            "ldpc_ok": result.get("ldpc_ok"), "ldpc_iterations": result.get("ldpc_iterations"),
            "net_bps": net_bps,
            "raw_ber": raw_ber_info["ber"], "raw_bit_errors": raw_ber_info["bit_errors"],
            "raw_bits_compared": raw_ber_info["bits_compared"],
            "ber": post_ber_info["ber"], "bit_errors": post_ber_info["bit_errors"],
            "bits_compared": post_ber_info["bits_compared"],
        }
        # Raw 12 kHz capture, kept so a disputed trial can be re-demodulated
        # offline without re-keying a radio. Optional: callers that pass no
        # capture_dir behave exactly as before.
        if capture_dir is not None:
            cap_path = Path(capture_dir) / f"trial{trial:02d}.npy"
            np.save(cap_path, cap.astype(np.float32))
            record["capture_file"] = cap_path.name

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
        ldpc_text = "" if not mode.fec_rate else f" ldpc_ok={record['ldpc_ok']} iters={record['ldpc_iterations']}"
        lvl_text = ("" if cap_rms is None
                    else f" rms={cap_rms:.4f} peak={cap_peak:.3f}"
                         + (f" CLIP({cap_clipped})" if cap_clipped else ""))
        bsnr_lo = record["per_bin_snr_db_min"]
        bsnr_text = ("" if bsnr_lo is None
                     else f" binsnr={bsnr_lo:.1f}/{record['per_bin_snr_db_median']:.1f}/"
                          f"{record['per_bin_snr_db_max']:.1f}dB")
        print(f"    {trial}/{trials}: keyed={keyed:.2f}s rx={len(captured)}{lvl_text} "
              f"conf={conf_text}{snr_text}{bsnr_text}{foff_text} {raw_ber_text} {ber_text}{ldpc_text} {outcome}")
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
    ap.add_argument("--fec-rate", choices=("1/2", "2/3", "3/4"), default=None,
                     help="IEEE 802.11n QC-LDPC rate applied to the packet "
                          "bit stream (experiments/qpsk29/ldpc.py); default "
                          "None = no FEC (v5's original behaviour)")
    ap.add_argument("--n-preamble-symbols", type=int, default=2)
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--direction", choices=("ab", "ba"), default="ab",
                     help="ab is the safe default; ba additionally requires "
                          "--allow-ic705-tx")
    ap.add_argument("--allow-ic705-tx", action="store_true",
                    help="explicit acknowledgement required for direction ba")
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--label", default="")
    ap.add_argument("--save-captures", action="store_true",
                     help="write each trial's raw 12 kHz capture to "
                          "<output-dir>/captures/trialNN.npy for offline re-analysis")
    args = ap.parse_args(argv)
    if args.direction == "ba" and not args.allow_ic705_tx:
        ap.error("--direction ba requires explicit --allow-ic705-tx")
    if args.allow_ic705_tx and args.direction != "ba":
        ap.error("--allow-ic705-tx is valid only with --direction ba")

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
                              edge_taper=args.edge_taper,
                              fec_rate=args.fec_rate)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "mode_sweeps" / f"hf10_ofdm49_v6-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    capture_dir = None
    if args.save_captures:
        capture_dir = output_dir / "captures"
        capture_dir.mkdir(parents=True, exist_ok=True)

    print(f"hf10_ofdm49_v6 hardware test {args.label}")
    print(f"radios A={args.a}, B={args.b}; seed={args.seed}; trials={args.trials}")
    bin_hz = ofdm49.DESIGN_RATE / args.fft_size
    active_hz = [round(b * bin_hz, 1) for b in mode.active_bins]
    print(f"fft_size={args.fft_size} (bin spacing {bin_hz:.1f} Hz) cp_len={args.cp_len} "
          f"n_active={mode.n_active} n_comb={mode.n_comb()} range=[{active_hz[0]},{active_hz[-1]}]Hz "
          f"bps={args.bps} fec_rate={args.fec_rate} pilot_interval={args.pilot_interval} equalizer={args.equalizer}")
    print(f"frame: {mode.max_payload_bytes} B payload, {mode.frame_seconds():.3f}s, "
          f"crest factor {mode.crest_factor_db():.1f} dB, "
          f"net_bps_if_all_decode={(mode.max_payload_bytes*8)/mode.frame_seconds():.1f}")

    records = []
    # Whichever station receives is opened without a PTT backend. Reverse
    # operation additionally passed the explicit CLI acknowledgement above.
    pair_options = ({"b_receive_only": True} if args.direction == "ab" else
                    {"a_receive_only": True})
    with pair_factory(args.a, args.b, warmup=3.0,
                      **pair_options) as (transport_a, transport_b):
        if args.direction == "ab":
            tx, rx, direction = transport_a, transport_b, f"A:{args.a}->B:{args.b}"
        else:
            tx, rx, direction = transport_b, transport_a, f"B:{args.b}->A:{args.a}"
        records.extend(run_direction(
            tx, rx, direction, mode, args.trials, args.seed,
            capture_tail=args.capture_tail, inter_trial=args.inter_trial,
            capture_dir=capture_dir))

    decoded = sum(1 for r in records if r["outcome"] == "decoded")
    total = len(records)
    bers = [r["ber"] for r in records if r["ber"] is not None]
    raw_bers = [r["raw_ber"] for r in records if r["raw_ber"] is not None]
    mean_ber = float(np.mean(bers)) if bers else None
    mean_raw_ber = float(np.mean(raw_bers)) if raw_bers else None
    net_bps = (mode.max_payload_bytes * 8 / mode.frame_seconds()) if decoded == total and total > 0 else None
    peaks = [r["capture_peak"] for r in records if r["capture_peak"] is not None]
    rmss = [r["capture_rms"] for r in records if r["capture_rms"] is not None]
    clipped_trials = sum(1 for r in records if r["capture_clipped_samples"])
    foffs = [r["freq_offset_hz"] for r in records if r["freq_offset_hz"] is not None]
    bmins = [r["per_bin_snr_db_min"] for r in records if r["per_bin_snr_db_min"] is not None]
    bmeds = [r["per_bin_snr_db_median"] for r in records if r["per_bin_snr_db_median"] is not None]
    bmaxs = [r["per_bin_snr_db_max"] for r in records if r["per_bin_snr_db_max"] is not None]
    levels = {
        "max_capture_peak": max(peaks) if peaks else None,
        "mean_capture_rms": float(np.mean(rmss)) if rmss else None,
        "clipped_trials": clipped_trials,
        "mean_freq_offset_hz": float(np.mean(foffs)) if foffs else None,
        "mean_per_bin_snr_db_min": float(np.mean(bmins)) if bmins else None,
        "mean_per_bin_snr_db_median": float(np.mean(bmeds)) if bmeds else None,
        "mean_per_bin_snr_db_max": float(np.mean(bmaxs)) if bmaxs else None,
    }
    print(f"\n== RESULTS == {decoded}/{total} decoded; mean_raw_ber={mean_raw_ber}; "
          f"mean_post_ber={mean_ber}; net_bps={net_bps}")
    if peaks:
        print(f"   levels: max_peak={max(peaks):.3f} mean_rms={np.mean(rmss):.4f} "
              f"clipped_trials={clipped_trials}/{total}")
    if bmins:
        print(f"   per-carrier SNR (mean over trials): min={np.mean(bmins):.1f} "
              f"median={np.mean(bmeds):.1f} max={np.mean(bmaxs):.1f} dB")

    out = {
        "note": "Provisional smoke evidence only, not a qualification run.",
        "label": args.label,
        "channel": {"type": "hardware", "radio_a": args.a, "radio_b": args.b},
        "config": {"fft_size": args.fft_size, "cp_len": args.cp_len,
                   "active_bins": list(mode.active_bins), "bits_per_symbol": args.bps,
                   "fec_rate": args.fec_rate,
                   "packet_bytes": args.packet_bytes, "pilot_interval": args.pilot_interval,
                   "pilot_comb_stride": args.pilot_comb_stride, "equalizer": args.equalizer,
                   "edge_guard_bins": args.edge_guard_bins, "edge_taper": args.edge_taper,
                   "n_preamble_symbols": args.n_preamble_symbols,
                   "crest_factor_db": mode.crest_factor_db(),
                   "frame_seconds": mode.frame_seconds(),
                   "max_payload_bytes": mode.max_payload_bytes},
        "seed": args.seed, "trials": records,
        "summary": {"decoded": decoded, "total": total, "mean_ber": mean_ber,
                     "mean_raw_ber": mean_raw_ber, "net_bps": net_bps, **levels},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if decoded == total else 1


if __name__ == "__main__":
    sys.exit(main())
