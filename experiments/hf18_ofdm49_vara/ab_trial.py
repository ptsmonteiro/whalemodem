"""Interleaved A/B reliability trial: hf10's 97-carrier geometry vs hf18's
49-carrier geometry, both resized to the same ~5 s air time.

Two things motivate this over the runs recorded in RESULTS.md:

1. **Air time, not frame time.** Every keying costs a fixed ~155 ms of PTT
   ramp/tail on this bench (measured, flat across every config and drive
   level tried). At the 2.4 kB frame size that is 5.5% of occupancy, and
   it is paid identically by both geometries, so it inflates neither's
   advantage but does mean neither mode actually moved 7 kbps through the
   channel. Doubling the frame amortizes it to ~3%.

2. **Sample size.** The RESULTS.md verdict rests on 20/20 vs 27/30, which
   Fisher's exact puts at p = 0.27 -- the delivery-rate gap is not
   established at those sizes. 100 trials per arm can separate a 97.5%
   arm from a 90% arm decisively.

Both payloads are chosen at rate-3/4 LDPC codeword-packing boundaries
(k=486 info bits/codeword), the principle hf10 and hf11 both used, and
sized so the two arms land within ~65 ms of each other in air time:

  hf10  fft 480 / cp 60 / 97 bins : 75 codewords, 4550 B, 5.060 s air
  hf18  fft 240 / cp 24 / 49 bins : 78 codewords, 4732 B, 4.995 s air

Arms alternate TRIAL BY TRIAL within a single radio session, and the
order within each round flips on odd/even rounds, so neither channel
drift (hf8, hf9, hf11 all found it session to session) nor any
first-in-round ordering effect can land on one arm rather than the other.
Blocked runs -- 100 of one then 100 of the other -- could not distinguish
a geometry difference from a 10-minute change in the ionosphere.

Note the frame sizes here are ~1.9x the largest hf11 tested and carry
75-78 LDPC codewords against hf10's 40. hf11 found frame delivery falls
as codeword count rises (a frame needs EVERY codeword to converge), so
BOTH arms are expected to deliver less reliably than their 2.4 kB
numbers. That is the point: the question is whether the gap between them
survives at the frame size that actually amortizes the keying overhead.

  python experiments/hf18_ofdm49_vara/ab_trial.py --rounds 100

`experiments/hf10_ofdm49_v6/{ofdm49_v6,hardware_test}.py` are imported
read-only and unmodified.
"""

from __future__ import annotations

import argparse
import contextlib
import io
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
from experiments.hf10_ofdm49_v6.hardware_test import run_direction

KEYING_OVERHEAD_S = 0.155   # measured on this bench, flat across configs

# (name, fft_size, cp_len, packet_bytes, drive_scale)
ARMS = (
    ("hf10", 480, 60, 4556, 0.008),
    ("hf18", 240, 24, 4738, 0.008),
)
DEFAULT_OUTPUT_ROOT = Path("logs") / "mode_qualification" / "hf-ssb" / "hf18"


