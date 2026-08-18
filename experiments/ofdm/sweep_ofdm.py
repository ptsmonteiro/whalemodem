"""Bench sweep for OFDM candidates over the real two-radio link.

Answers one question -- what is the highest-throughput OFDM profile that
decodes 100% on the known weak HT -> IC-705 path inside a 3s keying -- by
walking candidates in descending throughput order and stopping at the first
that does. Only after that candidate survives confirmation is the stronger
IC-705 -> HT path tested once to establish that the result works both ways.

Follows the pattern scripts/bench.py and experiments/mfsk/sweep_mfsk.py
established, and for the same reasons:

  - ARQ is bypassed entirely. One modulate -> TX -> capture -> demodulate per
    trial, no whale.link, no retransmits, so a failure is a failure of the
    mode and not of a timeout constant.
  - Screening and drive selection use only HT -> IC-705. Repeating every
    failed rung on a path already known to be stronger spends airtime without
    changing the ordering. The stronger path is still mandatory, but is run
    only once at the end for the confirmed weak-path winner.
  - Trials run at the candidate's full keying-budget payload, with random
    payloads checked byte-for-byte. A fixed payload can pass on a decoder that
    reconstructs what it expects.
  - Nothing in whale/ is modified.

Two things it does that the MFSK sweep did not need, both forced by OFDM
rather than chosen:

  - **--drive.** OFDM is the first non-constant-envelope mode in this repo, so
    how hard to drive the radio is a real parameter rather than a fixed 0.6.
    The peak amplitude and the clipping ratio trade against each other (see
    ofdm._clip_and_filter), the modelled optimum is flat between 8 and 10 dB,
    and the model does not include the radio's own limiter -- which is the one
    thing that could make the whole answer different. So it is A/B'd on air
    before the ladder, at the top candidate, and the winner is used for
    everything after.

  - **--band.** probe_channel.py measures the usable band far more directly
    than the 2-byte FSK frame scripts/measure_band_edges.py used, and with no
    FEC a single bad subcarrier fails every frame. Run the probe first and
    pass what it recommends.

Run: python experiments/ofdm/probe_channel.py --out probe.json   # first
     python experiments/ofdm/sweep_ofdm.py --top 5 --trials 4
     python experiments/ofdm/sweep_ofdm.py --band 600 2300 --drive
     python experiments/ofdm/sweep_ofdm.py --band 650 1950 --bits 2 \
         --pilot-intervals 4 8 16 --only ofdm4_50hz_cp8
     python experiments/ofdm/sweep_ofdm.py --confirm ofdm4_50hz_cp8 --confirm-trials 10
"""

import argparse
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

import ofdm
from whale import afsk
from whale.transport import RadioTransport

# Seconds to keep capturing after send() returns. send() already waits for
# playout and PTT release, so this only has to cover the receiver's squelch
# tail and the decoder's need for a little audio past the last symbol.
CAPTURE_TAIL = 0.8

# Between trials, so squelch/AGC at both ends settle and one trial's tail
# cannot land inside the next one's capture.
INTER_TRIAL_GAP = 0.6

# Slack before a transmission is called over budget. Same reasoning as
# sweep_mfsk.py: the budget rests on a measured overhead quoted to 0.01s and
# the PTT/stream timings vary by a few ms keying to keying, so flagging a
# 3.001s transmission against a 3.0s cap is noise. 30ms still catches a real
# overrun, which would be a whole OFDM symbol or more.
KEYING_BUDGET_SLACK = 0.03

# Drive settings A/B'd by --drive: (peak amplitude, PAPR target in dB).
# Spread across the flat part of the modelled optimum plus one point either
# side of it, because what the model cannot see -- the radio's own limiter --
# is exactly what would move the answer off that plateau.
DRIVE_SETTINGS = ((0.9, 9.0), (0.9, 7.0), (0.9, 12.0), (0.6, 9.0))

REFERENCE_BITRATE = afsk.PROFILE_1200.chunk_size * 8 / ofdm.MAX_KEYING_SECONDS


