"""Measures per-direction SNR on the real two-radio bench.

Both radios run with squelch active, so there is no such thing as a "quiet
before TX" capture to use as a noise reference -- squelch-closed audio is
just the receiver muted, not the channel noise floor. The only time the
channel's actual noise is visible in the captured audio is while squelch is
open, i.e. during reception of the far end's transmission.

So SNR here is measured in a single window (the received tone burst, from a
squelch-open reception) by comparing:
  - "signal" power: RMS in the AFSK tone band (500-1600 Hz), dominated by the
    700/1300 Hz CPFSK tones.
  - "noise" power: RMS in two side bands just outside the tone band
    (300-550 Hz and 1750-2050 Hz, chosen to dodge the 2100 Hz 3rd harmonic of
    the 700 Hz tone), power-spectral-density-normalized and scaled up to the
    tone band's bandwidth, as an estimate of what the noise floor contributes
    inside the tone band.

SNR_dB = 10*log10(signal_power / estimated_noise_power_in_band)

This is *not* the same number VARA's status line calls "SNR" -- there's no
shared reference implementation here -- but it is a real, reproducible
in-band-signal-over-estimated-in-band-noise measurement made from audio
captured live off each radio's receiver while squelch is open.

Run: python scripts/measure_snr.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from scipy.signal import butter, sosfiltfilt

from whale import afsk
from whale.transport import RadioTransport, SAMPLE_RATE

TONE_PAYLOAD = b"SNRTEST " * 24  # 192 bytes -> several seconds of AFSK at 300 baud
SIGNAL_BAND = (500.0, 1600.0)
NOISE_BANDS = [(300.0, 550.0), (1750.0, 2050.0)]
SETTLE_AFTER_PTT = 0.5  # let PTT/AGC/squelch settle before trusting captured audio
TAIL_TRIM = 0.2  # drop the last bit too, in case squelch starts closing before audio ends


def _band_rms(audio, sample_rate, lo, hi):
    if len(audio) < 50:
        return 0.0
    sos = butter(4, [lo, hi], btype="bandpass", fs=sample_rate, output="sos")
    filtered = sosfiltfilt(sos, audio)
    return float(np.sqrt(np.mean(filtered ** 2)))


def _estimate_snr_db(audio, sample_rate):
    lo, hi = SIGNAL_BAND
    signal_rms = _band_rms(audio, sample_rate, lo, hi)
    signal_power = signal_rms ** 2
    signal_bw = hi - lo

    noise_psd_estimates = []
    for nlo, nhi in NOISE_BANDS:
        rms = _band_rms(audio, sample_rate, nlo, nhi)
        noise_psd_estimates.append((rms ** 2) / (nhi - nlo))
    noise_psd = float(np.mean(noise_psd_estimates))
    noise_power_in_band = noise_psd * signal_bw

    if signal_power <= 0 or noise_power_in_band <= 0:
        return None, signal_rms, noise_power_in_band
    snr_db = 10 * np.log10(signal_power / noise_power_in_band)
    return snr_db, signal_rms, noise_power_in_band


def _measure_direction(tx: RadioTransport, rx: RadioTransport, tx_name, rx_name):
    print(f"\n=== {tx_name} -> {rx_name} ===")
    print(f"  {tx_name} keying up, sending test tone...")
    tx_audio = afsk.modulate(TONE_PAYLOAD)
    rx.snapshot_rx()  # discard anything captured while we were setting up

    # tx.send() blocks until PTT is un-keyed; rx keeps recording throughout
    # (RadioTransport's RX stream is always-on, see transport.py). Since
    # tx.send() clears *its own* buffer around the TX, and rx is a separate
    # RadioTransport instance, this does not disturb what rx is capturing.
    tx.send(tx_audio)

    captured = rx.snapshot_rx()
    settle_samples = int(SETTLE_AFTER_PTT * SAMPLE_RATE)
    tail_samples = int(TAIL_TRIM * SAMPLE_RATE)
    core = captured[settle_samples:max(settle_samples, len(captured) - tail_samples)]

    if len(core) < SAMPLE_RATE * 0.5:
        print(f"  only captured {len(core)} samples after trimming -- "
              f"squelch may not have opened at {rx_name} (check PTT/audio routing)")
        return None

    snr_db, signal_rms, noise_power_in_band = _estimate_snr_db(core, SAMPLE_RATE)
    print(f"  signal RMS (500-1600 Hz): {signal_rms:.6f}")
    print(f"  estimated noise power in tone band: {noise_power_in_band:.3e}")
    if snr_db is None:
        print(f"  could not compute SNR (zero signal or noise) for {tx_name} -> {rx_name}")
        return None
    print(f"  SNR {tx_name} -> {rx_name}: {snr_db:.1f} dB")
    return snr_db


def main():
    print("opening radios...")
    t1 = RadioTransport("ic705")
    t2 = RadioTransport("ht")
    try:
        t1.start_receiving()
        t2.start_receiving()
        print("warming up 2s...")
        time.sleep(2)

        snr_ab = _measure_direction(t1, t2, "STA1", "STA2")
        time.sleep(1.0)
        snr_ba = _measure_direction(t2, t1, "STA2", "STA1")

        print("\n=== SUMMARY ===")
        print(f"STA1 -> STA2: {snr_ab:.1f} dB" if snr_ab is not None else "STA1 -> STA2: N/A")
        print(f"STA2 -> STA1: {snr_ba:.1f} dB" if snr_ba is not None else "STA2 -> STA1: N/A")
    finally:
        t1.close()
        t2.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
