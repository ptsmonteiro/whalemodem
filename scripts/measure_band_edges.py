"""Bench sweep to find the usable audio-tone band edges of the real
STA1(ic705)/STA2(ht) link -- i.e. how low can the mark tone go and how high
can the space tone go before the radios' mic/speaker audio chains (filters,
de-emphasis, etc.) stop passing a CPFSK frame reliably.

Mirrors scripts/sweep_baud_payload.py's approach: bypasses whale.link's ARQ
entirely (single direct modulate -> TX -> capture -> demodulate per trial),
and every candidate is tested in BOTH directions (ic705->ht and ht->ic705),
taking whichever direction is weaker as the result for that point -- so the
reported edges are the ones that hold up both ways, not just one leg's best
case.

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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale import afsk
from whale.transport import RadioTransport, SAMPLE_RATE

CAPTURE_TAIL = 1.0
TRIALS = 5
SUCCESS_THRESHOLD = 0.8  # fraction of trials that must decode cleanly to call a point "good"
TONE_SEPARATION_MIN = 200.0  # Hz; refuse candidates that push freq0/freq1 too close together

PAYLOAD = bytes([0x06, 0x00])  # DATA_ACK-shaped, worst-case-small frame (same as sweep_baud_payload.py)
BAUD = afsk.PROFILE_600.baud

# candidates walk outward from PROFILE_600's own tones (700/1500 Hz)
LOW_CANDIDATES = [600, 500, 400, 300, 250, 200]
HIGH_CANDIDATES = [2200, 2300, 2400]


def make_profile(freq0, freq1, name):
    return afsk.Profile(name=name, mode_id=99, baud=BAUD, freq0=float(freq0), freq1=float(freq1))


def run_point(tx, rx, profile, label):
    ok = 0
    confidences = []
    for i in range(1, TRIALS + 1):
        # snapshot_rx() does not consume the buffer -- flush stale audio
        # from prior trials or captures accumulate across the whole run.
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        tx_audio = afsk.modulate(PAYLOAD, profile=profile)
        t0 = time.time()
        tx.send(tx_audio)
        tx_wall = time.time() - t0
        time.sleep(CAPTURE_TAIL)
        captured = rx.snapshot_rx()
        result = afsk.demodulate(captured, profile=profile)
        good = result.get("payload") == PAYLOAD
        confidences.append(result.get("confidence", 0.0))
        ok += int(good)
        print(f"  [{label}] trial {i}/{TRIALS}: tx={tx_wall:.2f}s captured={len(captured)}samp "
              f"confidence={result.get('confidence', 0.0):.1f} decoded={good}")
        time.sleep(0.5)
    rate = ok / TRIALS
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    print(f"  [{label}] => {ok}/{TRIALS} ok ({rate*100:.0f}%), avg confidence={avg_conf:.1f}")
    return rate


def run_candidate(t_a, t_b, name_a, name_b, freq0, freq1):
    profile = make_profile(freq0, freq1, name=f"edge_{freq0:.0f}_{freq1:.0f}")
    print(f"\n-- freq0={freq0:.0f}Hz freq1={freq1:.0f}Hz (baud={BAUD}) --")
    rate_ab = run_point(t_a, t_b, profile, f"{name_a}->{name_b}")
    rate_ba = run_point(t_b, t_a, profile, f"{name_b}->{name_a}")
    worst = min(rate_ab, rate_ba)
    print(f"  worst-direction success at freq0={freq0:.0f}/freq1={freq1:.0f}: {worst*100:.0f}%")
    return worst


def walk_low_edge(t_a, t_b, name_a, name_b):
    print(f"\n=== LOW EDGE WALK (freq1 fixed at {afsk.PROFILE_600.freq1:.0f} Hz) ===")
    last_good = None
    for freq0 in LOW_CANDIDATES:
        if afsk.PROFILE_600.freq1 - freq0 < TONE_SEPARATION_MIN:
            print(f"  skipping freq0={freq0} -- too close to freq1 (< {TONE_SEPARATION_MIN:.0f} Hz separation)")
            continue
        worst = run_candidate(t_a, t_b, name_a, name_b, freq0, afsk.PROFILE_600.freq1)
        if worst >= SUCCESS_THRESHOLD:
            last_good = freq0
        else:
            print(f"  freq0={freq0} failed the {SUCCESS_THRESHOLD*100:.0f}% bar -- stopping low-edge walk")
            break
    print(f"\nLOW EDGE RESULT: lowest reliable freq0 (both directions) = {last_good}")
    return last_good


def walk_high_edge(t_a, t_b, name_a, name_b):
    print(f"\n=== HIGH EDGE WALK (freq0 fixed at {afsk.PROFILE_600.freq0:.0f} Hz) ===")
    last_good = None
    for freq1 in HIGH_CANDIDATES:
        if freq1 - afsk.PROFILE_600.freq0 < TONE_SEPARATION_MIN:
            print(f"  skipping freq1={freq1} -- too close to freq0 (< {TONE_SEPARATION_MIN:.0f} Hz separation)")
            continue
        worst = run_candidate(t_a, t_b, name_a, name_b, afsk.PROFILE_600.freq0, freq1)
        if worst >= SUCCESS_THRESHOLD:
            last_good = freq1
        else:
            print(f"  freq1={freq1} failed the {SUCCESS_THRESHOLD*100:.0f}% bar -- stopping high-edge walk")
            break
    print(f"\nHIGH EDGE RESULT: highest reliable freq1 (both directions) = {last_good}")
    return last_good


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=None)
    ap.add_argument("--skip-low", action="store_true", help="skip the low-edge walk")
    ap.add_argument("--skip-high", action="store_true", help="skip the high-edge walk")
    ap.add_argument("--probe", nargs=2, type=float, metavar=("FREQ0", "FREQ1"),
                     help="skip both walks; just run TRIALS at this freq0/freq1 pair, both directions")
    args = ap.parse_args()
    global TRIALS
    if args.trials is not None:
        TRIALS = args.trials

    print("opening radios...")
    t_ic705 = RadioTransport("ic705")
    t_ht = RadioTransport("ht")
    try:
        t_ic705.start_receiving()
        t_ht.start_receiving()
        print("warming up 2s...")
        time.sleep(2)

        if args.probe is not None:
            freq0, freq1 = args.probe
            run_candidate(t_ic705, t_ht, "ic705", "ht", freq0, freq1)
            return 0

        low_edge = None if args.skip_low else walk_low_edge(t_ic705, t_ht, "ic705", "ht")
        high_edge = None if args.skip_high else walk_high_edge(t_ic705, t_ht, "ic705", "ht")

        print("\n\n===== FINAL SUMMARY =====")
        print(f"Usable band (reliable >= {SUCCESS_THRESHOLD*100:.0f}% both directions, baud={BAUD}): "
              f"{low_edge if low_edge is not None else '?'} Hz to "
              f"{high_edge if high_edge is not None else '?'} Hz")
    finally:
        t_ic705.close()
        t_ht.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
