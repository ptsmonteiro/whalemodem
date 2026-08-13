"""Bench sweep: largest reliable payload at 1200 baud / 1200-2200 Hz tones
(the Bell-202-style profile that cleared 1200 baud in
scripts/sweep_baud_600_2300.py, after 600/2300 Hz topped out at 600 baud).

Same method as scripts/sweep_baud_payload.py's payload sweep: bypasses
whale.link's ARQ, one direct modulate -> TX -> capture -> demodulate per
trial, tested in both directions, worst direction decides. TX buffers are
padded with 1s of low-level noise before/after the frame (per the timing
test in sweep_baud_600_2300.py) to rule out PTT/audio-chain settling as a
confound -- this is a passband/frame-size characterization, not a timing one.

Run: python scripts/sweep_payload_1200_2200.py
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale import afsk, framing
from whale.transport import RadioTransport, SAMPLE_RATE

TRIALS = 5
SUCCESS_THRESHOLD = 0.8

FREQ0 = 1200.0
FREQ1 = 2200.0
BAUD = 1200

PAD_SECONDS = 1.0
PAD_AMPLITUDE = 0.1
CAPTURE_TAIL = 1.0 + PAD_SECONDS

PAYLOAD_CANDIDATES = [2, 4, 6, 8, 10, 20, 40, 80, 120, 160, 200, 255]

PROFILE = afsk.Profile(name="1200baud_1200_2200", mode_id=99, baud=BAUD, freq0=FREQ0, freq1=FREQ1)


def _noise_pad(seconds=PAD_SECONDS, amplitude=PAD_AMPLITUDE):
    n = int(SAMPLE_RATE * seconds)
    rng = np.random.default_rng()
    return (amplitude * rng.standard_normal(n)).astype(np.float32)


def run_point(tx, rx, payload, label):
    ok = 0
    confidences = []
    for i in range(1, TRIALS + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        frame_audio = afsk.modulate(payload, profile=PROFILE)
        tx_audio = np.concatenate([_noise_pad(), frame_audio, _noise_pad()])
        t0 = time.time()
        tx.send(tx_audio)
        tx_wall = time.time() - t0
        time.sleep(CAPTURE_TAIL)
        captured = rx.snapshot_rx()
        result = afsk.demodulate(captured, profile=PROFILE)
        good = result.get("payload") == payload
        confidences.append(result.get("confidence", 0.0))
        ok += int(good)
        diag = ""
        if not good and "end_index" in result:
            n_head = len(framing.head_pad_bits(BAUD))
            n_sync = len(framing.SYNC_BITS)
            expected_bits = n_head + n_sync + 8 + 8 * len(payload) + 16 + len(framing.tail_pad_bits(BAUD))
            diag = f" [near-miss: end_index={result['end_index']} vs expected ~{round(expected_bits/BAUD*SAMPLE_RATE)} samples]"
        print(f"  [{label}] trial {i}/{TRIALS}: tx={tx_wall:.2f}s captured={len(captured)}samp "
              f"confidence={result.get('confidence', 0.0):.1f} decoded={good}{diag}")
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

        print(f"\n=== PAYLOAD SWEEP @ baud={BAUD} freq0={FREQ0:.0f}Hz freq1={FREQ1:.0f}Hz ===")
        last_good = None
        for size in PAYLOAD_CANDIDATES:
            payload = bytes((i % 256 for i in range(size)))
            airtime = afsk.frame_seconds(size, PROFILE)
            print(f"\n-- payload={size} bytes (frame airtime ~{airtime:.2f}s) --")
            rate_ab = run_point(t_ic705, t_ht, payload, "ic705->ht")
            rate_ba = run_point(t_ht, t_ic705, payload, "ht->ic705")
            worst = min(rate_ab, rate_ba)
            print(f"  worst-direction success at payload={size}: {worst*100:.0f}%")
            if worst >= SUCCESS_THRESHOLD:
                last_good = size
            else:
                print(f"  payload={size} failed the {SUCCESS_THRESHOLD*100:.0f}% bar -- stopping")
                break

        print("\n\n===== FINAL SUMMARY =====")
        print(f"Largest reliable payload @ baud={BAUD}, 1200/2200 Hz (>= {SUCCESS_THRESHOLD*100:.0f}% both directions): {last_good} bytes")
    finally:
        t_ic705.close()
        t_ht.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