def run_direction(tx, rx, profile, trials, rng, label, amplitude):
    """`trials` frames one way. Returns (decode_rate, stats dict)."""
    ok = 0
    confidences, keyed_times = [], []
    payload_len = profile.max_payload
    for i in range(1, trials + 1):
        # snapshot_rx() does not consume, so flush explicitly or captures
        # accumulate across the whole run and every decode gets slower.
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        payload = rng.integers(0, 256, payload_len, dtype=np.uint8).tobytes()
        audio = ofdm.modulate(payload, profile, amplitude=amplitude)
        keyed = tx.send(audio)
        time.sleep(CAPTURE_TAIL)

        result = ofdm.demodulate(rx.snapshot_rx(), profile)
        good = result.get("payload") == payload
        confidence = float(result.get("confidence", 0.0))
        ok += int(good)
        confidences.append(confidence)
        keyed_times.append(keyed)

        over = (" OVER BUDGET" if keyed > ofdm.MAX_KEYING_SECONDS + KEYING_BUDGET_SLACK
                else "")
        print(f"  [{label}] {i}/{trials}: {payload_len}B keyed={keyed:.2f}s{over} "
              f"conf={confidence:.3f} decoded={good}")
        time.sleep(INTER_TRIAL_GAP)

    stats = {
        "ok": ok, "trials": trials, "rate": ok / trials,
        "mean_confidence": float(np.mean(confidences)) if confidences else 0.0,
        "min_confidence": float(np.min(confidences)) if confidences else 0.0,
        "max_keyed_seconds": float(np.max(keyed_times)) if keyed_times else 0.0,
    }
    print(f"  [{label}] => {ok}/{trials} ({stats['rate'] * 100:.0f}%), "
          f"conf min={stats['min_confidence']:.3f} mean={stats['mean_confidence']:.3f}, "
          f"max keyed={stats['max_keyed_seconds']:.2f}s")
    return stats["rate"], stats


def profile_record(profile, amplitude):
    return {"profile": profile.name, "n_fft": profile.n_fft, "cp": profile.cp,
            "bits_per_carrier": profile.bits_per_carrier,
            "spacing_hz": profile.spacing,
            "cp_ms": profile.cp_seconds * 1000,
            "pilot_interval": profile.pilot_interval,
            "band": [profile.tone_low, profile.tone_high],
            "n_carriers": profile.n_carriers,
            "amplitude": amplitude, "papr_db": profile.papr_db,
            "payload_bytes": profile.max_payload,
            "payload_bitrate": profile.payload_bitrate}


def run_weak_candidate(t_a, t_b, profile, trials, rng, amplitude):
    print(f"\n-- {ofdm.describe(profile)}")
    rate, stats = run_direction(t_b, t_a, profile, trials, rng, "ht->ic705", amplitude)
    record = profile_record(profile, amplitude)
    record.update({"weak_rate": rate, "ht->ic705": stats})
    return rate, record


def validate_strong_path(t_a, t_b, profile, trials, rng, amplitude):
    print(f"\n=== FINAL STRONG-PATH VALIDATION: {trials} trials IC-705 -> HT ===")
    rate, stats = run_direction(t_a, t_b, profile, trials, rng,
                                "ic705->ht", amplitude)
    return rate, stats


def choose_drive(t_a, t_b, profile, trials, rng):
    """A/B the drive settings at one candidate and return the best.

    Scored on decode rate first and sync confidence second, because at a
    candidate that decodes 100% either way the confidence is the only thing
    left that distinguishes them -- and it is a direct read on how much margin
    is in hand.
    """
    print("\n===== DRIVE A/B =====")
    print("The one parameter with no bench evidence anywhere in this repo: "
          "every mode before this was constant-envelope.")
    best, results = None, []
    for amplitude, papr in DRIVE_SETTINGS:
        candidate = replace(profile, papr_db=papr)
        print(f"\n-- drive: peak={amplitude} papr={papr} dB "
              f"(RMS ~{amplitude / 10 ** (papr / 20):.2f}, "
              f"afsk runs {0.6 / np.sqrt(2):.2f})")
        rate, record = run_weak_candidate(t_a, t_b, candidate, trials, rng, amplitude)
        confidence = record["ht->ic705"]["mean_confidence"]
        record["drive"] = [amplitude, papr]
        results.append(record)
        score = (rate, confidence)
        if best is None or score > best[0]:
            best = (score, amplitude, papr)
    print(f"\n  drive chosen: peak={best[1]} papr={best[2]} dB "
          f"(rate {best[0][0] * 100:.0f}%, confidence {best[0][1]:.3f})")
    return best[1], best[2], results


