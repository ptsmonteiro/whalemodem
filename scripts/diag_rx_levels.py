"""Diagnostic: capture raw RX audio from both radios during a real TX from
the other one, and report level/clipping/envelope stats -- looking for
front-end clipping or a slow-rising ALC/AGC ramp masking the frame start.

Run: python scripts/diag_rx_levels.py
"""

import sys
import time

import numpy as np

import bench
from whale import afsk
from whale.transport import RX_SAMPLE_RATE

PAYLOAD = (b"hello whalemodem " * 4)


def stats(name, audio, sr):
    n = len(audio)
    dur = n / sr
    if n == 0:
        print(f"   {name}: EMPTY")
        return
    peak = np.max(np.abs(audio))
    clipped = np.mean(np.abs(audio) >= 0.999)
    # RMS envelope in 100ms windows to see level ramp / ALC behavior over time.
    win = max(1, int(sr * 0.1))
    n_wins = n // win
    env = [float(np.sqrt(np.mean(audio[i*win:(i+1)*win].astype(np.float64) ** 2)))
           for i in range(n_wins)]
    print(f"   {name}: {dur:.2f}s ({n} samples), peak={peak:.4f}, "
          f"clipped_frac={clipped:.4%}")
    print(f"      rms envelope (100ms steps): " +
          " ".join(f"{v:.3f}" for v in env))


def probe(tx_name, tx, rx_name, rx, sr, settle_s=2.0, listen_s=6.0):
    print(f"\n== {tx_name} -> {rx_name} ==")
    rx.snapshot_rx()
    time.sleep(settle_s)
    idle = rx.snapshot_rx()
    print("  idle noise floor before TX:")
    stats("idle", idle, sr)

    audio = afsk.modulate(PAYLOAD)
    print(f"   sending {len(PAYLOAD)} bytes, tx audio {len(audio)/afsk.SAMPLE_RATE:.2f}s")
    t0 = time.monotonic()
    keyed = tx.send(audio)
    t1 = time.monotonic()
    print(f"   keyed {keyed:.2f}s (PTT on -> PTT off), wall {t1 - t0:.2f}s")

    t_listen_start = time.monotonic()
    time.sleep(listen_s)
    t_listen_end = time.monotonic()
    captured = rx.snapshot_rx()
    print(f"   requested listen {listen_s:.2f}s, actual wall sleep {t_listen_end - t_listen_start:.2f}s")
    print("  post-TX capture:")
    stats("captured", captured, sr)

    result = afsk.demodulate(captured)
    print(f"   synced={result.get('synced')} confidence={result.get('confidence')}")
    return captured


def main():
    with bench.radio_pair("ic705", "ic7300", warmup=3.0) as (a, b):
        c1 = probe("ic705", a, "ic7300", b, RX_SAMPLE_RATE)
        time.sleep(1)
        c2 = probe("ic7300", b, "ic705", a, RX_SAMPLE_RATE)
        np.save("scratch_ic7300_rx.npy", c1)
        np.save("scratch_ic705_rx.npy", c2)
        print("\nSaved raw captures to scratch_ic7300_rx.npy / scratch_ic705_rx.npy")


if __name__ == "__main__":
    main()
