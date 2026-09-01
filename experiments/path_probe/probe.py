"""Raw channel characterization: IC-7300 -> IC-705, 300-2700 Hz.

No mode, no framing, no FEC -- just measure what the real path actually
does to a multitone probe before designing anything. Sends a deterministic
sum-of-sinusoids (Newman phases, low PAPR) covering the whole legal band,
captures it on the other radio, and reports per-tone amplitude/phase/SNR
plus the noise floor with the transmitter silent.

Run (from the repository root):
    python experiments/path_probe/probe.py --a ic7300 --b ic705 --trials 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import numpy as np
from scipy.signal import resample_poly

import bench

TX_SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = 12_000
DURATION_S = 2.0
TONE_SPACING_HZ = 50.0
BAND_LO_HZ = 300.0
BAND_HI_HZ = 2700.0
EDGE_TAPER_S = 0.05


def tone_freqs():
    n_lo = int(np.ceil(BAND_LO_HZ / TONE_SPACING_HZ))
    n_hi = int(np.floor(BAND_HI_HZ / TONE_SPACING_HZ))
    return np.arange(n_lo, n_hi + 1) * TONE_SPACING_HZ


def build_probe():
    freqs = tone_freqs()
    n = len(freqs)
    t = np.arange(int(DURATION_S * TX_SAMPLE_RATE)) / TX_SAMPLE_RATE
    # Newman phase schedule: keeps crest factor low for a sum of many tones.
    k = np.arange(n)
    phases = np.pi * k * k / n
    signal = np.zeros_like(t)
    for f, ph in zip(freqs, phases):
        signal += np.cos(2 * np.pi * f * t + ph)
    signal /= np.max(np.abs(signal))
    signal *= 0.7  # headroom below full scale / ALC threshold

    taper_n = int(EDGE_TAPER_S * TX_SAMPLE_RATE)
    window = np.ones_like(signal)
    ramp = 0.5 * (1 - np.cos(np.linspace(0, np.pi, taper_n)))
    window[:taper_n] = ramp
    window[-taper_n:] = ramp[::-1]
    signal *= window
    return signal.astype(np.float32), freqs, phases


def align(captured_12k, reference_12k):
    """Find burst onset by RMS envelope, not waveform correlation.

    A sum-of-many-tones waveform's *shape* is highly sensitive to the
    relative phase of each tone. This channel applies different phase
    shifts to different tones (that's the frequency-selective effect this
    probe exists to measure), so the received composite waveform can look
    almost nothing like the transmitted one even though every tone's
    energy survives intact. Time-domain cross-correlation against the
    known reference is fooled by that and reports near-zero match even on
    a clean capture -- use signal power instead, which phase can't hide.
    """
    win = max(1, int(0.02 * RX_SAMPLE_RATE))
    power = np.convolve(captured_12k ** 2, np.ones(win) / win, mode="valid")
    if power.size == 0:
        return None, 0.0
    noise_floor = np.median(power[: max(1, win)])
    threshold = max(noise_floor * 8, power.max() * 0.1)
    above = np.where(power > threshold)[0]
    if above.size == 0:
        return None, 0.0
    onset = int(above[0])
    quality = float(power[onset:onset + win].mean() / (noise_floor + 1e-12))
    return onset, quality


def analyze(captured_12k, freqs, start):
    seg = captured_12k[start:start + int(DURATION_S * RX_SAMPLE_RATE)]
    trim = int(0.1 * RX_SAMPLE_RATE)
    seg = seg[trim:-trim]
    n = len(seg)
    spec = np.fft.rfft(seg * np.hanning(n))
    bin_hz = RX_SAMPLE_RATE / n
    mags = np.abs(spec)

    results = []
    for f in freqs:
        idx = int(round(f / bin_hz))
        sig = mags[idx]
        noise_idx = [j for j in range(idx - 6, idx + 7)
                     if j != idx and 0 <= j < len(mags)]
        noise = np.median(mags[noise_idx]) if noise_idx else 1e-12
        snr_db = 20 * np.log10((sig + 1e-12) / (noise + 1e-12))
        results.append({"freq_hz": float(f), "mag": float(sig),
                         "noise_mag": float(noise), "snr_db": float(snr_db)})
    return results


def run_trial(tx, rx, probe_tx, reference_12k, freqs, capture_tail):
    stale = rx.snapshot_rx()
    rx.consume_rx(len(stale))
    keyed = tx.send(probe_tx)
    time.sleep(capture_tail)
    captured = rx.snapshot_rx()
    start, quality = align(captured, reference_12k)
    if start is None:
        return {"keyed_seconds": keyed, "aligned": False}
    tones = analyze(captured, freqs, start)
    snrs = np.array([t["snr_db"] for t in tones])
    return {
        "keyed_seconds": keyed, "aligned": True, "corr_score": quality,
        "start_sample": start, "tones": tones,
        "snr_min": float(snrs.min()), "snr_median": float(np.median(snrs)),
        "snr_max": float(snrs.max()),
        "n_below_6db": int((snrs < 6).sum()),
        "n_below_10db": int((snrs < 10).sum()),
    }


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--capture-tail", type=float, default=1.0)
    ap.add_argument("--output-dir", type=Path)
    args = ap.parse_args(argv)

    probe_tx, freqs, phases = build_probe()
    reference_12k = resample_poly(probe_tx, 1, TX_SAMPLE_RATE // RX_SAMPLE_RATE)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "path_probe" / stamp
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"path probe: {len(freqs)} tones, {BAND_LO_HZ:.0f}-{BAND_HI_HZ:.0f} Hz, "
          f"{DURATION_S:.1f}s @ {TX_SAMPLE_RATE} Hz TX")

    records = []
    with pair_factory(args.a, args.b, warmup=3.0) as (transport_a, transport_b):
        for i in range(args.trials):
            rec = run_trial(transport_a, transport_b, probe_tx, reference_12k,
                            freqs, args.capture_tail)
            records.append(rec)
            if rec["aligned"]:
                print(f"  trial {i+1}/{args.trials}: corr={rec['corr_score']:.3f} "
                      f"snr min/med/max={rec['snr_min']:.1f}/"
                      f"{rec['snr_median']:.1f}/{rec['snr_max']:.1f} dB "
                      f"below6db={rec['n_below_6db']} below10db={rec['n_below_10db']}")
            else:
                print(f"  trial {i+1}/{args.trials}: ALIGNMENT FAILED")
            time.sleep(0.5)

        print("\n  measuring noise floor (tx silent)...")
        stale = transport_b.snapshot_rx()
        transport_b.consume_rx(len(stale))
        time.sleep(2.0)
        noise_capture = transport_b.snapshot_rx()
        noise_spec = np.abs(np.fft.rfft(noise_capture * np.hanning(len(noise_capture))))
        bin_hz = RX_SAMPLE_RATE / len(noise_capture)
        noise_tone_mags = []
        for f in freqs:
            idx = int(round(f / bin_hz))
            if idx < len(noise_spec):
                noise_tone_mags.append(float(noise_spec[idx]))
        print(f"  silent-channel median bin magnitude: {np.median(noise_tone_mags):.4f} "
              f"(vs tx-on tone mags around {np.median([t['mag'] for r in records if r['aligned'] for t in r['tones']]):.4f})")

    out = {
        "freqs_hz": freqs.tolist(),
        "trials": records,
        "channel": {"radio_a": args.a, "radio_b": args.b},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"\nwrote {result_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
