"""Bench sweep to find the usable audio-tone band edges of the real
STA1(ic705)/STA2(ht) link -- i.e. how low can the mark tone go and how high
can the space tone go before the radios' mic/speaker audio chains (filters,
de-emphasis, etc.) stop passing a CPFSK frame reliably.

Mirrors scripts/sweep_baud_payload.py's approach: bypasses whale.link's ARQ
entirely (single direct modulate -> TX -> capture -> demodulate per trial),
and every candidate is tested in BOTH directions (ic705->ht and ht->ic705),
taking whichever direction is weaker as the result for that point -- so the
reported edges are the ones that hold up both ways, not just one leg's best
case. See scripts/bench.py.

Two independent walks, each holding the other tone fixed at PROFILE_600's
value (700/1500 Hz) so only one edge moves at a time:
  1. LOW EDGE:  walk freq0 downward from 700 Hz (freq1 fixed at 1500 Hz)
  2. HIGH EDGE: walk freq1 upward from 1500 Hz (freq0 fixed at 700 Hz)
Each walk stops at the first candidate that fails the success bar in either
direction; the edge is the last candidate that still cleared it.

Does not modify any existing module -- only imports whale.afsk/transport as
already shipped.

Run: python scripts/measure_band_edges.py
     python scripts/measure_band_edges.py --trials 10
"""
import argparse
import sys

import bench
from whale import afsk

TONE_SEPARATION_MIN = 200.0  # Hz; refuse candidates that push freq0/freq1 too close together

PAYLOAD = bench.ACK_SHAPED_PAYLOAD  # worst-case-small frame (same as sweep_baud_payload.py)
BAUD = afsk.PROFILE_600.baud

# candidates walk outward from PROFILE_600's own tones (700/1500 Hz)
LOW_CANDIDATES = [600, 500, 400, 300, 250, 200]
HIGH_CANDIDATES = [2200, 2300, 2400]


def make_profile(freq0, freq1, name):
    return afsk.Profile(name=name, mode_id=99, baud=BAUD, freq0=float(freq0), freq1=float(freq1))


def run_candidate(t_a, t_b, freq0, freq1, trials):
    profile = make_profile(freq0, freq1, name=f"edge_{freq0:.0f}_{freq1:.0f}")
    print(f"\n-- freq0={freq0:.0f}Hz freq1={freq1:.0f}Hz (baud={BAUD}) --")
    return bench.run_both_directions(t_a, t_b, profile, PAYLOAD, trials=trials)


def _separated(candidates, fixed, describe):
    """Drops candidates that would sit closer than TONE_SEPARATION_MIN to
    the tone being held fixed. Filtered out rather than failed: too-close
    tones say nothing about the radio's passband, so hitting one must not
    end the walk the way a genuine decode failure does."""
    kept = []
    for freq in candidates:
        if abs(fixed - freq) < TONE_SEPARATION_MIN:
            print(f"  skipping {describe}={freq} -- too close to the fixed tone "
                  f"(< {TONE_SEPARATION_MIN:.0f} Hz separation)")
            continue
        kept.append(freq)
    return kept


def walk_low_edge(t_a, t_b, trials):
    fixed = afsk.PROFILE_600.freq1
    print(f"\n=== LOW EDGE WALK (freq1 fixed at {fixed:.0f} Hz) ===")
    last_good = bench.walk(
        _separated(LOW_CANDIDATES, fixed, "freq0"),
        lambda freq0: run_candidate(t_a, t_b, freq0, fixed, trials),
        lambda freq0: f"freq0={freq0}")
    print(f"\nLOW EDGE RESULT: lowest reliable freq0 (both directions) = {last_good}")
    return last_good


def walk_high_edge(t_a, t_b, trials):
    fixed = afsk.PROFILE_600.freq0
    print(f"\n=== HIGH EDGE WALK (freq0 fixed at {fixed:.0f} Hz) ===")
    last_good = bench.walk(
        _separated(HIGH_CANDIDATES, fixed, "freq1"),
        lambda freq1: run_candidate(t_a, t_b, fixed, freq1, trials),
        lambda freq1: f"freq1={freq1}")
    print(f"\nHIGH EDGE RESULT: highest reliable freq1 (both directions) = {last_good}")
    return last_good


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=bench.TRIALS)
    ap.add_argument("--skip-low", action="store_true", help="skip the low-edge walk")
    ap.add_argument("--skip-high", action="store_true", help="skip the high-edge walk")
    ap.add_argument("--probe", nargs=2, type=float, metavar=("FREQ0", "FREQ1"),
                     help="skip both walks; just run --trials at this freq0/freq1 pair, both directions")
    args = ap.parse_args()

    with bench.radio_pair() as (t_ic705, t_ht):
        if args.probe is not None:
            freq0, freq1 = args.probe
            run_candidate(t_ic705, t_ht, freq0, freq1, args.trials)
            return 0

        low_edge = None if args.skip_low else walk_low_edge(t_ic705, t_ht, args.trials)
        high_edge = None if args.skip_high else walk_high_edge(t_ic705, t_ht, args.trials)

        print("\n\n===== FINAL SUMMARY =====")
        print(f"Usable band (reliable >= {bench.SUCCESS_THRESHOLD * 100:.0f}% both directions, "
              f"baud={BAUD}): {low_edge if low_edge is not None else '?'} Hz to "
              f"{high_edge if high_edge is not None else '?'} Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
