"""Measure the radio pair's audio passband as a function of keying age.

Unlike probe_channel.py, this keeps every repeated training-symbol estimate.
It reports contiguous -6 and -10 dB passbands for short blocks at the start,
middle, and end of a roughly three-second transmission, in both directions.

This keys both radios.  Run from the repository root:
    python experiments/ofdm/measure_audio_bandwidth.py
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ofdm
import probe_channel
from whale.transport import RadioTransport

N_TRAIN = 60
BAND = (120.0, 3900.0)
BLOCK = 4                       # 150 ms at this probe geometry
TRIALS = 3
CAPTURE_TAIL = 0.8


def profile():
    return ofdm.OfdmProfile(name="time_band_probe", n_fft=1600, cp=200,
                            bits_per_carrier=2, band_low=BAND[0],
                            band_high=BAND[1], n_train=N_TRAIN)


def smooth(values, width=5):
    return np.convolve(values, np.ones(width) / width, mode="same")


def contiguous_band(freqs, mag_db, threshold):
    """Thresholded region containing the response peak, with interpolated edges."""
    y = smooth(mag_db)
    peak = int(np.argmax(y))
    keep = y >= y[peak] + threshold
    lo = peak
    hi = peak
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


def summarize(trials, p):
    """Average power, not complex H, across trials.

    Capture start jitter adds a linear phase ramp to H.  Complex-averaging
    separate keyings would therefore manufacture high-frequency attenuation;
    coherent averaging is only valid inside each short block of one keying.
    """
    freqs = p.carriers * p.spacing
    n_symbols = trials.shape[1]
    windows = {
        "start": slice(0, BLOCK),
        "middle": slice(n_symbols//2-BLOCK//2, n_symbols//2+BLOCK//2),
        "end": slice(-BLOCK, None),
    }
    result = {}
    for name, selection in windows.items():
        per_trial_channel = np.mean(trials[:, selection, :], axis=1)
        power = np.mean(np.abs(per_trial_channel) ** 2, axis=0)
        mag = 10 * np.log10(np.maximum(power, 1e-30))
        mag -= np.max(mag)
        indices = np.arange(n_symbols)[selection]
        result[name] = {
            "time_s": float(np.mean(indices) * p.symbol_samples / ofdm.SAMPLE_RATE),
            "band_6db_hz": contiguous_band(freqs, mag, -6.0),
            "band_10db_hz": contiguous_band(freqs, mag, -10.0),
            "mag_db": mag.tolist(),
        }
    result["freqs_hz"] = freqs.tolist()
    return result


def run_direction(tx, rx, p, label, trials):
    all_estimates = []
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        keyed = tx.send(ofdm.modulate(b"", p))
        time.sleep(CAPTURE_TAIL)
        measured = probe_channel.measure(rx.snapshot_rx(), p)
        if measured is None:
            print(f"  {label} {trial}/{trials}: no sync")
        else:
            # Recreate the individual H estimates retained internally by measure().
            audio = rx.snapshot_rx()
            proposals, _ = ofdm._propose(audio, p)
            analytic = probe_channel.hilbert(audio)
            start = max((ofdm._refine(analytic, p, q) for q in proposals),
                        key=lambda item: item[1])[0]
            first = start + p.symbol_samples
            guard = int(ofdm._WINDOW_GUARD_FRACTION * p.cp)
            estimates = []
            train = ofdm.training_values(p)
            for i in range(p.n_train):
                w0 = first + i * p.symbol_samples - guard
                window = audio[w0:w0 + p.n_fft]
                estimates.append(np.fft.rfft(window)[p.carriers] * np.conj(train))
            all_estimates.append(np.asarray(estimates))
            print(f"  {label} {trial}/{trials}: keyed={keyed:.2f}s, "
                  f"conf={measured['confidence']:.3f}")
        time.sleep(0.6)
    if not all_estimates:
        return None
    return summarize(np.asarray(all_estimates), p)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=TRIALS)
    ap.add_argument("--out",
                    default="experiments/ofdm/results/measurements/bandwidth.json")
    args = ap.parse_args()
    p = profile()
    print(f"{p.n_carriers} tones, {p.tone_low:.0f}-{p.tone_high:.0f} Hz, "
          f"{p.symbol_samples/ofdm.SAMPLE_RATE*1000:.1f} ms/symbol")
    radios = (RadioTransport("ic705"), RadioTransport("ht"))
    results = {}
    try:
        for radio in radios:
            radio.start_receiving()
        time.sleep(2)
        for tx, rx, label in ((radios[0], radios[1], "ic705->ht"),
                              (radios[1], radios[0], "ht->ic705")):
            results[label] = run_direction(tx, rx, p, label, args.trials)
    finally:
        for radio in radios:
            radio.close()
    output = {"when": datetime.now(timezone.utc).isoformat(),
              "trials": args.trials, "block_symbols": BLOCK,
              "block_duration_ms": BLOCK*p.symbol_samples/ofdm.SAMPLE_RATE*1000,
              "directions": results}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(json.dumps({d: {w: ({"-6dB": x["band_6db_hz"],
                              "-10dB": x["band_10db_hz"]})
                           for w, x in v.items() if w != "freqs_hz"}
                      for d, v in results.items() if v}, indent=2))
    print(f"wrote {args.out}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
