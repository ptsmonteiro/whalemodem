"""Bench sweep: largest reliable payload at 1200 baud / 1200-2200 Hz tones
(the Bell-202-style profile that cleared 1200 baud in
scripts/sweep_baud_600_2300.py, after 600/2300 Hz topped out at 600 baud).

Same method as scripts/sweep_baud_payload.py's payload sweep: bypasses
whale.link's ARQ, one direct modulate -> TX -> capture -> demodulate per
trial, tested in both directions, worst direction decides. See
scripts/bench.py.

Trials are padded (bench.run_trials(pad=True)) with 1s of low-level noise
before/after the frame, per the timing test in sweep_baud_600_2300.py, to
rule out PTT/audio-chain settling as a confound -- this is a
passband/frame-size characterization, not a timing one.

Run: python scripts/sweep_payload_1200_2200.py
"""
import sys

import bench
from whale import afsk

FREQ0 = 1200.0
FREQ1 = 2200.0
BAUD = 1200

PAYLOAD_CANDIDATES = [2, 4, 6, 8, 10, 20, 40, 80, 120, 160, 200, 255]

PROFILE = afsk.Profile(name="1200baud_1200_2200", mode_id=99, baud=BAUD, freq0=FREQ0, freq1=FREQ1)


def main():
    with bench.radio_pair() as (t_ic705, t_ht):
        print(f"\n=== PAYLOAD SWEEP @ baud={BAUD} freq0={FREQ0:.0f}Hz freq1={FREQ1:.0f}Hz ===")

        def measure(size):
            payload = bench.counting_payload(size)
            airtime = afsk.frame_seconds(size, PROFILE)
            print(f"\n-- payload={size} bytes (frame airtime ~{airtime:.2f}s) --")
            return bench.run_both_directions(t_ic705, t_ht, PROFILE, payload, pad=True)

        last_good = bench.walk(PAYLOAD_CANDIDATES, measure, lambda size: f"payload={size}")

        print("\n\n===== FINAL SUMMARY =====")
        print(f"Largest reliable payload @ baud={BAUD}, 1200/2200 Hz "
              f"(>= {bench.SUCCESS_THRESHOLD * 100:.0f}% both directions): {last_good} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
