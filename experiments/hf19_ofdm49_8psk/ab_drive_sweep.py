"""Interleaved HF8 vs HF7 transmit-drive sweep: locating each mode's
hardware breaking point on the same path, in the same session.

HF8's claim from `sweep.py` is a *floor*: 90% frame delivery 8 dB below
HF7's, in simulation. This bench cannot test that at full drive. It is a
short, benign, strongly-coupled path -- prior hf18 runs and this
experiment's own smoke run both sit around 24 dB reported SNR with the
receive audio clipping -- where HF7 already delivers 94/100. Running both
modes at full drive would show them both near 100% and prove nothing about
the claim.

So this harness walks the transmit audio down instead, which is the method
`scripts/hf4_tx_volume_sweep.py` established here for locating a receiver
breakpoint on real hardware, and reads off where each mode stops
delivering. The difference between those two points is the hardware
analogue of the simulated 8 dB.

Design, following `experiments/hf18_ofdm49_vara/ab_trial.py`:

  * **Arms alternate trial by trial** inside one radio session, with the
    order flipping every round, so neither channel drift nor a
    first-in-round ordering effect can land on one arm. hf8, hf9 and hf11
    each found session-to-session drift on this bench; a blocked design
    cannot tell drift from a mode difference.
  * **The drive level is the outer loop and both arms see every level**, so
    the two curves are measured through the same path minute by minute.
  * Levels are multiplicative on top of each mode's own `drive_scale`, so
    1.0 is exactly the mode as installed. Each halving is -6 dB of transmit
    audio.

Note what a drive sweep is and is not. Backing off transmit audio lowers
the received signal against a fixed receiver noise floor, so it moves the
operating SNR -- that is the point -- but it is not the calibrated
SNR-per-3-kHz reference `CHANNELS.md` defines, and this bench's reported
`channel_snr_db` is disclaimed by the demodulator's own docstring. Treat
the result as a *relative* ordering of two modes through one path, which is
what it is, and not as a qualification measurement.

    python experiments/hf19_ofdm49_8psk/ab_drive_sweep.py --rounds 10

`experiments/hf10_ofdm49_v6/{ofdm49_v6,hardware_test}.py` are imported
read-only and unmodified, and both arms are built from their mode modules
rather than retyped.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from dataclasses import replace
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
from experiments.hf10_ofdm49_v6.hardware_test import run_direction
from whale.modes.hf7_mode import HF7_PHY
from whale.modes.hf8_mode import HF8_PHY

KEYING_OVERHEAD_S = 0.155   # measured on this bench (hf18), flat across configs

# The two arms are the installed modes themselves.
ARMS = (("hf8", HF8_PHY), ("hf7", HF7_PHY))

# Multipliers on each mode's own drive_scale. 1.0 is the mode as installed;
# every halving is -6 dB of transmit audio. The floor is deliberately far
# enough down that both arms are expected to fail there -- a sweep that
# never breaks the stronger arm cannot separate them.
DEFAULT_LEVELS = (1.0, 0.5, 0.25, 0.125, 0.0625, 0.03125)

DEFAULT_OUTPUT_ROOT = Path("logs") / "mode_qualification" / "hf-ssb" / "hf19"
DECODER_OPTIONS = {"gain_smoothing": 1, "noise_estimator": "repeat"}


def wilson(passed: int, total: int, z: float = 1.959963984540054):
    if total == 0:
        return [0.0, 1.0]
    p = passed / total
    d = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / d
    margin = z / d * (p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--rounds", type=int, default=10,
                    help="trials per arm PER LEVEL; each round runs both arms once")
    ap.add_argument("--level", type=float, action="append", default=None,
                    help="repeatable drive multiplier; default is the halving ladder")
    ap.add_argument("--seed", type=int, default=20260907)
    ap.add_argument("--capture-tail", type=float, default=1.0)
    ap.add_argument("--inter-trial", type=float, default=0.5)
    ap.add_argument("--direction", choices=("ab", "ba"), default="ab")
    ap.add_argument("--allow-ic705-tx", action="store_true")
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)
    if args.direction == "ba" and not args.allow_ic705_tx:
        ap.error("--direction ba requires explicit --allow-ic705-tx")
    if args.allow_ic705_tx and args.direction != "ba":
        ap.error("--allow-ic705-tx is valid only with --direction ba")

    levels = sorted(args.level or DEFAULT_LEVELS, reverse=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{args.label}" if args.label else "-drive-sweep"
    out_dir = args.output_dir or (REPOSITORY_ROOT / DEFAULT_OUTPUT_ROOT / f"{stamp}{suffix}")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.direction == "ab":
        tx_name, rx_name = args.a, args.b
        pair_kwargs = {"b_receive_only": True}
    else:
        tx_name, rx_name = args.b, args.a
        pair_kwargs = {"a_receive_only": True}

    for name, phy in ARMS:
        air = phy.frame_seconds() + KEYING_OVERHEAD_S
        print(f"{name}: bps={phy.bits_per_symbol} fec={phy.fec_rate} "
              f"pilot={phy.pilot_interval} payload={phy.max_payload_bytes} B "
              f"codewords={phy.n_codewords} frame={phy.frame_seconds():.3f}s "
              f"air~{air:.3f}s drive={phy.drive_scale} "
              f"crest={phy.crest_factor_db():.1f}dB "
              f"net_frame={phy.max_payload_bytes * 8 / phy.frame_seconds():.1f} "
              f"net_air~{phy.max_payload_bytes * 8 / air:.1f} bps")
    est = len(levels) * args.rounds * sum(
        p.frame_seconds() + KEYING_OVERHEAD_S + args.capture_tail + args.inter_trial
        for _, p in ARMS)
    print(f"\n{len(levels)} levels x {args.rounds} rounds x {len(ARMS)} arms; "
          f"estimated {est / 60:.0f} min")
    print(f"direction {tx_name} -> {rx_name}; levels {levels}\n")

    records: list[dict] = []
    log = (out_dir / "trials.log").open("w", encoding="utf-8")
    try:
        with bench.radio_pair(args.a, args.b, warmup=3.0, **pair_kwargs) as (t_a, t_b):
            tx, rx = (t_a, t_b) if args.direction == "ab" else (t_b, t_a)
            for level in levels:
                print(f"== drive x{level:g} ({20 * np.log10(level):+.1f} dB) ==",
                      flush=True)
                tally = {name: [0, 0] for name, _ in ARMS}
                for rnd in range(1, args.rounds + 1):
                    order = ARMS if rnd % 2 else tuple(reversed(ARMS))
                    for name, phy in order:
                        # drive_scale is the only thing that changes; the PHY
                        # is frozen, so build the scaled twin by replacement.
                        scaled = replace(phy, drive_scale=phy.drive_scale * level)
                        buf = io.StringIO()
                        with contextlib.redirect_stdout(buf):
                            recs = run_direction(
                                tx, rx, f"{tx_name}->{rx_name}", scaled, 1,
                                args.seed + rnd * 100,
                                capture_tail=args.capture_tail,
                                inter_trial=args.inter_trial,
                                decoder_options=DECODER_OPTIONS)
                        log.write(f"[level {level:g} round {rnd} arm {name}]\n"
                                  f"{buf.getvalue()}")
                        log.flush()
                        r = recs[0]
                        r.update({"arm": name, "level": level, "round": rnd})
                        records.append(r)
                        tally[name][1] += 1
                        if r["outcome"] == "decoded":
                            tally[name][0] += 1
                        snr = r.get("channel_snr_db")
                        raw = r.get("raw_ber")
                        print(f"  x{level:<7g} r{rnd:3d} {name}: {r['outcome']:15s} "
                              f"snr={'n/a' if snr is None else f'{snr:5.1f}dB'} "
                              f"raw_ber={'n/a' if raw is None else f'{raw:.4f}'} "
                              f"peak={r.get('capture_peak') or 0:.2f} "
                              f"running {tally[name][0]}/{tally[name][1]}",
                              flush=True)
                        time.sleep(args.inter_trial)
                for name, _ in ARMS:
                    ok, n = tally[name]
                    print(f"  -> x{level:g} {name}: {ok}/{n}", flush=True)
    finally:
        log.close()

    print("\n== SUMMARY: delivery by drive level ==")
    header = f"{'level':>8} {'dB':>7}"
    for name, _ in ARMS:
        header += f" {name:>14}"
    print(header)
    summary: dict[str, dict] = {}
    for level in levels:
        line = f"{level:8g} {20 * np.log10(level):+7.1f}"
        for name, _ in ARMS:
            rs = [r for r in records if r["arm"] == name and r["level"] == level]
            ok = sum(1 for r in rs if r["outcome"] == "decoded")
            lo, hi = wilson(ok, len(rs))
            summary.setdefault(name, {})[f"{level:g}"] = {
                "delivered": ok, "trials": len(rs),
                "rate": ok / len(rs) if rs else None,
                "wilson": [lo, hi],
                "mean_raw_ber": (float(np.mean([r["raw_ber"] for r in rs
                                                if r["raw_ber"] is not None]))
                                 if any(r["raw_ber"] is not None for r in rs) else None),
                "mean_snr_db": (float(np.mean([r["channel_snr_db"] for r in rs
                                               if r["channel_snr_db"] is not None]))
                                if any(r["channel_snr_db"] is not None for r in rs) else None),
            }
            line += f" {ok:5d}/{len(rs):<3d}{100 * ok / len(rs) if rs else 0:5.0f}%"
        print(line)

    # Breakpoint: the lowest level at or above which every tested level still
    # delivered at the target. Same rule as the simulation's boundary_snr.
    print("\n== breakpoint (lowest drive with >=90% delivery, "
          "'x' = not reached) ==")
    breakpoints = {}
    for name, _ in ARMS:
        best = None
        for level in levels:                       # already descending
            if summary[name][f"{level:g}"]["rate"] is not None and \
               summary[name][f"{level:g}"]["rate"] >= 0.9:
                best = level
            else:
                break
        breakpoints[name] = best
        text = "x" if best is None else f"x{best:g} ({20 * np.log10(best):+.1f} dB)"
        print(f"  {name}: {text}")
    if all(breakpoints[n] is not None for n, _ in ARMS):
        gap = 20 * np.log10(breakpoints["hf7"] / breakpoints["hf8"])
        print(f"  HF8 holds {gap:+.1f} dB of transmit audio below HF7")
        breakpoints["gap_db"] = float(gap)

    doc = {
        "note": "Relative two-mode comparison through one bench path; the "
                "drive multiplier is not a calibrated SNR reference.",
        "direction": f"{tx_name}->{rx_name}",
        "levels": list(levels), "rounds_per_level": args.rounds,
        "seed": args.seed, "decoder_options": DECODER_OPTIONS,
        "arms": {name: {"bits_per_symbol": p.bits_per_symbol,
                        "fec_rate": p.fec_rate,
                        "pilot_interval": p.pilot_interval,
                        "cp_len": p.cp_len, "fft_size": p.fft_size,
                        "packet_bytes": p.packet_bytes,
                        "payload_bytes": p.max_payload_bytes,
                        "n_codewords": p.n_codewords,
                        "frame_seconds": p.frame_seconds(),
                        "drive_scale": p.drive_scale}
                 for name, p in ARMS},
        "summary": summary, "breakpoints": breakpoints,
        "records": records,
    }
    (out_dir / "result.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(f"\nwrote {out_dir / 'result.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
