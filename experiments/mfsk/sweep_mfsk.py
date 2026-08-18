"""Bench sweep for MFSK candidates over the real two-radio link.

Answers one question -- what is the highest-throughput MFSK profile that
decodes 100% in *both* directions inside a 3s keying -- by walking candidates
in descending throughput order and stopping at the first that does. "Max
throughput subject to 100%" then falls out of the ordering rather than out of
anyone's judgement about which candidate ought to win.

Follows the pattern scripts/measure_band_edges.py and
scripts/sweep_baud_payload.py established, and for the same reasons:

  - ARQ is bypassed entirely. One modulate -> TX -> capture -> demodulate per
    trial, no whale.link, no retransmits, so a failure is a failure of the
    mode and not of a timeout constant.
  - Every candidate is tested both directions and the *worse* one is its
    score. The ht -> ic705 leg is consistently the weaker of the two and is
    the leg every previous tone-placement change died on; a mode that only
    works one way is not a mode.
  - Nothing in whale/ is modified. This imports whale.transport to reach the
    radios and experiments/mfsk/mfsk.py for the modem, and writes nothing
    back.

Two things it does differently, both deliberate:

  - Trials run at the candidate's *full* keying-budget payload, not at the
    2-byte probe frame the band-edge sweep used. A short frame is the easiest
    thing on the channel; the mode being evaluated here is defined by filling
    a 3s keying, and under CRC a frame is all-or-nothing, so the only error
    rate that matters is the one over the whole ~3000 bits.
  - Payloads are random and different every trial, and checked byte-for-byte.
    A fixed payload can pass on a decoder bug that reconstructs what it
    expects; random ones cannot.

The keyed duration of every transmission is recorded and checked against
mfsk.MAX_KEYING_SECONDS. That is the constraint the whole exercise is under,
and it is measured here rather than trusted from the arithmetic -- the same
way whale/transport.py's overhead figure was found to be 0.43 and not the 0.40
it had been reasoned at.

Run: python experiments/mfsk/sweep_mfsk.py                 # ladder, then confirm
     python experiments/mfsk/sweep_mfsk.py --trials 5
     python experiments/mfsk/sweep_mfsk.py --only 4fsk_650bd_x0.833
     python experiments/mfsk/sweep_mfsk.py --confirm 4fsk_575bd_x1 --confirm-trials 20
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

import mfsk
from whale import afsk
from whale.transport import RadioTransport

# Seconds to keep capturing after send() returns. send() already waits for
# playout and PTT release, so this only has to cover the receiver's squelch
# tail and the decoder's need for a little audio past the last symbol.
CAPTURE_TAIL = 0.8

# Between trials, so squelch/AGC at both ends settle and one trial's tail
# cannot land inside the next one's capture.
INTER_TRIAL_GAP = 0.6

# Slack on the keying-budget check before a transmission is called over
# budget. The budget is built on mfsk.KEYING_OVERHEAD_SECONDS, which is a
# measured worst case quoted to 0.01s (transport.py: "0.42-0.43s over 88
# keyings"), and the PTT/stream timings behind it vary by a few ms keying to
# keying. Flagging a 3.001s transmission against a 3.0s cap derived from a
# figure known only to +/-0.01s is noise, not a finding; 30ms is comfortably
# inside the constant's own uncertainty and still catches a real overrun,
# which would be a whole symbol or more.
KEYING_BUDGET_SLACK = 0.03

REFERENCE_BITRATE = afsk.PROFILE_1200.chunk_size * 8 / mfsk.MAX_KEYING_SECONDS


def run_direction(tx, rx, profile, trials, rng, label):
    """`trials` frames one way. Returns (decode_rate, stats dict)."""
    ok = 0
    confidences, keyed = [], []
    payload_len = profile.max_payload
    for i in range(1, trials + 1):
        # snapshot_rx() does not consume, so flush explicitly or captures
        # accumulate across the whole run and every decode gets slower.
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        payload = rng.integers(0, 256, payload_len, dtype=np.uint8).tobytes()
        keyed_seconds = tx.send(mfsk.modulate(payload, profile))
        time.sleep(CAPTURE_TAIL)

        result = mfsk.demodulate(rx.snapshot_rx(), profile)
        good = result.get("payload") == payload
        conf = float(result.get("confidence", 0.0))
        ok += int(good)
        confidences.append(conf)
        keyed.append(keyed_seconds)

        over = (" OVER BUDGET" if keyed_seconds > mfsk.MAX_KEYING_SECONDS + KEYING_BUDGET_SLACK
                else "")
        print(f"  [{label}] {i}/{trials}: {payload_len}B keyed={keyed_seconds:.2f}s{over} "
              f"conf={conf:.3f} decoded={good}")
        time.sleep(INTER_TRIAL_GAP)

    stats = {
        "ok": ok, "trials": trials, "rate": ok / trials,
        "mean_confidence": float(np.mean(confidences)) if confidences else 0.0,
        "min_confidence": float(np.min(confidences)) if confidences else 0.0,
        "max_keyed_seconds": float(np.max(keyed)) if keyed else 0.0,
    }
    print(f"  [{label}] => {ok}/{trials} ({stats['rate']*100:.0f}%), "
          f"conf min={stats['min_confidence']:.3f} mean={stats['mean_confidence']:.3f}, "
          f"max keyed={stats['max_keyed_seconds']:.2f}s")
    return stats["rate"], stats


def run_candidate(t_a, t_b, profile, trials, rng, name_a="ic705", name_b="ht"):
    print(f"\n-- {mfsk.describe(profile)}")
    ab_rate, ab = run_direction(t_a, t_b, profile, trials, rng, f"{name_a}->{name_b}")
    ba_rate, ba = run_direction(t_b, t_a, profile, trials, rng, f"{name_b}->{name_a}")
    worst = min(ab_rate, ba_rate)
    print(f"  worst direction: {worst*100:.0f}%")
    return worst, {"profile": profile.name, "m": profile.m,
                   "symbol_rate": profile.symbol_rate,
                   "spacing": profile.spacing,
                   "spacing_ratio": profile.spacing_ratio,
                   "tones": [float(f) for f in profile.tones],
                   "payload_bytes": profile.max_payload,
                   "payload_bitrate": profile.payload_bitrate,
                   "worst_rate": worst,
                   f"{name_a}->{name_b}": ab, f"{name_b}->{name_a}": ba}


def build_pool(args):
    pool = mfsk.candidates(train_on_preamble=not args.no_training)
    by_name = {p.name: p for p in pool}
    if args.only:
        missing = [n for n in args.only if n not in by_name]
        if missing:
            raise SystemExit(
                f"unknown candidate(s): {missing}\nlist them with:\n"
                f"  python -c \"import sys; sys.path.insert(0, 'experiments/mfsk'); "
                f"import mfsk; [print(mfsk.describe(p)) for p in mfsk.candidates()]\"")
        return [by_name[n] for n in args.only]
    pool = [p for p in pool if p.payload_bitrate > REFERENCE_BITRATE * args.min_gain]
    return pool[:args.top]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=5,
                    help="trials per direction while walking the ladder")
    ap.add_argument("--confirm-trials", type=int, default=20,
                    help="trials per direction for the winner")
    ap.add_argument("--top", type=int, default=12, help="candidates to walk at most")
    ap.add_argument("--only", nargs="+", help="test exactly these candidates, in this order")
    ap.add_argument("--confirm", help="skip the ladder; confirm this candidate directly")
    ap.add_argument("--min-gain", type=float, default=1.0,
                    help="skip candidates not at least this multiple of the shipped "
                         "1200-baud profile's throughput")
    ap.add_argument("--no-training", action="store_true",
                    help="disable the preamble-trained per-tone equaliser")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default=None, help="write results as JSON here")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    if args.confirm:
        pool = [p for p in mfsk.candidates(train_on_preamble=not args.no_training)
                if p.name == args.confirm]
        if not pool:
            raise SystemExit(f"unknown candidate {args.confirm!r}")
    else:
        pool = build_pool(args)

    print(f"reference: {afsk.PROFILE_1200.name} delivers {REFERENCE_BITRATE:.0f} payload bits/s")
    print(f"walking {len(pool)} candidate(s), best throughput first, "
          f"{args.trials} trials per direction, stopping at the first 100%\n")
    for p in pool:
        print("   ", mfsk.describe(p))

    print("\nopening radios...")
    t_a = RadioTransport("ic705")
    t_b = RadioTransport("ht")
    results, winner = [], None
    try:
        t_a.start_receiving()
        t_b.start_receiving()
        print("warming up 2s...")
        time.sleep(2)

        if args.confirm:
            worst, record = run_candidate(t_a, t_b, pool[0], args.confirm_trials, rng)
            results.append(record)
            winner = pool[0] if worst >= 1.0 else None
        else:
            for profile in pool:
                worst, record = run_candidate(t_a, t_b, profile, args.trials, rng)
                results.append(record)
                if worst >= 1.0:
                    winner = profile
                    break

            if winner is not None:
                print(f"\n=== CONFIRMING {winner.name}: {args.confirm_trials} trials "
                      f"per direction at {winner.max_payload}B ===")
                worst, record = run_candidate(t_a, t_b, winner, args.confirm_trials, rng)
                record["confirmation"] = True
                results.append(record)
                if worst < 1.0:
                    print(f"\n{winner.name} did NOT hold up over "
                          f"{args.confirm_trials} trials -- not a result.")
                    winner = None
    finally:
        t_a.close()
        t_b.close()

    print("\n\n===== SUMMARY =====")
    for r in results:
        tag = " (confirmation)" if r.get("confirmation") else ""
        print(f"  {r['profile']:<22} {r['payload_bitrate']:>6.1f} bits/s "
              f"worst={r['worst_rate']*100:>5.1f}%{tag}")
    if winner is not None:
        print(f"\nWINNER: {winner.name}")
        print(f"  {mfsk.describe(winner)}")
        print(f"  {winner.payload_bitrate:.1f} payload bits/s "
              f"= {winner.payload_bitrate / REFERENCE_BITRATE:.2f}x the shipped "
              f"1200-baud profile ({REFERENCE_BITRATE:.0f} bits/s)")
    else:
        print("\nNo candidate decoded 100% both directions. The ladder is ordered by "
              "throughput, so this means every candidate above the shipped profile's "
              "rate failed -- widen --top, or lower --min-gain to test at/below parity.")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "when": datetime.now(timezone.utc).isoformat(),
            "reference_bitrate": REFERENCE_BITRATE,
            "trials": args.trials, "confirm_trials": args.confirm_trials,
            "training": not args.no_training,
            "winner": winner.name if winner else None,
            "results": results,
        }, indent=2))
        print(f"\nwrote {args.out}")
    return 0 if winner is not None else 1


if __name__ == "__main__":
    sys.exit(main())
