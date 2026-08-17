"""Bench probe: is the frame-size ceiling actually a frame-size ceiling?

Every sweep that found one (scripts/sweep_baud_payload.py,
scripts/sweep_payload_1200_2200.py) varied payload size and read off the
byte count where decoding stopped. But a bigger payload is also a longer
keying, so those two things were never separated. This probe separates
them: the payload is held at one small size and only the amount of dead air
*before* it changes, so the frame's size is constant and its position in
the transmission moves.

If the ceiling is about frame size, every trial here decodes -- the frame is
the same 40 bytes throughout. If it is about elapsed time since PTT, the
failures appear in a band that tracks the padding.

RESOLVED: the cause was **priority scan enabled on the HT**, which briefly
mutes the receiver every ~3s to check the priority channel. Measured at
~280ms of lost audio starting ~3.0s after PTT key-down and roughly every
3.1s after. A hole that long is ~170 bits at 600 baud, so any frame
overlapping one is unrecoverable -- and because whale/afsk.py lays symbol
sample points on a rigid grid from the sync peak, a frame that resumes
after a hole is also misaligned for its whole remainder, which is why these
failed CRC at ~0.99 sync confidence rather than failing to sync.

This probe is what separated the two explanations, and it is kept as the
regression test for the setting. Results:

    priority scan ON    ic705->ht  4/4 up to a 2.64s frame end, 0/4 with
                                   the frame across 3.0s, 4/4 again after
    priority scan OFF   both legs  4/4 at every offset, 0.98 confidence

If a size- or duration-shaped failure ever reappears on this bench, run
this before anything else: it distinguishes "long frames are hard" from
"something mutes the receiver on a timer" in about two minutes, and those
two look identical in a payload sweep.

Run: python scripts/probe_tx_duration_dropout.py
     python scripts/probe_tx_duration_dropout.py --trials 6 --payload 40
     python scripts/probe_tx_duration_dropout.py --both-legs
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale import afsk, framing
from whale.transport import RadioTransport, SAMPLE_RATE, PTT_LEAD

PROFILE = afsk.PROFILE_600
LEAD_PADS = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
TRIALS = 4
PAYLOAD_BYTES = 40
PAD_AMPLITUDE = 0.05  # quiet noise, just to keep the carrier modulated
CAPTURE_TAIL = 0.8

# Time between PTT key-down and the first audio sample reaching the air:
# PTT_LEAD plus the ~130ms the output stream spends opening and filling its
# first buffer (see whale/transport.py's PTT_LEAD comment).
STREAM_FILL = 0.13


def _noise(seconds, rng):
    return (PAD_AMPLITUDE * rng.standard_normal(int(SAMPLE_RATE * seconds))).astype(np.float32)


def frame_window(lead_pad, payload_len):
    """When the frame's own audio starts and ends, relative to PTT key-down."""
    bits = (len(framing.head_pad_bits(PROFILE.baud)) + len(framing.SYNC_BITS)
            + 8 + 8 * payload_len + 16 + len(framing.tail_pad_bits(PROFILE.baud)))
    start = PTT_LEAD + STREAM_FILL + lead_pad
    return start, start + bits / PROFILE.baud


def run_point(tx, rx, lead_pad, payload, trials, rng):
    ok = 0
    confs = []
    for _ in range(trials):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        audio = np.concatenate([_noise(lead_pad, rng),
                                afsk.modulate(payload, profile=PROFILE)]) \
            if lead_pad > 0 else afsk.modulate(payload, profile=PROFILE)
        tx.send(audio)
        time.sleep(CAPTURE_TAIL)
        result = afsk.demodulate(rx.snapshot_rx(), profile=PROFILE)
        ok += int(result.get("payload") == payload)
        confs.append(result.get("confidence") or 0.0)
    return ok, float(np.mean(confs))


def run_leg(tx, rx, label, args, rng):
    print(f"\n-- {label}, payload fixed at {args.payload} bytes --")
    print(f"{'lead pad':>9} {'frame on air':>16} {'decoded':>9} {'avg conf':>9}")
    results = []
    for pad in LEAD_PADS:
        start, end = frame_window(pad, args.payload)
        ok, conf = run_point(tx, rx, pad, args.payload_bytes, args.trials, rng)
        results.append((pad, ok, start, end))
        print(f"{pad:>8.1f}s {start:>6.2f}-{end:>5.2f}s "
              f"{ok:>5}/{args.trials} {conf:>9.3f}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=TRIALS)
    ap.add_argument("--payload", type=int, default=PAYLOAD_BYTES)
    ap.add_argument("--both-legs", action="store_true",
                    help="also run ht->ic705, which is expected to pass throughout")
    args = ap.parse_args()
    rng = np.random.default_rng(0)
    args.payload_bytes = bytes(rng.integers(0, 256, size=args.payload, dtype=np.uint8))

    print("opening radios...")
    sta1 = RadioTransport("ic705")
    sta2 = RadioTransport("ht")
    sta1.start_receiving()
    sta2.start_receiving()
    time.sleep(1.5)
    try:
        bad = run_leg(sta1, sta2, "ic705->ht", args, rng)
        if args.both_legs:
            run_leg(sta2, sta1, "ht->ic705", args, rng)
    finally:
        sta1.stop_receiving()
        sta2.stop_receiving()

    failing = [(pad, s, e) for pad, ok, s, e in bad if ok < args.trials]
    print("\n" + "=" * 62)
    if not failing:
        print("no failures at any padding -- the ~3.0s dropout did not reproduce")
    else:
        lo = min(s for _, s, _ in failing)
        hi = max(e for _, _, e in failing)
        print(f"failures at lead pads: {[p for p, _, _ in failing]}")
        print(f"those frames occupied {lo:.2f}-{hi:.2f}s after PTT key-down.")
        print("Payload size was identical in every trial, so a frame-size")
        print("ceiling cannot explain this; elapsed time since PTT can.")
    print("=" * 62)


if __name__ == "__main__":
    main()
