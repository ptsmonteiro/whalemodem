"""Measure the standard HF bench audio SNR on IC-7300 -> IC-705.

The calibration signal is a deterministic equal-power multitone comb from
300 Hz through 2700 Hz in 60 Hz steps.  SNR is measured from the received
12 kHz audio by comparing power around those tones with inter-tone noise
bins, scaled to a 3 kHz reference bandwidth.

Run from the repository root::

    python scripts/measure_hf_bench_snr.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bench


TX_SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = 12_000
SIGNAL_LO_HZ = 300.0
SIGNAL_HI_HZ = 2700.0
TONE_SPACING_HZ = 60.0
SIGNAL_SECONDS = 4.0
SIGNAL_RMS = 0.60
REFERENCE_BANDWIDTH_HZ = 3000.0
# The two bench radios have shown about 8 Hz of common audio offset. These
# windows tolerate that offset while leaving a 30 Hz gap to the neighbouring
# calibration tones and keeping the inter-tone noise windows separate.
TONE_HALF_WIDTH_HZ = 15.0
NOISE_HALF_WIDTH_HZ = 10.0


def calibration_signal() -> np.ndarray:
    """Return the fixed 300--2700 Hz equal-power calibration waveform."""
    tones = np.arange(SIGNAL_LO_HZ, SIGNAL_HI_HZ + 0.1, TONE_SPACING_HZ)
    samples = np.arange(round(SIGNAL_SECONDS * TX_SAMPLE_RATE)) / TX_SAMPLE_RATE
    # Fixed phases make the signal reproducible while spreading its crest
    # factor instead of making all carriers peak together.
    phases = (np.arange(len(tones)) * 1.618033988749895) % (2 * np.pi)
    amplitude = SIGNAL_RMS * np.sqrt(2.0 / len(tones))
    signal = sum(np.cos(2 * np.pi * tone * samples + phase)
                 for tone, phase in zip(tones, phases))
    return (amplitude * signal).astype(np.float32)


def _band_power(freqs: np.ndarray, power: np.ndarray,
                centres: np.ndarray, half_width: float) -> np.ndarray:
    values = []
    for centre in centres:
        selected = np.abs(freqs - centre) <= half_width
        values.append(float(np.sum(power[selected])))
    return np.asarray(values)


def measure_snr(audio: np.ndarray) -> dict:
    """Measure received calibration SNR in a 3 kHz reference bandwidth."""
    samples = np.asarray(audio, dtype=np.float64)
    if samples.ndim != 1 or len(samples) < RX_SAMPLE_RATE:
        raise ValueError(f"capture too short ({len(samples)} samples)")

    # Discard receiver/transmitter settling at both ends, then use a window
    # whose FFT bins are exact multiples of the 1 Hz tone grid.
    samples = samples[int(0.5 * RX_SAMPLE_RATE):]
    samples = samples[:-int(0.5 * RX_SAMPLE_RATE)]
    n = min(len(samples), int(3 * RX_SAMPLE_RATE))
    samples = samples[:n]
    window = np.hanning(len(samples))
    spectrum = np.fft.rfft(samples * window)
    freqs = np.fft.rfftfreq(len(samples), 1.0 / RX_SAMPLE_RATE)
    power = np.abs(spectrum) ** 2

    tones = np.arange(SIGNAL_LO_HZ, SIGNAL_HI_HZ + 0.1, TONE_SPACING_HZ)
    noise_centres = tones[:-1] + TONE_SPACING_HZ / 2.0
    signal_power = float(np.sum(_band_power(
        freqs, power, tones, TONE_HALF_WIDTH_HZ)))
    noise_band_power = _band_power(
        freqs, power, noise_centres, NOISE_HALF_WIDTH_HZ)
    noise_psd = float(np.median(noise_band_power / (2 * NOISE_HALF_WIDTH_HZ)))
    snr_db = 10.0 * np.log10(signal_power /
                              (noise_psd * REFERENCE_BANDWIDTH_HZ))
    return {
        "snr_3khz_db": float(snr_db),
        "signal_power": signal_power,
        "noise_psd": noise_psd,
        "capture_rms": float(np.sqrt(np.mean(samples ** 2))),
        "capture_peak": float(np.max(np.abs(samples))),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=ROOT / "logs" / "mode_qualification" /
                        "hf-ssb" / "bench-snr-20260908.json")
    args = parser.parse_args(argv)

    signal = calibration_signal()
    print("HF bench calibration: IC-7300 -> IC-705")
    print(f"signal: {SIGNAL_LO_HZ:.0f}-{SIGNAL_HI_HZ:.0f} Hz, "
          f"{TONE_SPACING_HZ:.0f} Hz comb, {SIGNAL_SECONDS:.1f} s, "
          f"RMS={SIGNAL_RMS:.3f}")
    with bench.radio_pair("ic7300", "ic705", warmup=3.0,
                          b_receive_only=True) as (tx, rx):
        rx.consume_rx(len(rx.snapshot_rx()))
        keyed = tx.send(signal)
        time.sleep(1.0)
        captured = rx.snapshot_rx()

    measurement = measure_snr(captured)
    result = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "direction": "IC-7300->IC-705",
        "signal": {
            "type": "deterministic_equal_power_multitone",
            "low_hz": SIGNAL_LO_HZ,
            "high_hz": SIGNAL_HI_HZ,
            "spacing_hz": TONE_SPACING_HZ,
            "duration_s": SIGNAL_SECONDS,
            "tx_rms": SIGNAL_RMS,
            "reference_bandwidth_hz": REFERENCE_BANDWIDTH_HZ,
        },
        "keyed_seconds": keyed,
        "rx_samples": len(captured),
        **measurement,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    np.save(args.out.with_suffix(".npy"), captured)
    print(f"SNR/3 kHz: {measurement['snr_3khz_db']:.2f} dB")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
