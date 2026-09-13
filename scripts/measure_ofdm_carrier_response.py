"""Measures how a multi-tone ("OFDM-like") probe signal's individual carriers
arrive on each leg of the real two-radio bench.

Builds one probe: 80 equal-amplitude sinusoids at 50, 100, 150, ... 4000 Hz
(50 Hz spacing, matching an OFDM subcarrier grid), each with a random,
fixed-seed phase so the sum doesn't crest-factor into a single huge spike.
Sends it whole -- no framing, no FEC, this is not a waveform under test, it
is a swept-in-parallel probe of the audio path.

Both radios' receive chain decimates 48 kHz capture down to 12 kHz before
snapshot_rx() ever sees it (whale/rx_audio.py) -- a real part of the path,
not an artifact -- so the receive-side FFT here runs at RX_SAMPLE_RATE
(12 kHz) with 1-second blocks, giving exactly 1 Hz bins: the k-th rfft bin is
k Hz, so every 50 Hz carrier lands on a bin with no interpolation. Multiple
1-second blocks are averaged to beat down channel/receiver noise before
reading each carrier's power off its bin.

Reports each carrier's received power in dB, relative to the mean across all
carriers -- i.e. the shape of the path's frequency response, not its
absolute gain, since absolute gain is set by AGC/volume knobs that have
nothing to do with what's being measured here. Also estimates a per-carrier
SNR by comparing each carrier's bin against its own local noise floor (the
average of the two neighboring off-grid bins, e.g. +/-25 Hz), so a low
relative-power carrier that's still comfortably above the noise floor reads
differently from one that's actually gone.

Run: python scripts/measure_ofdm_carrier_response.py
     python scripts/measure_ofdm_carrier_response.py --seconds 8
"""
import argparse
import csv
import sys
import time

import numpy as np

import bench
from whale.transport import RX_SAMPLE_RATE, SAMPLE_RATE

CARRIER_LO_HZ = 50.0
CARRIER_HI_HZ = 4000.0
CARRIER_STEP_HZ = 50.0

TONE_SECONDS = 5.0          # length of the multitone burst itself
SETTLE_AFTER_PTT = 0.5      # matches measure_snr.py -- let PTT/AGC/squelch settle
TAIL_TRIM = 0.2
PEAK_TARGET = 0.8           # normalize the multitone sum to this peak amplitude
PHASE_SEED = 0x0FDA         # fixed so repeated runs probe the identical waveform

BLOCK_SAMPLES = RX_SAMPLE_RATE  # 1 second at 12 kHz -> exactly 1 Hz FFT bins
NOISE_OFFSET_HZ = 25.0         # off-grid neighbors used as each carrier's noise reference


def carrier_freqs():
    return np.arange(CARRIER_LO_HZ, CARRIER_HI_HZ + 1e-6, CARRIER_STEP_HZ)


def build_probe(freqs, seconds=TONE_SECONDS, seed=PHASE_SEED, peak=PEAK_TARGET):
    n = int(round(seconds * SAMPLE_RATE))
    t = np.arange(n) / SAMPLE_RATE
    rng = np.random.default_rng(seed)
    phases = rng.uniform(0, 2 * np.pi, size=len(freqs))
    tone = np.zeros(n, dtype=np.float64)
    for f, phase in zip(freqs, phases):
        tone += np.sin(2 * np.pi * f * t + phase)
    tone *= peak / np.max(np.abs(tone))
    return tone.astype(np.float32)


def block_power_spectrum(core, block_samples=BLOCK_SAMPLES):
    """Averages |rfft|^2 / N over as many non-overlapping 1s blocks as fit.

    Deliberately unwindowed: every carrier sits on an exact integer number
    of cycles within a 1s/12kHz block, so a rectangular window has zero
    spectral leakage for them and gives the cleanest possible isolation from
    the off-grid noise-reference bins. A Hann/Hamming window would only cost
    SNR here, not buy anything.

    Returns (freqs_hz, power) with 1 Hz bins, or (None, None) if core is
    shorter than one block.
    """
    n_blocks = len(core) // block_samples
    if n_blocks == 0:
        return None, None
    acc = None
    for i in range(n_blocks):
        block = core[i * block_samples:(i + 1) * block_samples]
        spectrum = np.fft.rfft(block)
        power = (np.abs(spectrum) ** 2) / block_samples
        acc = power if acc is None else acc + power
    acc /= n_blocks
    freqs = np.fft.rfftfreq(block_samples, d=1.0 / RX_SAMPLE_RATE)
    return freqs, acc


