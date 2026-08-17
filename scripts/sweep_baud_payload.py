"""Bench sweep to find, empirically over the real STA1(ic705)/STA2(ht) link:

  1. the highest baud (at PROFILE_600's tones, 700/1500 Hz) that still
     decodes reliably, and
  2. at that baud, the largest payload that still decodes reliably,

tested in both directions (ic705->ht and ht->ic705, since the two legs have
different measured SNR -- see whale/afsk.py docstring), taking whichever
direction is weaker as the limit.

Bypasses whale.link's ARQ entirely (like scripts/probe_600_ack.py) so each
data point is a single direct modulate -> TX -> capture -> demodulate, no
retries/acks muddying the result.

Run: python scripts/sweep_baud_payload.py
"""
import argparse
import dataclasses
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale import afsk, framing
from whale.transport import RadioTransport, SAMPLE_RATE

SETTLE_AFTER_PTT = 0.3
CAPTURE_TAIL = 1.0
TRIALS = 5
SUCCESS_THRESHOLD = 0.8  # fraction of trials that must decode cleanly to call a point "good"

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


def run_point(tx, rx, payload, profile, label):
    ok = 0
    confidences = []
    for i in range(1, TRIALS + 1):
        # snapshot_rx() does not consume the buffer -- flush stale audio
        # from prior trials or captures accumulate across the whole run.
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        tx_audio = afsk.modulate(payload, profile=profile)
        t0 = time.time()
        tx.send(tx_audio)
        tx_wall = time.time() - t0
        time.sleep(CAPTURE_TAIL)
        captured = rx.snapshot_rx()
        result = afsk.demodulate(captured, profile=profile)
        good = result.get("payload") == payload
        confidences.append(result.get("confidence", 0.0))
        ok += int(good)
        diag = ""
        if not good and "end_index" in result:
            # Measure the frame the decoder *found*, not where it ended in
            # the buffer. end_index is an absolute offset into the capture,
            # and the frame starts ~1s into it (PTT lead, stream fill, head
            # pad), so comparing it against a bare frame duration overstates
            # the span by that much. This printed "~1.2x expected" for
            # perfectly well-read frames, and that number was taken as
            # evidence of a false sync lock on garbage -- it was not. What
            # the decoder believed is start_index..end_index, which is the
            # length byte read back as a duration.
            n_sync = len(framing.SYNC_BITS)
            expected_bits = n_sync + 8 + 8 * len(payload) + 16
            expected = round(expected_bits / profile.baud * SAMPLE_RATE)
            span = result["end_index"] - result.get("start_index", 0)
            diag = (f" [near-miss: frame span={span} vs expected {expected} samples"
                    f" ({span/expected:.2f}x)]")
        print(f"  [{label}] trial {i}/{TRIALS}: tx={tx_wall:.2f}s captured={len(captured)}samp "
              f"confidence={result.get('confidence', 0.0):.1f} decoded={good}{diag}")
        time.sleep(0.5)
    rate = ok / TRIALS
    print(f"  [{label}] => {ok}/{TRIALS} ok ({rate*100:.0f}%), "
          f"avg confidence={sum(confidences)/len(confidences):.1f}")
    return rate, confidences


def sweep_baud(t_a, t_b, name_a, name_b, small_payload):
    print(f"\n=== BAUD SWEEP (payload={len(small_payload)} bytes fixed) ===")
    results = {}
    last_good_baud = None
    for baud in BAUD_CANDIDATES:
        profile = make_profile(baud)
        airtime = afsk.frame_seconds(len(small_payload), profile)
        print(f"\n-- baud={baud} (frame airtime ~{airtime:.2f}s) --")
        rate_ab, _ = run_point(t_a, t_b, small_payload, profile, f"{name_a}->{name_b}")
        rate_ba, _ = run_point(t_b, t_a, small_payload, profile, f"{name_b}->{name_a}")
        worst = min(rate_ab, rate_ba)
        results[baud] = (rate_ab, rate_ba, worst)
        print(f"  worst-direction success at baud={baud}: {worst*100:.0f}%")
        if worst >= SUCCESS_THRESHOLD:
            last_good_baud = baud
        else:
            print(f"  baud={baud} failed the {SUCCESS_THRESHOLD*100:.0f}% bar -- stopping baud sweep")
            break
    print(f"\nBAUD SWEEP RESULT: highest reliable baud = {last_good_baud}")
    return last_good_baud, results


