"""Bench measurement: the sample-clock offset between the two stations.

This settles, without decoding anything, whether the two sound cards run at
meaningfully different rates -- a mechanism once proposed for the
frame-size ceiling that the historical baud sweeps hit (160-byte payloads fail
while 120 passes, at every baud).

Measured on this bench, and the answer is no:

    ic705->ht   -3.7 ppm     ht->ic705   +3.1 ppm     sum  -0.6 ppm

The two legs are reciprocal to within 0.6 ppm, which is what says the
number is a real clock difference rather than an artefact of the method.
3.4 ppm is ~100x too little to cost a single bit: a frame dies when its
accumulated timing error reaches half a symbol, needing ~366 ppm at 160
bytes and ~550 ppm at the 102-byte production frame. So whatever a
frame-size ceiling turns out to be, on this bench it is not this.

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
import time

import numpy as np

import bench
from whale.transport import RX_SAMPLE_RATE, SAMPLE_RATE

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
# A squelch opening can be several times the tone's own level for ~150ms, so
# the envelope's PEAK is not a safe reference for "the tone is present" -- on
# a radio that does this, half of peak sits above the tone and the tone is
# never found. A high percentile is dominated by the tone instead, which is
# most of the capture.
_LEVEL_PERCENTILE = 75
# The docstring's own rule, applied: a clean clock offset fits a straight line
# to within a fraction of a radian. Anything worse is not a clock measurement
# and is dropped rather than averaged in.
_MAX_RESIDUAL_RAD = 1.0


def _tone(seconds, freq=TONE_HZ, amplitude=AMPLITUDE):
    n = int(SAMPLE_RATE * seconds)
    t = np.arange(n) / SAMPLE_RATE
    sig = amplitude * np.cos(2 * np.pi * freq * t)
    ramp = int(SAMPLE_RATE * 0.005)
    window = np.hanning(2 * ramp)
    sig[:ramp] *= window[:ramp]
    sig[-ramp:] *= window[ramp:]
    return sig.astype(np.float32)


def _locate_tone(audio, freq=TONE_HZ, rate=RX_SAMPLE_RATE):
    """The span of `audio` that actually holds the tone, edges trimmed.

    `rate` is the rate of the captured audio, which is NOT the rate the tone
    was generated at: the transport decimates what it captures. Analysing a
    12 kHz capture on the 48 kHz transmit grid puts the mixer 4x away from
    the tone and the phase fit then measures that, not the clocks.
    """
    n = np.arange(len(audio))
    mixed = audio * np.exp(-1j * 2 * np.pi * freq * n / rate)
    # Smooth the magnitude over ~20ms to get an envelope robust to noise.
    win = int(rate * 0.02)
    env = np.convolve(np.abs(mixed), np.ones(win) / win, mode="same")
    if env.max() <= 0:
        return None
    level = float(np.percentile(env, _LEVEL_PERCENTILE))
    if level <= 0:
        return None
    # Longest contiguous run, not first-to-last: a transient before the tone
    # would otherwise stretch the span across the gap between them.
    present = env >= _PRESENT_FRACTION * level
    edges = np.diff(np.concatenate(([0], present.view(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    if starts.size == 0:
        return None
    longest = int(np.argmax(ends - starts))
    if ends[longest] - starts[longest] < rate // 2:  # need at least 0.5s to fit
        return None
    start, end = int(starts[longest]), int(ends[longest] - 1)
    trim = int((end - start) * _EDGE_TRIM)
    start, end = start + trim, end - trim
    if end - start < rate // 2:
        return None
    return start, end


def measure_ppm(audio, freq=TONE_HZ, rate=RX_SAMPLE_RATE):
    """Offset of the tone in `audio` from `freq`, in ppm, or None.

    Also returns the residual of the straight-line phase fit: a genuine
    clock offset gives a near-perfect line, so a large residual means the
    capture was not a clean tone and the number should not be trusted.
    """
    span = _locate_tone(audio, freq, rate)
    if span is None:
        return None
    start, end = span
    seg = np.asarray(audio[start:end], dtype=np.float64)
    n = np.arange(len(seg))
    mixed = seg * np.exp(-1j * 2 * np.pi * freq * n / rate)
    # Low-pass the product so the phase we unwrap is the offset alone and
    # not the sum/noise terms riding on it.
    win = int(rate * 0.002)
    smoothed = np.convolve(mixed, np.ones(win) / win, mode="valid")
    phase = np.unwrap(np.angle(smoothed))
    t = np.arange(len(phase)) / rate
    slope, intercept = np.polyfit(t, phase, 1)
    residual = float(np.sqrt(np.mean((phase - (slope * t + intercept)) ** 2)))
    df = slope / (2 * np.pi)
    return {
        "ppm": df / freq * 1e6,
        "df_hz": df,
        "residual_rad": residual,
        "fit_seconds": len(phase) / rate,
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
        usable = m["residual_rad"] <= _MAX_RESIDUAL_RAD
        if usable:
            results.append(m["ppm"])
        print(f"  trial {i}/{trials}: {m['ppm']:+8.1f} ppm "
              f"({m['df_hz']:+.3f} Hz at {TONE_HZ:.0f}, fit {m['fit_seconds']:.2f}s, "
              f"residual {m['residual_rad']:.4f} rad)"
              f"{'' if usable else '  DISCARDED: not a clean tone'}")
    if not results:
        print(f"  {label}: no measurement")
        return None
    arr = np.array(results)
    print(f"  {label} => median {np.median(arr):+.1f} ppm, spread "
          f"{arr.max() - arr.min():.1f} ppm, {len(arr)}/{trials} trials usable")
    return float(np.median(arr))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=TONE_SECONDS)
    ap.add_argument("--trials", type=int, default=TRIALS)
    args = ap.parse_args()

    with bench.radio_pair() as (sta1, sta2):
        a = run_leg(sta1, sta2, "ic705->ht", args.seconds, args.trials)
        b = run_leg(sta2, sta1, "ht->ic705", args.seconds, args.trials)

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