def build(fft, cp, pkt, drive, pilot_interval):
    return ofdm49.OFDM49Mode(
        fft_size=fft, cp_len=cp, active_bins=tuple(ofdm49.bins_in_band(fft)),
        bits_per_symbol=5, packet_bytes=pkt, pilot_interval=pilot_interval,
        fec_rate="3/4", interleave=True, n_preamble_symbols=2, drive_scale=drive)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--rounds", type=int, default=100,
                    help="trials PER ARM; each round runs both arms once")
    ap.add_argument("--pilot-interval", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260907)
    ap.add_argument("--capture-tail", type=float, default=1.0)
    ap.add_argument("--inter-trial", type=float, default=0.5)
    ap.add_argument("--output-dir", type=Path)
    args = ap.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.output_dir or (REPOSITORY_ROOT / DEFAULT_OUTPUT_ROOT / f"{stamp}-ab100")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "trials.log"

    modes = {name: build(fft, cp, pkt, drive, args.pilot_interval)
             for name, fft, cp, pkt, drive in ARMS}
    for (name, fft, cp, pkt, drive) in ARMS:
        m = modes[name]
        air = m.frame_seconds() + KEYING_OVERHEAD_S
        print(f"{name}: fft={fft} cp={cp} bins={m.n_active} "
              f"payload={m.max_payload_bytes} B codewords={m.n_codewords} "
              f"frame={m.frame_seconds():.3f}s air~{air:.3f}s "
              f"crest={m.crest_factor_db():.1f}dB "
              f"net_frame={m.max_payload_bytes * 8 / m.frame_seconds():.1f} "
              f"net_air~{m.max_payload_bytes * 8 / air:.1f} bps")
    est = args.rounds * sum(modes[n].frame_seconds() + KEYING_OVERHEAD_S
                            + args.capture_tail + args.inter_trial for n, *_ in ARMS)
    print(f"{args.rounds} rounds x {len(ARMS)} arms; estimated {est / 60:.0f} min\n")

    records = {name: [] for name, *_ in ARMS}
    decoder_options = {"gain_smoothing": 1, "noise_estimator": "repeat"}
    log = log_path.open("w", encoding="utf-8")
    try:
        with bench.radio_pair(args.a, args.b, warmup=3.0,
                              b_receive_only=True) as (tx, rx):
            for rnd in range(1, args.rounds + 1):
                # flip arm order every other round so neither arm is always
                # the one that follows the inter-round gap
                order = ARMS if rnd % 2 else tuple(reversed(ARMS))
                for (name, *_rest) in order:
                    mode = modes[name]
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        recs = run_direction(
                            tx, rx, f"A:{args.a}->B:{args.b}", mode, 1,
                            args.seed + rnd * 100,
                            capture_tail=args.capture_tail,
                            inter_trial=args.inter_trial,
                            decoder_options=decoder_options)
                    log.write(f"[round {rnd} arm {name}]\n{buf.getvalue()}")
                    log.flush()
                    r = recs[0]
                    r["round"] = rnd
                    records[name].append(r)
                    ok = sum(1 for x in records[name] if x["outcome"] == "decoded")
                    print(f"r{rnd:3d} {name}: {r['outcome']:15s} "
                          f"raw_ber={r['raw_ber']:.4f} "
                          f"snr={r['channel_snr_db']:.1f}dB  "
                          f"running {ok}/{len(records[name])}", flush=True)
                    time.sleep(args.inter_trial)
    finally:
        log.close()

    print("\n== SUMMARY ==")
    print(f"{'arm':6s} {'delivered':>11s} {'rate':>7s} {'mean raw BER':>13s} "
          f"{'air s':>7s} {'net_air':>9s} {'effective':>10s}")
    summary = {}
    for (name, *_rest) in ARMS:
        rs = records[name]
        m = modes[name]
        ok = sum(1 for x in rs if x["outcome"] == "decoded")
        keyed = float(np.mean([x["keyed_seconds"] for x in rs]))
        raw = float(np.mean([x["raw_ber"] for x in rs if x["raw_ber"] is not None]))
        net_air = m.max_payload_bytes * 8 / keyed
        summary[name] = {"delivered": ok, "trials": len(rs), "mean_raw_ber": raw,
                         "mean_keyed_seconds": keyed, "net_air_bps": net_air,
                         "effective_bps": net_air * ok / len(rs),
                         "payload_bytes": m.max_payload_bytes,
                         "frame_seconds": m.frame_seconds(),
                         "n_codewords": m.n_codewords}
        print(f"{name:6s} {ok:5d}/{len(rs):-5d} {100 * ok / len(rs):6.1f}% "
              f"{raw:13.4f} {keyed:7.3f} {net_air:9.1f} "
              f"{net_air * ok / len(rs):10.1f}")

    try:
        from scipy.stats import fisher_exact
        a, b = [summary[n] for n, *_ in ARMS]
        _, p = fisher_exact([[a["delivered"], a["trials"] - a["delivered"]],
                             [b["delivered"], b["trials"] - b["delivered"]]])
        print(f"\nFisher exact on delivery: p = {p:.4f}")
        summary["fisher_p"] = float(p)
    except Exception as exc:  # scipy optional
        print(f"(fisher test unavailable: {exc})")

    (out_dir / "result.json").write_text(json.dumps(
        {"config": {"arms": [{"name": n, "fft_size": f, "cp_len": c,
                              "packet_bytes": p, "drive_scale": d} for n, f, c, p, d in ARMS],
                    "rounds": args.rounds, "pilot_interval": args.pilot_interval,
                    "seed": args.seed, "decoder_options": decoder_options},
         "direction": f"{args.a}->{args.b}",
         "summary": summary,
         "records": records}, indent=1))
    print(f"\nwrote {out_dir / 'result.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
