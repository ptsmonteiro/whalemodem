"""Bench measurement: the sample-clock offset between the two stations.

This settles, without decoding anything, whether the two sound cards run at
meaningfully different rates -- a mechanism once proposed for the
frame-size ceiling that scripts/sweep_payload_1200_2200.py and the 600-baud
sweep both hit (160-byte payloads fail while 120 passes, at every baud).

Measured on this bench, and the answer is no:

    ic705->ht   -3.7 ppm     ht->ic705   +3.1 ppm     sum  -0.6 ppm

The two legs are reciprocal to within 0.6 ppm, which is what says the
number is a real clock difference rather than an artefact of the method.
3.4 ppm is ~100x too little to cost a single bit: a frame dies when its
accumulated timing error reaches half a symbol, needing ~366 ppm at 160
bytes and ~550 ppm at the 102-byte production frame. The ceiling was
priority scan on the HT muting its receiver every ~3s -- see
scripts/probe_tx_duration_dropout.py.

Run it after any change of radio, interface, or cabling. The decoder has no
timing recovery (it lays symbol sample points on a rigid integer-sample
grid from the sync peak), so it depends on this number staying small, and
nothing else in the suite would notice if it stopped being.

Method: transmit a steady tone of known frequency, and measure what
frequency comes back at the far end. Nothing about the frame format is
involved, so this measures the clocks alone -- ratio = f_measured / f_sent,
and (ratio - 1) in ppm is the offset. FM recovers audio as audio, so any RF
frequency error drops out; what is left is the transmitting card's DAC clock
against the receiving card's ADC clock.

The measurement is run in both directions, and that is the part which makes
it conclusive rather than suggestive. Each station uses one sound card for
both input and output, so if the offset is really the clocks then leg
STA1->STA2 measures cB/cA and leg STA2->STA1 measures cA/cB: the two are
reciprocal, i.e. equal in magnitude and opposite in sign. A common-mode
error in this measurement (a tone generator bug, a resampling artefact)
would instead show the same sign both ways.

Frequency is estimated by phase regression rather than an FFT peak: mixing
the capture down by the nominal tone frequency leaves a residual whose
phase advances linearly at exactly the offset, and fitting that line over a
multi-second capture resolves far below the 0.25 Hz an FFT bin would give.
400 ppm at 1500 Hz is 0.6 Hz, so bin resolution alone would not be enough.

Run: python scripts/measure_clock_offset.py
     python scripts/measure_clock_offset.py --seconds 6 --trials 5
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale.transport import RadioTransport, SAMPLE_RATE

TONE_HZ = 1500.0  # the centre the profiles share, comfortably mid-passband
TONE_SECONDS = 4.0
TRIALS = 3
AMPLITUDE = 0.6
CAPTURE_TAIL = 0.6

# Fraction of peak envelope that counts as "the tone is present", and how
# much of each end of that region to discard before fitting. The edges hold
# the PTT transient, the AGC settling, and the amplitude ramp, none of which
# have a clean phase slope.
_PRESENT_FRACTION = 0.5
_EDGE_TRIM = 0.15


def _tone(seconds, freq=TONE_HZ, amplitude=AMPLITUDE):
    n = int(SAMPLE_RATE * seconds)
    t = np.arange(n) / SAMPLE_RATE
    sig = amplitude * np.cos(2 * np.pi * freq * t)
    ramp = int(SAMPLE_RATE * 0.005)
    window = np.hanning(2 * ramp)
    sig[:ramp] *= window[:ramp]
    sig[-ramp:] *= window[ramp:]
    return sig.astype(np.float32)


def _locate_tone(audio, freq=TONE_HZ):
    """The span of `audio` that actually holds the tone, edges trimmed."""
    n = np.arange(len(audio))
    mixed = audio * np.exp(-1j * 2 * np.pi * freq * n / SAMPLE_RATE)
    # Smooth the magnitude over ~20ms to get an envelope robust to noise.
    win = int(SAMPLE_RATE * 0.02)
    env = np.convolve(np.abs(mixed), np.ones(win) / win, mode="same")
    if env.max() <= 0:
        return None
    present = np.flatnonzero(env >= _PRESENT_FRACTION * env.max())
    if present.size < SAMPLE_RATE // 2:  # need at least 0.5s to fit
        return None
    start, end = int(present[0]), int(present[-1])
    trim = int((end - start) * _EDGE_TRIM)
    start, end = start + trim, end - trim
    if end - start < SAMPLE_RATE // 2:
        return None
    return start, end


def measure_ppm(audio, freq=TONE_HZ):
    """Offset of the tone in `audio` from `freq`, in ppm, or None.

    Also returns the residual of the straight-line phase fit: a genuine
    clock offset gives a near-perfect line, so a large residual means the
    capture was not a clean tone and the number should not be trusted.
    """
    span = _locate_tone(audio, freq)
    if span is None:
        return None
    start, end = span
    seg = np.asarray(audio[start:end], dtype=np.float64)
    n = np.arange(len(seg))
    mixed = seg * np.exp(-1j * 2 * np.pi * freq * n / SAMPLE_RATE)
    # Low-pass the product so the phase we unwrap is the offset alone and
    # not the sum/noise terms riding on it.
    win = int(SAMPLE_RATE * 0.002)
    smoothed = np.convolve(mixed, np.ones(win) / win, mode="valid")
    phase = np.unwrap(np.angle(smoothed))
    t = np.arange(len(phase)) / SAMPLE_RATE
    slope, intercept = np.polyfit(t, phase, 1)
    residual = float(np.sqrt(np.mean((phase - (slope * t + intercept)) ** 2)))
    df = slope / (2 * np.pi)
    return {
        "ppm": df / freq * 1e6,
        "df_hz": df,
        "residual_rad": residual,
        "fit_seconds": len(phase) / SAMPLE_RATE,
    }


def run_leg(tx, rx, label, seconds, trials):
    print(f"\n-- {label} --")
    results = []
    for i in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        tx.send(_tone(seconds))
        time.sleep(CAPTURE_TAIL)
        captured = rx.snapshot_rx()
        m = measure_ppm(captured)
        if m is None:
            print(f"  trial {i}/{trials}: no usable tone in {len(captured)} samples")
            continue
        results.append(m["ppm"])
        print(f"  trial {i}/{trials}: {m['ppm']:+8.1f} ppm "
              f"({m['df_hz']:+.3f} Hz at {TONE_HZ:.0f}, fit {m['fit_seconds']:.2f}s, "
              f"residual {m['residual_rad']:.4f} rad)")
    if not results:
        print(f"  {label}: no measurement")
        return None
    arr = np.array(results)
    print(f"  {label} => mean {arr.mean():+.1f} ppm, spread {arr.max() - arr.min():.1f} ppm")
    return float(arr.mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=TONE_SECONDS)
    ap.add_argument("--trials", type=int, default=TRIALS)
    args = ap.parse_args()

    print("opening radios...")
    sta1 = RadioTransport("ic705")
    sta2 = RadioTransport("ht")
    sta1.start_receiving()
    sta2.start_receiving()
    print("warming up 2s...")
    time.sleep(2.0)
    try:
        a = run_leg(sta1, sta2, "ic705->ht", args.seconds, args.trials)
        b = run_leg(sta2, sta1, "ht->ic705", args.seconds, args.trials)
    finally:
        sta1.stop_receiving()
        sta2.stop_receiving()

    print("\n" + "=" * 66)
    if a is None or b is None:
        print("incomplete: one leg produced no measurement")
        return
    print(f"ic705->ht : {a:+8.1f} ppm")
    print(f"ht->ic705 : {b:+8.1f} ppm")
    print(f"sum       : {a + b:+8.1f} ppm   (0 if the two legs are reciprocal,")
    print("                              i.e. a genuine clock difference)")
    print(f"half-difference (per-pair offset): {(a - b) / 2:+.1f} ppm")
    print("=" * 66)


if __name__ == "__main__":
    main()
