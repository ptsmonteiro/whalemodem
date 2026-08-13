"""Bench sweep: how high can baud go with tones fixed at 600/2300 Hz (the
measured band edges from scripts/measure_band_edges.py), on the real
STA1(ic705)/STA2(ht) link?

Same method as scripts/sweep_baud_payload.py: bypasses whale.link's ARQ,
one direct modulate -> TX -> capture -> demodulate per trial, tested in
both directions, worst direction decides.

Suspect the 700-baud cliff seen without padding (0/5 both directions,
right after 600 baud passing) is a PTT/audio-chain startup transient
racing the frame rather than a genuine passband limit -- the real frame
starts right at t=0 of the TX buffer, so any settling time on the
transmit or receive side eats into the sync preamble. To test that, each
TX buffer here is padded with 1s of noise before and after the real
modulated frame, keeping the frame itself untouched but giving the audio
chain a full second to settle before the sync preamble arrives (and a
second of trailing noise so a captured tail overrun doesn't truncate the
frame either).

Run: python scripts/sweep_baud_600_2300.py
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale import afsk
from whale.transport import RadioTransport, SAMPLE_RATE

TRIALS = 5
SUCCESS_THRESHOLD = 0.8

FREQ0 = 1200.0
FREQ1 = 2200.0
PAYLOAD = bytes([0x06, 0x00])

PAD_SECONDS = 1.0
PAD_AMPLITUDE = 0.1  # low-level noise, not full-scale -- just occupies the channel/settles the chain

CAPTURE_TAIL = 1.0 + PAD_SECONDS  # frame now ends 1s of pad before tx finishes; keep the same post-frame margin

BAUD_CANDIDATES = [600, 900, 1200, 1400, 1600]


def make_profile(baud):
    return afsk.Profile(name=f"{baud}baud_1200_2200", mode_id=99, baud=baud, freq0=FREQ0, freq1=FREQ1)


def _noise_pad(seconds=PAD_SECONDS, amplitude=PAD_AMPLITUDE):
    n = int(SAMPLE_RATE * seconds)
    rng = np.random.default_rng()
    return (amplitude * rng.standard_normal(n)).astype(np.float32)


def run_point(tx, rx, profile, label):
    ok = 0
    confidences = []
    for i in range(1, TRIALS + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        frame_audio = afsk.modulate(PAYLOAD, profile=profile)
        tx_audio = np.concatenate([_noise_pad(), frame_audio, _noise_pad()])
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


def main():
    print("opening radios...")
    t_ic705 = RadioTransport("ic705")
    t_ht = RadioTransport("ht")
    try:
        t_ic705.start_receiving()
        t_ht.start_receiving()
        print("warming up 2s...")
        time.sleep(2)

        print(f"\n=== BAUD SWEEP @ freq0={FREQ0:.0f}Hz freq1={FREQ1:.0f}Hz ===")
        last_good = None
        for baud in BAUD_CANDIDATES:
            profile = make_profile(baud)
            airtime = afsk.frame_seconds(len(PAYLOAD), profile)
            print(f"\n-- baud={baud} (frame airtime ~{airtime:.2f}s) --")
            rate_ab = run_point(t_ic705, t_ht, profile, "ic705->ht")
            rate_ba = run_point(t_ht, t_ic705, profile, "ht->ic705")
            worst = min(rate_ab, rate_ba)
            print(f"  worst-direction success at baud={baud}: {worst*100:.0f}%")
            if worst >= SUCCESS_THRESHOLD:
                last_good = baud
            else:
                print(f"  baud={baud} failed the {SUCCESS_THRESHOLD*100:.0f}% bar -- stopping")
                break

        print("\n\n===== FINAL SUMMARY =====")
        print(f"Highest reliable baud @ 600/2300 Hz (>= {SUCCESS_THRESHOLD*100:.0f}% both directions): {last_good}")
    finally:
        t_ic705.close()
        t_ht.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