def sweep_payload(t_a, t_b, name_a, name_b, baud):
    print(f"\n=== PAYLOAD SWEEP (baud={baud} fixed) ===")
    profile = make_profile(baud)
    results = {}
    last_good_size = None
    for size in PAYLOAD_CANDIDATES:
        payload = bytes((i % 256 for i in range(size)))
        airtime = afsk.frame_seconds(size, profile)
        print(f"\n-- payload={size} bytes (frame airtime ~{airtime:.2f}s) --")
        rate_ab, _ = run_point(t_a, t_b, payload, profile, f"{name_a}->{name_b}")
        rate_ba, _ = run_point(t_b, t_a, payload, profile, f"{name_b}->{name_a}")
        worst = min(rate_ab, rate_ba)
        results[size] = (rate_ab, rate_ba, worst)
        print(f"  worst-direction success at payload={size}: {worst*100:.0f}%")
        if worst >= SUCCESS_THRESHOLD:
            last_good_size = size
        else:
            print(f"  payload={size} failed the {SUCCESS_THRESHOLD*100:.0f}% bar -- stopping payload sweep")
            break
    print(f"\nPAYLOAD SWEEP RESULT: largest reliable payload at baud={baud} = {last_good_size}")
    return last_good_size, results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-baud", action="store_true", help="skip baud sweep, use --baud directly")
    ap.add_argument("--baud", type=int, default=None, help="baud to use for payload sweep if --skip-baud")
    ap.add_argument("--trials", type=int, default=None)
    ap.add_argument("--probe-baud", type=int, default=None,
                     help="skip both sweeps; just run TRIALS at this baud/--probe-payload both directions")
    ap.add_argument("--probe-payload", type=int, default=2)
    args = ap.parse_args()
    global TRIALS
    if args.trials is not None:
        TRIALS = args.trials

    small_payload = bytes([0x06, 0x00])  # DATA_ACK-shaped, worst-case-small frame

    print("opening radios...")
    t_ic705 = RadioTransport("ic705")
    t_ht = RadioTransport("ht")
    try:
        t_ic705.start_receiving()
        t_ht.start_receiving()
        print("warming up 2s...")
        time.sleep(2)

        if args.probe_baud is not None:
            profile = make_profile(args.probe_baud)
            payload = bytes((i % 256 for i in range(args.probe_payload)))
            print(f"\n=== PROBE baud={args.probe_baud} payload={len(payload)} bytes ===")
            run_point(t_ic705, t_ht, payload, profile, "ic705->ht")
            run_point(t_ht, t_ic705, payload, profile, "ht->ic705")
            return 0

        if args.skip_baud:
            best_baud = args.baud
            assert best_baud is not None, "--baud required with --skip-baud"
        else:
            best_baud, baud_results = sweep_baud(t_ic705, t_ht, "ic705", "ht", small_payload)
            if best_baud is None:
                print("No baud even at the slowest candidate cleared the bar -- aborting.")
                return 1

        best_payload, payload_results = sweep_payload(t_ic705, t_ht, "ic705", "ht", best_baud)

        print("\n\n===== FINAL SUMMARY =====")
        print(f"Max reliable baud (>= {SUCCESS_THRESHOLD*100:.0f}% both directions): {best_baud}")
        print(f"Max reliable payload at that baud: {best_payload} bytes")
    finally:
        t_ic705.close()
        t_ht.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
