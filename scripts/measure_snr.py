"""Measures per-direction SNR on the real two-radio bench.

Both radios run with squelch active, so there is no such thing as a "quiet
before TX" capture to use as a noise reference -- squelch-closed audio is
just the receiver muted, not the channel noise floor. The only time the
channel's actual noise is visible in the captured audio is while squelch is
open, i.e. during reception of the far end's transmission.

So SNR here is measured in a single window (the received tone burst, from a
squelch-open reception) by comparing:
  - "signal" power: RMS in the AFSK tone band, i.e. the profile's two tones
    plus BAND_MARGIN either side (1000-2000 Hz for PROFILE_300's 1200/1800 Hz
    CPFSK tones).
  - "noise" power: RMS in two side bands just outside the tone band
    (700-950 Hz and 2050-2300 Hz for that same profile),
    power-spectral-density-normalized and scaled to the standard 3 kHz
    reference bandwidth. _bands_for_profile flags a noise band landing on a tone's 3rd
    harmonic; none of the current profiles' do, since every profile is
    centred on 1500 Hz and their harmonics all sit above 2900 Hz.

SNR/3kHz_dB = 10*log10(signal_power / (estimated_noise_PSD * 3000 Hz))

This is *not* the same number VARA's status line calls "SNR" -- there's no
shared reference implementation here -- but it is a real, reproducible
in-band-signal-over-estimated-in-band-noise measurement made from audio
captured live off each radio's receiver while squelch is open.

Run: python scripts/measure_snr.py
       python scripts/measure_snr.py --profile 600baud
"""
import argparse
import sys
import time

import bench

import numpy as np
from scipy.signal import butter, sosfiltfilt

from whale import afsk
from whale.transport import RadioTransport, SAMPLE_RATE

PROFILES_BY_NAME = {p.name: p for p in afsk.PROFILES}
TONE_PAYLOAD = b"SNRTEST " * 24  # 192 bytes -> several seconds of AFSK at 300 baud
SETTLE_AFTER_PTT = 0.5  # let PTT/AGC/squelch settle before trusting captured audio
TAIL_TRIM = 0.2  # drop the last bit too, in case squelch starts closing before audio ends
BAND_MARGIN = 200.0  # Hz of headroom either side of the tone pair for the signal band
NOISE_GUARD = 50.0    # Hz gap left between the signal band and each noise band
NOISE_WIDTH = 250.0   # Hz width of each noise band
SNR_REFERENCE_BANDWIDTH_HZ = 3_000.0


def _bands_for_profile(profile):
    """Signal band spans the two CPFSK tones (plus margin); noise bands sit
    just outside it on each side. Flags (doesn't avoid) a noise band that
    lands near either tone's 3rd harmonic, since that skewed PROFILE_300's
    original band choice back when its lower tone was at 700 Hz."""
    lo_tone, hi_tone = sorted((profile.freq0, profile.freq1))
    signal_band = (lo_tone - BAND_MARGIN, hi_tone + BAND_MARGIN)
    noise_bands = [
        (signal_band[0] - NOISE_GUARD - NOISE_WIDTH, signal_band[0] - NOISE_GUARD),
        (signal_band[1] + NOISE_GUARD, signal_band[1] + NOISE_GUARD + NOISE_WIDTH),
    ]
    for harmonic in (3 * profile.freq0, 3 * profile.freq1):
        for nlo, nhi in noise_bands:
            if nlo <= harmonic <= nhi:
                print(f"  WARNING: noise band {nlo:.0f}-{nhi:.0f} Hz overlaps a tone's 3rd "
                      f"harmonic ({harmonic:.0f} Hz) -- noise estimate may read high; "
                      f"adjust NOISE_GUARD/NOISE_WIDTH if SNR looks suspiciously low")
    return signal_band, noise_bands


def _band_rms(audio, sample_rate, lo, hi):
    if len(audio) < 50:
        return 0.0
    sos = butter(4, [lo, hi], btype="bandpass", fs=sample_rate, output="sos")
    filtered = sosfiltfilt(sos, audio)
    return float(np.sqrt(np.mean(filtered ** 2)))


def _estimate_snr_db(audio, sample_rate, signal_band, noise_bands):
    lo, hi = signal_band
    signal_rms = _band_rms(audio, sample_rate, lo, hi)
    signal_power = signal_rms ** 2
    noise_psd_estimates = []
    for nlo, nhi in noise_bands:
        rms = _band_rms(audio, sample_rate, nlo, nhi)
        noise_psd_estimates.append((rms ** 2) / (nhi - nlo))
    noise_psd = float(np.mean(noise_psd_estimates))
    noise_power_in_band = noise_psd * SNR_REFERENCE_BANDWIDTH_HZ

    if signal_power <= 0 or noise_power_in_band <= 0:
        return None, signal_rms, noise_power_in_band
    snr_db = 10 * np.log10(signal_power / noise_power_in_band)
    return snr_db, signal_rms, noise_power_in_band


def _measure_direction(tx: RadioTransport, rx: RadioTransport, tx_name, rx_name, profile):
    signal_band, noise_bands = _bands_for_profile(profile)
    print(f"\n=== {tx_name} -> {rx_name} ({profile.name}) ===")
    print(f"  {tx_name} keying up, sending test tone...")
    tx_audio = afsk.modulate(TONE_PAYLOAD, profile=profile)
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

    snr_db, signal_rms, noise_power_in_band = _estimate_snr_db(core, SAMPLE_RATE, signal_band, noise_bands)
    print(f"  signal RMS ({signal_band[0]:.0f}-{signal_band[1]:.0f} Hz): {signal_rms:.6f}")
    print(f"  estimated noise power in 3 kHz: {noise_power_in_band:.3e}")
    if snr_db is None:
        print(f"  could not compute SNR (zero signal or noise) for {tx_name} -> {rx_name}")
        return None
    print(f"  SNR/3 kHz {tx_name} -> {rx_name}: {snr_db:.1f} dB (confidence_threshold={profile.confidence_threshold})")
    return snr_db


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default=afsk.PROFILE_300.name, choices=sorted(PROFILES_BY_NAME),
                     help="afsk.Profile to transmit the test tone at (default: %(default)s)")
    args = ap.parse_args()
    profile = PROFILES_BY_NAME[args.profile]

    print(f"profile={profile.name}, tones={profile.freq0:.0f}/{profile.freq1:.0f} Hz, "
          f"baud={profile.baud}")
    with bench.radio_pair() as (t1, t2):
        snr_ab = _measure_direction(t1, t2, "STA1", "STA2", profile)
        time.sleep(1.0)
        snr_ba = _measure_direction(t2, t1, "STA2", "STA1", profile)

        print("\n=== SUMMARY ===")
        print(f"STA1 -> STA2: {snr_ab:.1f} dB" if snr_ab is not None else "STA1 -> STA2: N/A")
        print(f"STA2 -> STA1: {snr_ba:.1f} dB" if snr_ba is not None else "STA2 -> STA1: N/A")


if __name__ == "__main__":
    sys.exit(main() or 0)
