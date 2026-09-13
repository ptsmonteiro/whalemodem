"""Measure the FM radio pair's audio passband, and its delay spread.

This is what produces `experiments/ofdm/results/measurements/bandwidth.json`,
which is in turn where the `audio_band_6db_hz` / `audio_band_10db_hz` numbers
in `whale/fm_channel.py`'s measured radio presets come from -- and
`tests/test_fm_channel.py` asserts the two agree, so regenerating that file is
how a preset gets refreshed after a radio changes.

It replaces a tool that lived in the retired `experiments/ofdm/` package and
went with it. The geometry is kept identical so the numbers stay comparable to
the committed ones: a 30 Hz tone grid from 120 to 3,900 Hz (127 tones), 1,600
samples per period at 48 kHz, blocks of 4 periods sampled at the start, middle
and end of a ~2.2 s transmission, in both directions.

Two things differ from the retired version, both deliberate:

  * It probes with a **repeated multitone period** rather than an OFDM frame
    with training symbols, so it needs no OFDM sync, no cyclic prefix and no
    payload machinery -- a repeated period is already cyclic, so any FFT window
    one period long sees the same spectrum. That removes the whole dependency
    on the retired package.
  * Tone phases are Schroeder rather than a data-derived sequence. A 127-tone
    comb at equal phase has a ~21 dB crest factor, and this path is
    intermodulation-limited rather than noise-limited (see
    `whale/modes/vf9.py`): probing it with a signal that clips the transmitter
    would measure our own distortion. Schroeder brings it to ~4 dB.

Neither changes what is being measured. Band edges are read from the
peak-normalised magnitude response, which is invariant to the probe's absolute
level and to its phases.

Why keying age matters, and why there are three windows: a handheld's squelch,
AGC and de-emphasis are still settling early in a transmission, so the
passband at 50 ms into a keying is not the passband at 2 s. The presets use
the middle window.

Delay spread is the RMS spread of the power-delay profile, from the IFFT of
the complex per-tone channel estimate with a -20 dB floor so it does not
measure its own noise. It is reported per direction and is invariant to bulk
timing offset, so it does not depend on where the capture happens to start.

This keys both radios. Run from the repository root:
    python scripts/measure_fm_audio_band.py
    python scripts/measure_fm_audio_band.py --trials 5
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import bench
from whale import afsk

BAND = (120.0, 3900.0)
SPACING_HZ = 30.0
PERIOD_SAMPLES = 1600          # 48 kHz / 30 Hz
RX_PERIOD_SAMPLES = 400        # 12 kHz / 30 Hz
PERIODS = 66                   # ~2.2 s, matching the committed measurement
MIN_PERIODS = 45               # usable trial: still spans start/middle/end
BLOCK = 4                      # periods per window; 133 ms at this geometry
TRIALS = 3
CAPTURE_TAIL = 0.8
INTER_TRIAL = 0.6
AMPLITUDE = 0.4                # see vf9.py: this path is IMD-limited, back off
DELAY_FLOOR_DB = -20.0

DEFAULT_OUT = "experiments/ofdm/results/measurements/bandwidth.json"


def tone_bins(period_samples, sample_rate):
    """The FFT bins of the probe grid, at whichever rate we are working in."""
    resolution = sample_rate / period_samples
    low = int(round(BAND[0] / resolution))
    high = int(round(BAND[1] / resolution))
    return np.arange(low, high + 1)


def probe_phases():
    """Schroeder phases, one per tone. See the module docstring on crest factor."""
    n = len(tone_bins(PERIOD_SAMPLES, afsk.SAMPLE_RATE))
    return np.pi * np.arange(n) ** 2 / n


def probe_period():
    """One cyclic period of the multitone comb, at the transmit rate."""
    bins = tone_bins(PERIOD_SAMPLES, afsk.SAMPLE_RATE)
    spectrum = np.zeros(PERIOD_SAMPLES // 2 + 1, dtype=complex)
    spectrum[bins] = np.exp(1j * probe_phases())
    period = np.fft.irfft(spectrum, PERIOD_SAMPLES)
    return (AMPLITUDE * period / np.max(np.abs(period))).astype(np.float32)


def reference_values():
    """The transmitted tone values: unit magnitude, Schroeder phase.

    Dividing the captured spectrum by these -- rather than by the spectrum of
    a locally resampled copy of the probe -- keeps the receive chain's own
    decimation inside the measured response, which is where it belongs: the
    passband a mode sees is the one after that filter, and the retired tool
    referenced against unit-magnitude training values for the same reason.
    """
    return np.exp(1j * probe_phases())


def transmit_audio(period):
    return np.tile(period, PERIODS)


def align(captured, reference_period):
    """Sample index of the first whole probe period in the capture."""
    if len(captured) < RX_PERIOD_SAMPLES * (BLOCK + 2):
        return None
    correlation = np.correlate(captured, reference_period, mode="valid")
    # Skip the first period: the receiver's squelch and AGC are still moving,
    # and a peak found inside that transient is not the frame start.
    guard = RX_PERIOD_SAMPLES
    if len(correlation) <= guard:
        return None
    return int(np.argmax(np.abs(correlation[guard:]))) + guard


SIGNAL_FLOOR_DB = -12.0


def channel_estimates(captured, start, reference_spectrum, bins):
    """Complex per-tone channel estimate for each whole period captured.

    Stops at the end of the transmission rather than at PERIODS. The capture
    keeps running past PTT-off, and a window that straddles the end holds part
    probe and part squelch tail; left in, it lands in the `end` block and
    reports the radio's release as if it were its passband.
    """
    windows = []
    for index in range(PERIODS):
        begin = start + index * RX_PERIOD_SAMPLES
        window = captured[begin:begin + RX_PERIOD_SAMPLES]
        if len(window) < RX_PERIOD_SAMPLES:
            break
        windows.append(window)
    if not windows:
        return np.asarray([])
    power = np.array([float(np.mean(window ** 2)) for window in windows])
    reference = float(np.median(power))
    keep = len(windows)
    while keep > 0 and 10 * np.log10(max(power[keep - 1], 1e-30) /
                                     max(reference, 1e-30)) < SIGNAL_FLOOR_DB:
        keep -= 1
    return np.asarray([np.fft.rfft(window)[bins] / reference_spectrum
                       for window in windows[:keep]])


def smooth(values, width=5):
    return np.convolve(values, np.ones(width) / width, mode="same")


def contiguous_band(freqs, mag_db, threshold):
    """Thresholded region containing the response peak, with interpolated edges.

    Kept identical to the retired tool's routine, so refreshed numbers are
    comparable to the committed ones rather than merely similar.
    """
    y = smooth(mag_db)
    peak = int(np.argmax(y))
    keep = y >= y[peak] + threshold
    lo = hi = peak
    while lo > 0 and keep[lo - 1]:
        lo -= 1
    while hi + 1 < len(keep) and keep[hi + 1]:
        hi += 1

    def cross(i0, i1):
        target = y[peak] + threshold
        if y[i1] == y[i0]:
            return float(freqs[i1])
        return float(freqs[i0] + (target - y[i0]) *
                     (freqs[i1] - freqs[i0]) / (y[i1] - y[i0]))

    low = float(freqs[0]) if lo == 0 else cross(lo - 1, lo)
    high = float(freqs[-1]) if hi == len(freqs) - 1 else cross(hi, hi + 1)
    return [low, high]


def delay_spread_ms(channel):
    """RMS delay spread of one averaged complex channel estimate.

    Invariant to bulk delay: the spread is taken about the profile's own
    centroid, so where the capture started does not enter it.
    """
    profile = np.abs(np.fft.ifft(channel)) ** 2
    # The profile is circular, and the path's bulk delay puts the peak at an
    # arbitrary tap. Centre it first: otherwise a peak near tap 0 has its own
    # skirt wrapped to the far end of the window and the spread comes out as
    # most of the window rather than as the channel.
    profile = np.roll(profile, len(profile) // 2 - int(np.argmax(profile)))
    power_db = 10 * np.log10(np.maximum(profile, 1e-30))
    keep = power_db >= power_db.max() + DELAY_FLOOR_DB
    if not keep.any():
        return None
    taps = np.arange(len(profile))
    weights = profile[keep]
    times = taps[keep] / (len(profile) * SPACING_HZ)
    centroid = np.sum(weights * times) / np.sum(weights)
    spread = np.sqrt(np.sum(weights * (times - centroid) ** 2) / np.sum(weights))
    return float(spread * 1000.0)


def summarize(trials, freqs):
    """Average power -- not complex H -- across separate keyings.

    Capture start jitter puts a linear phase ramp on H, so complex-averaging
    across keyings would manufacture high-frequency attenuation. Coherent
    averaging is only valid inside one keying, which is what each block does.
    """
    n_periods = trials.shape[1]
    windows = {
        "start": slice(0, BLOCK),
        "middle": slice(n_periods // 2 - BLOCK // 2, n_periods // 2 + BLOCK // 2),
        "end": slice(-BLOCK, None),
    }
    result = {}
    for name, selection in windows.items():
        per_trial_channel = np.mean(trials[:, selection, :], axis=1)
        power = np.mean(np.abs(per_trial_channel) ** 2, axis=0)
        mag = 10 * np.log10(np.maximum(power, 1e-30))
        mag -= np.max(mag)
        indices = np.arange(n_periods)[selection]
        result[name] = {
            "time_s": float(np.mean(indices) * PERIOD_SAMPLES / afsk.SAMPLE_RATE),
            "band_6db_hz": contiguous_band(freqs, mag, -6.0),
            "band_10db_hz": contiguous_band(freqs, mag, -10.0),
            "mag_db": mag.tolist(),
        }
    middle = np.mean(trials[:, windows["middle"], :], axis=1)
    spreads = [delay_spread_ms(channel) for channel in middle]
    spreads = [value for value in spreads if value is not None]
    # Median, not mean: the -20 dB floor means one trial with a noisy tone can
    # admit a tap far from the peak and multiply the spread, and averaging
    # keeps that while a median discards it.
    result["delay_spread_ms"] = float(np.median(spreads)) if spreads else None
    result["delay_spread_trials_ms"] = [round(value, 3) for value in spreads]
    result["freqs_hz"] = freqs.tolist()
    return result


def run_direction(tx, rx, label, trials, period, reference_spectrum, rx_bins,
                  rx_period):
    collected = []
    audio = transmit_audio(period)
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        keyed = tx.send(audio)
        time.sleep(CAPTURE_TAIL)
        captured = rx.snapshot_rx()
        start = align(captured, rx_period)
        if start is None:
            print(f"  {label} {trial}/{trials}: no probe found")
        else:
            estimates = channel_estimates(captured, start, reference_spectrum,
                                          rx_bins)
            # A capture never holds all PERIODS: it opens after PTT and the
            # first period is inside the squelch transient. Anything that
            # still spans the three windows is a usable trial.
            if len(estimates) < MIN_PERIODS:
                print(f"  {label} {trial}/{trials}: short capture, "
                      f"{len(estimates)}/{PERIODS} periods")
            else:
                collected.append(estimates)
                print(f"  {label} {trial}/{trials}: keyed={keyed:.2f}s, "
                      f"{len(estimates)} periods")
        time.sleep(INTER_TRIAL)
    if not collected:
        return None
    # Trials differ by a period or two; the windows have to mean the same
    # thing in each, so measure them all over the shortest one.
    common = min(len(estimates) for estimates in collected)
    collected = [estimates[:common] for estimates in collected]
    resolution = afsk.RX_SAMPLE_RATE / RX_PERIOD_SAMPLES
    freqs = rx_bins * resolution
    return summarize(np.asarray(collected), freqs)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=TRIALS)
    ap.add_argument("--a", default="ic705", help="station A radio name")
    ap.add_argument("--b", default="ht", help="station B radio name")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    period = probe_period()
    rx_bins = tone_bins(RX_PERIOD_SAMPLES, afsk.RX_SAMPLE_RATE)
    reference_spectrum = reference_values()
    # Used only to locate the probe in the capture, where a crude decimation
    # is good enough -- the response itself never goes through it.
    rx_period = period.reshape(-1, 4).mean(axis=1).astype(np.float32)
    crest = 20 * np.log10(np.max(np.abs(period)) / np.sqrt(np.mean(period ** 2)))
    print(f"{len(rx_bins)} tones, {BAND[0]:.0f}-{BAND[1]:.0f} Hz, "
          f"{PERIOD_SAMPLES / afsk.SAMPLE_RATE * 1000:.1f} ms/period, "
          f"{PERIODS} periods, crest {crest:.1f} dB")

    results = {}
    with bench.radio_pair(a=args.a, b=args.b) as (t_a, t_b):
        for tx, rx, label in ((t_a, t_b, f"{args.a}->{args.b}"),
                              (t_b, t_a, f"{args.b}->{args.a}")):
            print(f"\n{label}:")
            results[label] = run_direction(tx, rx, label, args.trials, period,
                                           reference_spectrum, rx_bins,
                                           rx_period)

    output = {
        "when": datetime.now(timezone.utc).isoformat(),
        "trials": args.trials,
        "block_symbols": BLOCK,
        "block_duration_ms": BLOCK * PERIOD_SAMPLES / afsk.SAMPLE_RATE * 1000,
        "directions": results,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))

    print("\n== BANDS ==")
    for direction, value in results.items():
        if not value:
            print(f"  {direction}: no measurement")
            continue
        for window in ("start", "middle", "end"):
            low6, high6 = value[window]["band_6db_hz"]
            low10, high10 = value[window]["band_10db_hz"]
            print(f"  {direction:14s} {window:6s} "
                  f"-6dB {low6:7.1f}-{high6:7.1f} Hz   "
                  f"-10dB {low10:7.1f}-{high10:7.1f} Hz")
        print(f"  {direction:14s} delay spread {value['delay_spread_ms']:.3f} ms "
              f"(trials {value['delay_spread_trials_ms']})")
    print(f"\nwrote {args.out}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