def build_pool(args):
    band = tuple(args.band) if args.band else (ofdm.BAND_LOW_HZ, ofdm.BAND_HIGH_HZ)
    bits = tuple(args.bits) if args.bits else (1, 2, 3, 4)
    pool = ofdm.candidates(bits=bits, band=band)
    by_name = {p.name: p for p in pool}
    if args.only:
        missing = [n for n in args.only if n not in by_name]
        if missing:
            raise SystemExit(f"unknown candidate(s): {missing}\nknown: {sorted(by_name)}")
        pool = [by_name[n] for n in args.only]
    if args.max_cp_ms:
        pool = [p for p in pool if p.cp_seconds * 1000 <= args.max_cp_ms]
    if args.min_cp_ms:
        pool = [p for p in pool if p.cp_seconds * 1000 >= args.min_cp_ms]
    intervals = args.pilot_intervals or [0]
    pool = [replace(p,
                    name=(p.name if interval == 0 else f"{p.name}_p{interval}"),
                    pilot_interval=interval)
            for p in pool for interval in intervals]
    pool = [p for p in pool if p.payload_bitrate > REFERENCE_BITRATE * args.min_gain]
    pool.sort(key=lambda p: -p.payload_bitrate)
    return pool[:args.top]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=4,
                    help="trials per direction while walking the ladder")
    ap.add_argument("--confirm-trials", type=int, default=10,
                    help="weak-path confirmation trials and final strong-path trials")
    ap.add_argument("--top", type=int, default=5, help="candidates to walk at most")
    ap.add_argument("--only", nargs="+", help="test exactly these candidates, in this order")
    ap.add_argument("--confirm", help="skip the ladder; confirm this candidate directly")
    ap.add_argument("--band", nargs=2, type=float, metavar=("LOW", "HIGH"),
                    help="subcarrier band in Hz, e.g. from probe_channel.py")
    ap.add_argument("--bits", nargs="+", type=int,
                    help="constellations to consider (1=BPSK 2=QPSK 3=8PSK 4=16QAM)")
    ap.add_argument("--min-cp-ms", type=float, default=None,
                    help="skip candidates whose prefix is shorter than this")
    ap.add_argument("--max-cp-ms", type=float, default=None)
    ap.add_argument("--min-gain", type=float, default=1.0,
                    help="skip candidates not at least this multiple of the shipped "
                         "1200-baud profile's throughput")
    ap.add_argument("--pilot-intervals", nargs="+", type=int, metavar="N",
                    help="insert a full-band tracking/timing pilot after every N data "
                         "symbols; multiple values compare pilot densities")
    ap.add_argument("--drive", action="store_true",
                    help="A/B drive settings at the top candidate before the ladder")
    ap.add_argument("--amplitude", type=float, default=0.9, help="peak amplitude")
    ap.add_argument("--papr", type=float, default=9.0, help="PAPR target in dB")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default=None, help="write results as JSON here")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    band = tuple(args.band) if args.band else (ofdm.BAND_LOW_HZ, ofdm.BAND_HIGH_HZ)
    if args.confirm:
        pool = build_pool(args)
        pool = [p for p in pool if p.name == args.confirm or p.name.split("_p")[0] == args.confirm]
        if not pool:
            raise SystemExit(f"unknown candidate {args.confirm!r}")
    else:
        pool = build_pool(args)
    if not pool:
        raise SystemExit("no candidates match those constraints")

    amplitude, papr = args.amplitude, args.papr
    pool = [replace(p, papr_db=papr) for p in pool]

    print(f"reference: {afsk.PROFILE_1200.name} delivers "
          f"{REFERENCE_BITRATE:.0f} payload bits/s")
    print(f"band {band[0]:.0f}-{band[1]:.0f} Hz, drive peak={amplitude} papr={papr} dB")
    print(f"walking {len(pool)} candidate(s), best throughput first, "
          f"{args.trials} HT->IC-705 trials, stopping at the first 100%\n")
    for p in pool:
        print("   ", ofdm.describe(p))

    print("\nopening radios...")
    t_a = RadioTransport("ic705")
    t_b = RadioTransport("ht")
    results, winner = [], None
    try:
        t_a.start_receiving()
        t_b.start_receiving()
        print("warming up 2s...")
        time.sleep(2)

        if args.drive:
            amplitude, papr, drive_records = choose_drive(t_a, t_b, pool[0],
                                                          args.trials, rng)
            results.extend(drive_records)
            pool = [replace(p, papr_db=papr) for p in pool]

        if args.confirm:
            weak, record = run_weak_candidate(t_a, t_b, pool[0], args.confirm_trials,
                                              rng, amplitude)
            record["confirmation"] = True
            results.append(record)
            winner = pool[0] if weak >= 1.0 else None
        else:
            for profile in pool:
                weak, record = run_weak_candidate(t_a, t_b, profile, args.trials,
                                                  rng, amplitude)
                results.append(record)
                if weak >= 1.0:
                    winner = profile
                    break

            if winner is not None:
                print(f"\n=== CONFIRMING {winner.name}: {args.confirm_trials} trials "
                      f"on HT->IC-705 at {winner.max_payload}B ===")
                weak, record = run_weak_candidate(t_a, t_b, winner,
                                                  args.confirm_trials, rng, amplitude)
                record["confirmation"] = True
                results.append(record)
                if weak < 1.0:
                    print(f"\n{winner.name} did NOT hold up over "
                          f"{args.confirm_trials} trials -- not a result.")
                    winner = None
        if winner is not None:
            strong, stats = validate_strong_path(t_a, t_b, winner,
                                                 args.confirm_trials, rng, amplitude)
            validation = profile_record(winner, amplitude)
            validation.update({"strong_validation": True, "strong_rate": strong,
                               "ic705->ht": stats})
            results.append(validation)
            if strong < 1.0:
                print(f"\n{winner.name} passed the weak path but failed final "
                      "IC-705->HT validation -- not a bidirectional result.")
                winner = None
    finally:
        t_a.close()
        t_b.close()

    print("\n\n===== SUMMARY =====")
    for r in results:
        tag = " (confirmation)" if r.get("confirmation") else ""
        tag += f" drive={r['drive']}" if r.get("drive") else ""
        if r.get("strong_validation"):
            tag = " (final strong-path validation)"
            rate = r["strong_rate"]
        else:
            rate = r["weak_rate"]
        print(f"  {r['profile']:<24} {r['payload_bitrate']:>6.1f} bits/s "
              f"rate={rate * 100:>5.1f}%{tag}")
    if winner is not None:
        print(f"\nWINNER: {winner.name}")
        print(f"  {ofdm.describe(winner)}")
        print(f"  {winner.payload_bitrate:.1f} payload bits/s "
              f"= {winner.payload_bitrate / REFERENCE_BITRATE:.2f}x the shipped "
              f"1200-baud profile ({REFERENCE_BITRATE:.0f} bits/s)")
    else:
        print("\nNo candidate completed weak-path confirmation and final strong-path "
              "validation. Widen --top, lower --min-gain, or change pilot intervals.")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "when": datetime.now(timezone.utc).isoformat(),
            "reference_bitrate": REFERENCE_BITRATE,
            "band": list(band), "amplitude": amplitude, "papr_db": papr,
            "trials": args.trials, "confirm_trials": args.confirm_trials,
            "winner": winner.name if winner else None,
            "results": results,
        }, indent=2))
        print(f"\nwrote {args.out}")
    return 0 if winner is not None else 1


if __name__ == "__main__":
    sys.exit(main())