def carrier_report(freqs_hz, power, carriers):
    """For each carrier, reads its own bin plus the two off-grid neighbor
    bins (+/- NOISE_OFFSET_HZ) as a local noise reference. Since freqs_hz has
    1 Hz bins, bin index == round(frequency)."""
    rows = []
    for f in carriers:
        sig_bin = int(round(f))
        lo_bin = int(round(f - NOISE_OFFSET_HZ))
        hi_bin = int(round(f + NOISE_OFFSET_HZ))
        sig_power = power[sig_bin]
        noise_power = float(np.mean([power[lo_bin], power[hi_bin]]))
        rows.append((f, sig_power, noise_power))
    return rows


def run_direction(tx, rx, tx_name, rx_name, probe, carriers):
    print(f"\n== {tx_name} -> {rx_name} ==")
    rx.snapshot_rx()  # drop anything stale before we start
    lead_pad = bench.noise_pad(seconds=1.0)
    tail_pad = bench.noise_pad(seconds=1.0)
    tx_audio = np.concatenate([lead_pad, probe, tail_pad])

    keyed = tx.send(tx_audio)
    print(f"   keyed {keyed:.2f}s, probe is {len(probe) / SAMPLE_RATE:.2f}s of that")

    time.sleep(TAIL_TRIM + 0.5)
    captured = rx.snapshot_rx()

    settle_samples = int((1.0 + SETTLE_AFTER_PTT) * RX_SAMPLE_RATE)  # skip lead pad + settle
    tail_samples = int((1.0 + TAIL_TRIM) * RX_SAMPLE_RATE)  # skip tail pad + trim
    core = captured[settle_samples:max(settle_samples, len(captured) - tail_samples)]
    print(f"   captured {len(captured)} samples @ {RX_SAMPLE_RATE} Hz, "
          f"using {len(core)} after settle/tail trim")

    freqs_hz, power = block_power_spectrum(core)
    if freqs_hz is None:
        print(f"   not enough captured audio for even one {BLOCK_SAMPLES / RX_SAMPLE_RATE:.1f}s "
              f"analysis block -- squelch may not have opened on {rx_name}")
        return None

    rows = carrier_report(freqs_hz, power, carriers)
    sig_powers = np.array([r[1] for r in rows])
    mean_sig_db = 10 * np.log10(np.mean(sig_powers) + 1e-30)

    print(f"   {'freq_hz':>8} {'rel_db':>8} {'snr_db':>8}")
    out_rows = []
    for f, sig_power, noise_power in rows:
        rel_db = 10 * np.log10(sig_power + 1e-30) - mean_sig_db
        snr_db = 10 * np.log10((sig_power + 1e-30) / (noise_power + 1e-30))
        out_rows.append((f, rel_db, snr_db))
        print(f"   {f:8.0f} {rel_db:8.1f} {snr_db:8.1f}")
    return out_rows


def save_csv(path, rows, tx_name, rx_name):
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["direction", "freq_hz", "rel_power_db", "snr_db"])
        for f, rel_db, snr_db in rows:
            writer.writerow([f"{tx_name}->{rx_name}", f, f"{rel_db:.3f}", f"{snr_db:.3f}"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=TONE_SECONDS,
                     help="length of the multitone burst (default: %(default)s)")
    ap.add_argument("--out-prefix", default="scratch_ofdm_carrier_response",
                     help="prefix for the two output CSVs (default: %(default)s)")
    ap.add_argument("--direction", choices=["both", "ic705->ht", "ht->ic705"], default="both",
                     help="which leg(s) to probe (default: %(default)s)")
    args = ap.parse_args()

    carriers = carrier_freqs()
    print(f"{len(carriers)} carriers, {CARRIER_LO_HZ:.0f}-{CARRIER_HI_HZ:.0f} Hz "
          f"step {CARRIER_STEP_HZ:.0f} Hz, probe length {args.seconds:.1f}s")
    probe = build_probe(carriers, seconds=args.seconds)

    do_ab = args.direction in ("both", "ic705->ht")
    do_ba = args.direction in ("both", "ht->ic705")
    rows_ab = rows_ba = None
    written = []

    with bench.radio_pair() as (t_ic705, t_ht):
        if do_ab:
            rows_ab = run_direction(t_ic705, t_ht, "ic705", "ht", probe, carriers)
        if do_ab and do_ba:
            time.sleep(1.0)
        if do_ba:
            rows_ba = run_direction(t_ht, t_ic705, "ht", "ic705", probe, carriers)

    if rows_ab:
        path = f"{args.out_prefix}_ic705_to_ht.csv"
        save_csv(path, rows_ab, "ic705", "ht")
        written.append(path)
    if rows_ba:
        path = f"{args.out_prefix}_ht_to_ic705.csv"
        save_csv(path, rows_ba, "ht", "ic705")
        written.append(path)
    print(f"\nWrote {', '.join(written) if written else '(nothing -- no captures succeeded)'}")
    return 0 if ((not do_ab or rows_ab) and (not do_ba or rows_ba)) else 1


if __name__ == "__main__":
    sys.exit(main())
