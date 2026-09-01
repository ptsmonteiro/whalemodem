"""Diagnostic: sweep TX drive level (afsk.modulate amplitude) from ic7300 to
ic705 only, with compressor/ALC off on both radios, and report per-level
sync confidence + level/clipping/envelope stats.

Run: python scripts/diag_tx_volume_sweep.py
"""

import time

import numpy as np

import bench
from whale import afsk
from whale.transport import RX_SAMPLE_RATE

PAYLOAD = (b"hello whalemodem " * 4)
AMPLITUDES = [0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0]


def level_stats(audio):
    if len(audio) == 0:
        return "EMPTY"
    peak = float(np.max(np.abs(audio)))
    clipped = float(np.mean(np.abs(audio) >= 0.999))
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    return f"peak={peak:.4f} rms={rms:.4f} clipped={clipped:.4%}"


def trial(tx, rx, amplitude, settle_s=1.5, listen_s=6.0):
    rx.snapshot_rx()
    time.sleep(settle_s)
    rx.snapshot_rx()

    audio = afsk.modulate(PAYLOAD, amplitude=amplitude)
    keyed = tx.send(audio)

    time.sleep(listen_s)
    captured = rx.snapshot_rx()
    result = afsk.demodulate(captured)
    ok = result.get("synced") and result.get("payload") == PAYLOAD
    print(f"  amplitude={amplitude:.2f}  keyed={keyed:.2f}s  "
          f"synced={result.get('synced')} conf={result.get('confidence'):.3f}  "
          f"match={ok}  rx[{level_stats(captured)}]")
    return ok, result.get("confidence")


def main():
    with bench.radio_pair("ic7300", "ic705", warmup=3.0) as (tx, rx):
        print("== ic7300 -> ic705, sweeping TX amplitude ==")
        rows = []
        for amp in AMPLITUDES:
            ok, conf = trial(tx, rx, amp)
            rows.append((amp, ok, conf))
            time.sleep(1)

        print("\n== SUMMARY ==")
        for amp, ok, conf in rows:
            print(f"  amplitude={amp:.2f}: {'OK' if ok else 'FAIL'} (conf={conf:.3f})")


if __name__ == "__main__":
    main()
