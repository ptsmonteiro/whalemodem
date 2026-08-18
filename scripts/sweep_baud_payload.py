"""Bench sweep to find, empirically over the real STA1(ic705)/STA2(ht) link:

  1. the highest baud (at PROFILE_600's tones, 700/1500 Hz) that still
     decodes reliably, and
  2. at that baud, the largest payload that still decodes reliably,

tested in both directions (ic705->ht and ht->ic705, since the two legs have
different measured SNR -- see whale/afsk.py docstring), taking whichever
direction is weaker as the limit.

Bypasses whale.link's ARQ entirely (like scripts/probe_600_ack.py) so each
data point is a single direct modulate -> TX -> capture -> demodulate, no
retries/acks muddying the result. See scripts/bench.py.

Trials here are *unpadded*, unlike sweep_baud_600_2300.py and
sweep_payload_1200_2200.py: the frame starts at t=0 of the TX buffer, so
these numbers include whatever the audio chain's post-PTT settling costs.
That is the configuration production actually runs in.

Run: python scripts/sweep_baud_payload.py
"""
import argparse
import sys

import bench
from whale import afsk

# baud candidates to probe, holding PROFILE_600's tones fixed
BAUD_CANDIDATES = [300, 450, 600, 900, 1200, 1600, 2000, 2400, 3000]
PAYLOAD_CANDIDATES = [2, 4, 6, 8, 10, 20, 40, 80, 120, 160, 200, 255]


def make_profile(baud, confidence_threshold=afsk.CONFIDENCE_THRESHOLD):
    # PROFILE_300's own tones (700/1300) for the 300baud sanity baseline --
    # exactly the shipped, proven-good profile. Every higher candidate uses
    # PROFILE_600's tones (700/1500), the flatter-passband pair already
    # validated for anything faster than 300 baud (see whale/afsk.py).
    freq1 = afsk.PROFILE_300.freq1 if baud <= 300 else afsk.PROFILE_600.freq1
    return afsk.Profile(name=f"{baud}baud", mode_id=99, baud=baud,
                         freq0=afsk.PROFILE_600.freq0, freq1=freq1,
                         confidence_threshold=confidence_threshold)


def sweep_baud(t_a, t_b, small_payload, trials):
    print(f"\n=== BAUD SWEEP (payload={len(small_payload)} bytes fixed) ===")

    def measure(baud):
        profile = make_profile(baud)
        airtime = afsk.frame_seconds(len(small_payload), profile)
        print(f"\n-- baud={baud} (frame airtime ~{airtime:.2f}s) --")
        return bench.run_both_directions(t_a, t_b, profile, small_payload, trials=trials)

    best = bench.walk(BAUD_CANDIDATES, measure, lambda baud: f"baud={baud}")
    print(f"\nBAUD SWEEP RESULT: highest reliable baud = {best}")
    return best


def sweep_payload(t_a, t_b, baud, trials):
    print(f"\n=== PAYLOAD SWEEP (baud={baud} fixed) ===")
    profile = make_profile(baud)

    def measure(size):
        payload = bench.counting_payload(size)
        airtime = afsk.frame_seconds(size, profile)
        print(f"\n-- payload={size} bytes (frame airtime ~{airtime:.2f}s) --")
        return bench.run_both_directions(t_a, t_b, profile, payload, trials=trials)

    best = bench.walk(PAYLOAD_CANDIDATES, measure, lambda size: f"payload={size}")
    print(f"\nPAYLOAD SWEEP RESULT: largest reliable payload at baud={baud} = {best}")
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-baud", action="store_true", help="skip baud sweep, use --baud directly")
    ap.add_argument("--baud", type=int, default=None, help="baud to use for payload sweep if --skip-baud")
    ap.add_argument("--trials", type=int, default=bench.TRIALS)
    ap.add_argument("--probe-baud", type=int, default=None,
                     help="skip both sweeps; just run --trials at this baud/--probe-payload both directions")
    ap.add_argument("--probe-payload", type=int, default=2)
    args = ap.parse_args()

    small_payload = bench.ACK_SHAPED_PAYLOAD  # worst-case-small frame

    with bench.radio_pair() as (t_ic705, t_ht):
        if args.probe_baud is not None:
            profile = make_profile(args.probe_baud)
            payload = bench.counting_payload(args.probe_payload)
            print(f"\n=== PROBE baud={args.probe_baud} payload={len(payload)} bytes ===")
            bench.run_both_directions(t_ic705, t_ht, profile, payload, trials=args.trials)
            return 0

        if args.skip_baud:
            best_baud = args.baud
            assert best_baud is not None, "--baud required with --skip-baud"
        else:
            best_baud = sweep_baud(t_ic705, t_ht, small_payload, args.trials)
            if best_baud is None:
                print("No baud even at the slowest candidate cleared the bar -- aborting.")
                return 1

        best_payload = sweep_payload(t_ic705, t_ht, best_baud, args.trials)

        print("\n\n===== FINAL SUMMARY =====")
        print(f"Max reliable baud (>= {bench.SUCCESS_THRESHOLD * 100:.0f}% both directions): {best_baud}")
        print(f"Max reliable payload at that baud: {best_payload} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
