"""Measure EVM vs cyclic-prefix length on hf10's original 49-bin geometry.

The question this experiment exists to answer: 49 contiguous carriers
filling 300-2700 Hz forces 50.0 Hz spacing and therefore a 20 ms useful
symbol, so "49 carriers at 50 symbols/s" is by definition the cp_len=0
case.  hf10's own record (RESULTS.md, 2026-09-07) measured that shrinking
cp_len from 60 to 30 costs 2 dB of EVM because the guard is covering the
two radios' SSB filter impulse responses -- but it never measured how far
down that cost keeps growing, or where it becomes fatal.

This driver sweeps cp_len over one radio session and reports hf10's own
EVM decomposition at each point.  It measures the impairment directly
rather than inferring it from frame decode rate, which is far cheaper in
airtime: a config whose residual-after-CPE-and-timing EVM sits below the
constellation's requirement cannot be rescued by FEC, drive or pilots,
because loss of subcarrier orthogonality is not additive noise.

Values are visited ROUND-ROBIN (all cp values once, then again) rather
than blocked, so the session-to-session channel drift this project has
repeatedly found (hf8, hf9, hf11) hits every arm of the comparison
equally instead of confounding one of them.

The probe frame is QPSK (bps=2) exactly as hf10's evm_probe.py uses it:
the truth symbols must stay recoverable even when the error is large, so
the measurement does not depend on the frame decoding.

  python experiments/hf18_ofdm49_vara/cp_sweep.py --rounds 2

`experiments/hf10_ofdm49_v6/{ofdm49_v6,evm_probe}.py` are imported
read-only and unmodified; no PHY or analysis code is written here.
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
from whale.phy import ofdm49 as ofdm49
from experiments.hf10_ofdm49_v6.evm_probe import analyse

DEFAULT_CP_LENS = (60, 36, 24, 12, 0)
DEFAULT_OUTPUT_ROOT = Path("logs") / "mode_qualification" / "hf-ssb" / "hf18"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--fft-size", type=int, default=240)
    ap.add_argument("--cp-lens", type=lambda s: tuple(int(x) for x in s.split(",")),
                    default=DEFAULT_CP_LENS)
    ap.add_argument("--bps", type=int, default=2)
    ap.add_argument("--packet-bytes", type=int, default=1200)
    ap.add_argument("--pilot-interval", type=int, default=20)
    ap.add_argument("--drive-scale", type=float, default=0.008,
                    help="hf10's measured EVM optimum; the 1.0 default of the "
                         "older harnesses is ~7 dB overdriven on this path")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--seed", type=int, default=20260907)
    ap.add_argument("--capture-tail", type=float, default=1.0)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args(argv)

    bins = tuple(ofdm49.bins_in_band(args.fft_size))
    modes = {}
    for cp in args.cp_lens:
        modes[cp] = ofdm49.OFDM49Mode(
            fft_size=args.fft_size, cp_len=cp, active_bins=bins,
            bits_per_symbol=args.bps, packet_bytes=args.packet_bytes,
            pilot_interval=args.pilot_interval, drive_scale=args.drive_scale)

    bin_hz = ofdm49.DESIGN_RATE / args.fft_size
    print(f"hf18 cp sweep: fft={args.fft_size} ({bin_hz:.1f} Hz spacing) "
          f"bins={len(bins)} [{bins[0]*bin_hz:.0f}-{bins[-1]*bin_hz:.0f} Hz] "
          f"drive={args.drive_scale}")
    for cp, m in modes.items():
        print(f"  cp={cp:3d} ({1000 * cp / ofdm49.DESIGN_RATE:4.1f} ms guard)  "
              f"sym/s={ofdm49.DESIGN_RATE / (args.fft_size + cp):5.1f}  "
              f"frame={m.frame_seconds():.3f}s  crest={m.crest_factor_db():.1f} dB")

    reports = []
    with bench.radio_pair(args.a, args.b, b_receive_only=True) as (tx, rx):
        for rnd in range(1, args.rounds + 1):
            for cp in args.cp_lens:
                mode = modes[cp]
                stale = rx.snapshot_rx()
                rx.consume_rx(len(stale))
                rng = np.random.default_rng(np.random.SeedSequence([args.seed, rnd, cp]))
                payload = rng.integers(0, 256, mode.max_payload_bytes,
                                       dtype=np.uint8).tobytes()
                keyed = tx.send(mode.modulate(payload))
                time.sleep(args.capture_tail)
                cap = np.asarray(rx.snapshot_rx(), dtype=np.float64)
                res = mode.demodulate(cap, diagnostics=True)
                if not res.get("synced"):
                    print(f"  r{rnd} cp={cp:3d}: NO SYNC (conf={res['confidence']:.3f})")
                    reports.append({"round": rnd, "cp_len": cp, "synced": False,
                                    "confidence": float(res["confidence"])})
                    time.sleep(0.5)
                    continue
                rep = analyse(mode, res, payload)
                rep.update(round=rnd, cp_len=cp, synced=True, keyed_seconds=keyed,
                           decoded=bool(res.get("payload") == payload),
                           capture_peak=float(np.max(np.abs(cap))),
                           channel_snr_db=res.get("channel_snr_db"))
                reports.append(rep)
                print(f"  r{rnd} cp={cp:3d}: evm_snr={rep['overall_snr_db']:5.1f} dB "
                      f"(+cpe {rep['after_cpe_removal_db']:5.1f} "
                      f"+timing {rep['after_cpe_and_timing_db']:5.1f}) "
                      f"decoded={rep['decoded']} "
                      f"drift={rep['clock_offset_ppm']:+.1f}ppm "
                      f"q1/q4={rep['first_quarter_snr_db']:.1f}/"
                      f"{rep['last_quarter_snr_db']:.1f} dB")
                time.sleep(0.5)

    print("\ncp_len | guard  | sym/s | trials | mean EVM SNR | after cpe+timing")
    for cp in args.cp_lens:
        good = [r for r in reports if r["cp_len"] == cp and r.get("synced")]
        head = (f"{cp:6d} | {1000 * cp / ofdm49.DESIGN_RATE:4.1f} ms | "
                f"{ofdm49.DESIGN_RATE / (args.fft_size + cp):5.1f} |")
        if not good:
            print(f"{head}      0 | (no sync)")
            continue
        print(f"{head} {len(good):6d} | "
              f"{np.mean([r['overall_snr_db'] for r in good]):8.1f} dB | "
              f"{np.mean([r['after_cpe_and_timing_db'] for r in good]):8.1f} dB")

    out = args.output or (REPOSITORY_ROOT / DEFAULT_OUTPUT_ROOT
                          / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-cp-sweep.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"config": {"fft_size": args.fft_size,
                                          "cp_lens": list(args.cp_lens),
                                          "bps": args.bps,
                                          "packet_bytes": args.packet_bytes,
                                          "pilot_interval": args.pilot_interval,
                                          "drive_scale": args.drive_scale,
                                          "rounds": args.rounds},
                               "direction": f"{args.a}->{args.b}",
                               "trials": reports}, indent=1))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
