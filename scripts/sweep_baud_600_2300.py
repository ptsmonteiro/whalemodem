"""Bench sweep: how high can baud go with tones fixed at 600/2300 Hz (the
measured band edges from scripts/measure_band_edges.py), on the real
STA1(ic705)/STA2(ht) link?

Same method as scripts/sweep_baud_payload.py: bypasses whale.link's ARQ,
one direct modulate -> TX -> capture -> demodulate per trial, tested in
both directions, worst direction decides. See scripts/bench.py.

Suspect the 700-baud cliff seen without padding (0/5 both directions,
right after 600 baud passing) is a PTT/audio-chain startup transient
racing the frame rather than a genuine passband limit -- the real frame
starts right at t=0 of the TX buffer, so any settling time on the
transmit or receive side eats into the sync preamble. To test that, every
trial here is padded (bench.run_trials(pad=True)): 1s of low-level noise
before and after the real modulated frame, keeping the frame itself
untouched but giving the audio chain a full second to settle before the
sync preamble arrives (and a second of trailing noise so a captured tail
overrun doesn't truncate the frame either).

Run: python scripts/sweep_baud_600_2300.py
"""
import sys

import bench
from whale import afsk

FREQ0 = 1200.0
FREQ1 = 2200.0
PAYLOAD = bench.ACK_SHAPED_PAYLOAD

BAUD_CANDIDATES = [600, 900, 1200, 1400, 1600]


def make_profile(baud):
    return afsk.Profile(name=f"{baud}baud_1200_2200", mode_id=99, baud=baud, freq0=FREQ0, freq1=FREQ1)


def main():
    with bench.radio_pair() as (t_ic705, t_ht):
        print(f"\n=== BAUD SWEEP @ freq0={FREQ0:.0f}Hz freq1={FREQ1:.0f}Hz ===")

        def measure(baud):
            profile = make_profile(baud)
            airtime = afsk.frame_seconds(len(PAYLOAD), profile)
            print(f"\n-- baud={baud} (frame airtime ~{airtime:.2f}s) --")
            return bench.run_both_directions(t_ic705, t_ht, profile, PAYLOAD, pad=True)

        last_good = bench.walk(BAUD_CANDIDATES, measure, lambda baud: f"baud={baud}")

        print("\n\n===== FINAL SUMMARY =====")
        print(f"Highest reliable baud @ 600/2300 Hz "
              f"(>= {bench.SUCCESS_THRESHOLD * 100:.0f}% both directions): {last_good}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
