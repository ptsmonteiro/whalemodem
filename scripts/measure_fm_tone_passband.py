"""Find the audio passband that is usable in BOTH directions of the FM bench.

First step toward a fast MFSK FM mode: before picking a carrier grid, find
out which part of 0-4000 Hz the link actually carries reliably, in both
directions, with margin.

Three probe types, each its own keying (RadioTransport's RX ring buffer
holds only the last RX_BUFFER_SECONDS = 10 s -- see whale/transport.py --
so a single keying has to fit comfortably under that, which is why the
80-tone stepped sweep is split into two keyings of 40 tones rather than
sent as one long burst):

  * Stepped tones, in two chunks covering the low and high half of the
    grid: every frequency, one at a time, 120 ms each. A single tone is the
    closest thing to an undistorted probe -- no intermodulation, no
    crest-factor compression by the limiter -- so this is the cleanest
    read of the per-frequency response and is immune to a multitone comb's
    clipping/limiting masking a real notch or inflating a real one. Each
    chunk's keying opens with its own noise-only (silence) segment, giving
    a direct per-frequency noise floor rather than one inferred from
    off-grid neighbour bins.
  * A Schroeder-phase multitone comb across the full grid (low crest
    factor, ~4 dB) as a cross-check that stepped and simultaneous tones
    agree, since pre-emphasis/de-emphasis and the limiter's attack/release
    act on a comb differently than on a lone tone. SNR here comes from
    off-grid neighbour bins (measure_ofdm_carrier_response.py's method),
    since no tone is alone long enough for its own silence segment.

Drive is fixed at 0.077 peak, the level e124a6d found holds both directions
on the Wouxun without compressing (0.22 clipped the handheld's mic input).

Grid: 50-4000 Hz, 50 Hz spacing (80 tones), matching
measure_ofdm_carrier_response.py's grid so results are comparable.

Each direction runs all three keyings --trials times (default 3).
Alignment locates each keying's deterministic tone/comb region in its
capture by cross-correlation against a crudely-decimated copy of the known
TX waveform (same trick as measure_fm_audio_band.py's align()); the
noise-only segment's position (stepped-tone keyings only) then falls out
of that offset algebraically, since it immediately precedes the tones in
the TX buffer and clock drift over one ~5 s keying is a small fraction of
a sample (measure_clock_offset.py: -5.2/+1.2 ppm).

"Usable" for one direction: per-tone SNR within 6 dB of that direction's
in-band plateau (median of the top quartile of stepped-tone SNR) AND that
margin held on every trial (worst-of-trials, not average). The reported
intersection band is where both directions' usable ranges overlap.

Run from the repository root:
    python scripts/measure_fm_tone_passband.py
    python scripts/measure_fm_tone_passband.py --trials 5 --out-dir scratch
"""
import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np
from scipy.signal import correlate

import bench
from whale.transport import RX_SAMPLE_RATE, SAMPLE_RATE, RX_BUFFER_SECONDS

FREQ_LO_HZ = 50.0
FREQ_HI_HZ = 4000.0
FREQ_STEP_HZ = 50.0
CHUNK_SIZE = 40                # tones per stepped-tone keying (2 chunks of 80)

DRIVE = 0.077                  # vf12's calibrated linear peak drive (e124a6d)

TONE_SECONDS = 0.12
TONE_GAP_SECONDS = 0.02
TONE_SKIP_SECONDS = 0.02       # settle time discarded from the start of each tone window
NOISE_SECONDS = 0.5
LEAD_PAD_SECONDS = 0.3         # low-level noise: let squelch/AGC re-settle before each keying
COMB_SECONDS = 2.0
TAIL_PAD_SECONDS = 0.1
CAPTURE_TAIL = 0.8
INTER_KEYING = 0.5

PLATEAU_PERCENTILE = 75.0      # top-quartile SNR tones define "in-band plateau"
USABLE_MARGIN_DB = 6.0
DECIMATION = SAMPLE_RATE // RX_SAMPLE_RATE

TRIALS = 3


def freqs():
    return np.arange(FREQ_LO_HZ, FREQ_HI_HZ + 1e-6, FREQ_STEP_HZ)


def chunks(freq_list, size=CHUNK_SIZE):
    return [freq_list[i:i + size] for i in range(0, len(freq_list), size)]


def schroeder_phases(n):
    return np.pi * np.arange(n) ** 2 / n


def build_stepped(freq_chunk):
    """Stepped-tone region only (no lead/noise/tail): one tone at a time.

    Returns (audio, tone_starts_samples) at TX rate, tone_starts relative to
    the first sample of this array.
    """
    tone_n = int(round(TONE_SECONDS * SAMPLE_RATE))
    gap_n = int(round(TONE_GAP_SECONDS * SAMPLE_RATE))
    t_tone = np.arange(tone_n) / SAMPLE_RATE

    tone_starts = []
    pieces = []
    cursor = 0
    for f in freq_chunk:
        tone_starts.append(cursor)
        pieces.append((DRIVE * np.sin(2 * np.pi * f * t_tone)).astype(np.float32))
        pieces.append(np.zeros(gap_n, dtype=np.float32))
        cursor += tone_n + gap_n

    return np.concatenate(pieces), np.array(tone_starts)


def build_comb(freq_list, seed=0x0FDA):
    comb_n = int(round(COMB_SECONDS * SAMPLE_RATE))
    t_comb = np.arange(comb_n) / SAMPLE_RATE
    phases = schroeder_phases(len(freq_list))
    comb = np.zeros(comb_n, dtype=np.float64)
    for f, phase in zip(freq_list, phases):
        comb += np.sin(2 * np.pi * f * t_comb + phase)
    comb *= DRIVE / np.max(np.abs(comb))
    return comb.astype(np.float32)


def decimate_crude(audio):
    """Average every DECIMATION samples -- good enough to locate the probe
    in a 12 kHz capture, never used to measure the response itself."""
    n = (len(audio) // DECIMATION) * DECIMATION
    return audio[:n].reshape(-1, DECIMATION).mean(axis=1).astype(np.float32)


def locate(captured, reference_rx):
    if len(captured) < len(reference_rx) + int(0.3 * RX_SAMPLE_RATE):
        return None
    corr = correlate(captured.astype(np.float64), reference_rx.astype(np.float64),
                     mode="valid", method="fft")
    return int(np.argmax(np.abs(corr)))


def tone_power_and_phase(captured, start, freq_chunk, tone_starts_tx):
    """Per-tone received power (summed over +/-2 bins) and centre-bin phase,
    at RX rate, from a stepped-tone region."""
    skip = int(round(TONE_SKIP_SECONDS * RX_SAMPLE_RATE))
    tone_n_rx = int(round(TONE_SECONDS * RX_SAMPLE_RATE)) - skip
    if tone_n_rx <= 0:
        raise ValueError("TONE_SKIP_SECONDS too close to TONE_SECONDS")
    window = np.hanning(tone_n_rx)
    power = np.full(len(freq_chunk), np.nan)
    phase = np.full(len(freq_chunk), np.nan)
    for i, (f, tx_start) in enumerate(zip(freq_chunk, tone_starts_tx)):
        rx_start = start + int(round(tx_start / DECIMATION)) + skip
        seg = captured[rx_start:rx_start + tone_n_rx]
        if len(seg) < tone_n_rx:
            continue
        spectrum = np.fft.rfft(seg * window)
        bin_hz = RX_SAMPLE_RATE / tone_n_rx
        center = int(round(f / bin_hz))
        lo, hi = max(0, center - 2), min(len(spectrum), center + 3)
        power[i] = float(np.sum(np.abs(spectrum[lo:hi]) ** 2))
        phase[i] = float(np.angle(spectrum[center]))
    return power, phase


def noise_floor(captured, start, freq_chunk):
    """Per-frequency noise power from the silence segment immediately
    preceding the stepped tones (algebraic offset -- see module docstring)."""
    noise_n = int(round(NOISE_SECONDS * RX_SAMPLE_RATE))
    noise_start = start - noise_n
    if noise_start < 0:
        return np.full(len(freq_chunk), np.nan)
    seg = captured[noise_start:start]
    if len(seg) < noise_n:
        return np.full(len(freq_chunk), np.nan)
    window = np.hanning(len(seg))
    spectrum = np.fft.rfft(seg * window)
    bin_hz = RX_SAMPLE_RATE / len(seg)
    out = np.empty(len(freq_chunk))
    for i, f in enumerate(freq_chunk):
        center = int(round(f / bin_hz))
        lo, hi = max(0, center - 2), min(len(spectrum), center + 3)
        out[i] = float(np.sum(np.abs(spectrum[lo:hi]) ** 2))
    return out


def comb_power_and_noise(captured, start, freq_list):
    comb_n_rx = int(round(COMB_SECONDS * RX_SAMPLE_RATE))
    seg = captured[start:start + comb_n_rx]
    if len(seg) < comb_n_rx:
        return np.full(len(freq_list), np.nan), np.full(len(freq_list), np.nan)
    spectrum = np.fft.rfft(seg)  # rectangular: every comb tone sits on an exact bin
    bin_hz = RX_SAMPLE_RATE / comb_n_rx
    power = np.empty(len(freq_list))
    noise = np.empty(len(freq_list))
    for i, f in enumerate(freq_list):
        center = int(round(f / bin_hz))
        power[i] = float(np.abs(spectrum[center]) ** 2)
        off = int(round(25.0 / bin_hz))
        lo_bin = max(0, center - off)
        hi_bin = min(len(spectrum) - 1, center + off)
        noise[i] = float(np.mean([np.abs(spectrum[lo_bin]) ** 2,
                                  np.abs(spectrum[hi_bin]) ** 2]))
    return power, noise


def run_stepped_keying(tx, rx, freq_chunk):
    stepped_audio, tone_starts_tx = build_stepped(freq_chunk)
    reference_rx = decimate_crude(stepped_audio)

    stale = rx.snapshot_rx()
    rx.consume_rx(len(stale))
    lead = bench.noise_pad(seconds=LEAD_PAD_SECONDS)
    noise_seg = np.zeros(int(round(NOISE_SECONDS * SAMPLE_RATE)), dtype=np.float32)
    tail = np.zeros(int(round(TAIL_PAD_SECONDS * SAMPLE_RATE)), dtype=np.float32)
    tx_audio = np.concatenate([lead, noise_seg, stepped_audio, tail])
    assert len(tx_audio) / SAMPLE_RATE + CAPTURE_TAIL < RX_BUFFER_SECONDS - 1.5, \
        "stepped-tone keying too long for the 10s RX ring buffer"

    tx.send(tx_audio)
    time.sleep(CAPTURE_TAIL)
    captured = rx.snapshot_rx()

    start = locate(captured, reference_rx)
    if start is None:
        return None
    power, phase = tone_power_and_phase(captured, start, freq_chunk, tone_starts_tx)
    noise = noise_floor(captured, start, freq_chunk)
    return power, noise, phase


def run_comb_keying(tx, rx, freq_list):
    comb_audio = build_comb(freq_list)
    reference_rx = decimate_crude(comb_audio)

    stale = rx.snapshot_rx()
    rx.consume_rx(len(stale))
    lead = bench.noise_pad(seconds=LEAD_PAD_SECONDS)
    tail = np.zeros(int(round(TAIL_PAD_SECONDS * SAMPLE_RATE)), dtype=np.float32)
    tx_audio = np.concatenate([lead, comb_audio, tail])
    assert len(tx_audio) / SAMPLE_RATE + CAPTURE_TAIL < RX_BUFFER_SECONDS - 1.5, \
        "comb keying too long for the 10s RX ring buffer"

    tx.send(tx_audio)
    time.sleep(CAPTURE_TAIL)
    captured = rx.snapshot_rx()

    start = locate(captured, reference_rx)
    if start is None:
        return None
    return comb_power_and_noise(captured, start, freq_list)


def run_trial(tx, rx, freq_list, freq_chunks):
    tone_power = np.full(len(freq_list), np.nan)
    tone_noise = np.full(len(freq_list), np.nan)
    tone_phase = np.full(len(freq_list), np.nan)
    offset = 0
    for chunk in freq_chunks:
        result = run_stepped_keying(tx, rx, chunk)
        if result is not None:
            power, noise, phase = result
            tone_power[offset:offset + len(chunk)] = power
            tone_noise[offset:offset + len(chunk)] = noise
            tone_phase[offset:offset + len(chunk)] = phase
        offset += len(chunk)
        time.sleep(INTER_KEYING)

    comb_result = run_comb_keying(tx, rx, freq_list)
    time.sleep(INTER_KEYING)

    tone_snr_db = 10 * np.log10(np.maximum(tone_power, 1e-30) /
                                np.maximum(tone_noise, 1e-30))
    if comb_result is None:
        comb_snr_db = np.full(len(freq_list), np.nan)
    else:
        comb_power, comb_noise = comb_result
        comb_snr_db = 10 * np.log10(np.maximum(comb_power, 1e-30) /
                                    np.maximum(comb_noise, 1e-30))

    return {
        "tone_power_db": 10 * np.log10(np.maximum(tone_power, 1e-30)),
        "tone_snr_db": tone_snr_db,
        "tone_phase": tone_phase,
        "comb_snr_db": comb_snr_db,
    }


def smooth(values, width=5):
    """Moving average across frequency, edge-replicated so it stays defined
    at the band ends.

    A single 0.5 s noise-only read per stepped-tone keying (two keyings
    cover all 80 tones) is one high-variance realisation per bin: adjacent
    50 Hz tones inside a genuinely flat part of the band can read 15+ dB
    apart in raw per-tone SNR even though the underlying response is smooth
    (see rel_db, which comes from a signal-power estimate and is not noisy
    the same way). Smoothing across frequency before picking band edges is
    the same fix measure_fm_audio_band.py applies for the same reason.
    """
    padded = np.pad(values, width // 2, mode="edge")
    return np.convolve(padded, np.ones(width) / width, mode="valid")


def plateau_db(snr_db):
    finite = snr_db[np.isfinite(snr_db)]
    if len(finite) == 0:
        return np.nan
    return float(np.percentile(finite, PLATEAU_PERCENTILE))


def contiguous_usable_band(freq_list, worst_snr_db, threshold_db):
    """Widest contiguous run of tones at/above threshold that contains the
    highest-SNR tone. Edges reported at the half-step outside the last
    passing tone, like a -6 dB crossing would land."""
    keep = np.isfinite(worst_snr_db) & (worst_snr_db >= threshold_db)
    if not keep.any():
        return None
    peak = int(np.nanargmax(worst_snr_db))
    if not keep[peak]:
        candidates = np.where(keep)[0]
        peak = int(candidates[np.argmax(worst_snr_db[candidates])])
    lo = hi = peak
    while lo > 0 and keep[lo - 1]:
        lo -= 1
    while hi + 1 < len(keep) and keep[hi + 1]:
        hi += 1
    half = FREQ_STEP_HZ / 2
    return (float(freq_list[lo] - half), float(freq_list[hi] + half))


def run_direction(tx, rx, label, trials, freq_list, freq_chunks):
    per_trial = []
    for i in range(1, trials + 1):
        result = run_trial(tx, rx, freq_list, freq_chunks)
        finite = np.isfinite(result["tone_snr_db"])
        if finite.any():
            print(f"  {label} {i}/{trials}: {finite.sum()}/{len(freq_list)} tones "
                  f"read, median SNR {np.nanmedian(result['tone_snr_db']):.1f} dB")
            per_trial.append(result)
        else:
            print(f"  {label} {i}/{trials}: no tones read (probe not found)")
        time.sleep(INTER_KEYING)
    if not per_trial:
        return None

    tone_snr = np.array([r["tone_snr_db"] for r in per_trial])       # trials x n
    tone_power = np.array([r["tone_power_db"] for r in per_trial])
    comb_snr = np.array([r["comb_snr_db"] for r in per_trial])
    phase = np.array([r["tone_phase"] for r in per_trial])

    worst_snr = np.nanmin(tone_snr, axis=0)   # worst-of-trials: this is the reliability bar
    mean_snr = np.nanmean(tone_snr, axis=0)
    mean_power_db = np.nanmean(tone_power, axis=0)
    rel_db = mean_power_db - np.nanmax(mean_power_db)
    mean_comb_snr = np.nanmean(comb_snr, axis=0)

    # Band-edge decisions run on frequency-smoothed SNR (see smooth()'s
    # docstring); raw per-tone values are still what gets printed/saved.
    smoothed_worst_snr = smooth(worst_snr)
    smoothed_mean_snr = smooth(mean_snr)
    plateau = plateau_db(smoothed_mean_snr)
    threshold = plateau - USABLE_MARGIN_DB
    band = contiguous_usable_band(freq_list, smoothed_worst_snr, threshold)

    return {
        "trials_ok": len(per_trial),
        "freqs_hz": freq_list,
        "rel_db": rel_db,
        "mean_snr_db": mean_snr,
        "worst_snr_db": worst_snr,
        "std_snr_db": np.nanstd(tone_snr, axis=0),
        "mean_comb_snr_db": mean_comb_snr,
        "phase_rad": phase,
        "plateau_db": plateau,
        "threshold_db": threshold,
        "usable_band_hz": band,
    }


def save_csv(path, direction_results):
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["direction", "freq_hz", "rel_db", "mean_snr_db",
                         "worst_snr_db", "std_snr_db", "mean_comb_snr_db",
                         "mean_phase_rad"])
        for label, res in direction_results.items():
            if not res:
                continue
            for i, f in enumerate(res["freqs_hz"]):
                mean_phase = np.nanmean(res["phase_rad"][:, i])
                writer.writerow([
                    label, f, f"{res['rel_db'][i]:.2f}", f"{res['mean_snr_db'][i]:.2f}",
                    f"{res['worst_snr_db'][i]:.2f}", f"{res['std_snr_db'][i]:.2f}",
                    f"{res['mean_comb_snr_db'][i]:.2f}",
                    "" if np.isnan(mean_phase) else f"{mean_phase:.3f}",
                ])


def print_table(direction_results, step=250.0):
    labels = [l for l, r in direction_results.items() if r]
    if not labels:
        return
    freq_list = direction_results[labels[0]]["freqs_hz"]
    idxs = [i for i, f in enumerate(freq_list)
           if abs((f - FREQ_LO_HZ) % step) < 1e-6 or i == 0]
    header = "  freq_hz  " + "  ".join(f"{l:>22s}" for l in labels)
    print(header)
    for i in idxs:
        f = freq_list[i]
        row = f"  {f:7.0f}  "
        for label in labels:
            res = direction_results[label]
            row += f"  rel{res['rel_db'][i]:6.1f} snr{res['worst_snr_db'][i]:6.1f}"
        print(row)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=TRIALS)
    ap.add_argument("--a", default="ic705", help="station A radio name")
    ap.add_argument("--b", default="ht", help="station B radio name")
    ap.add_argument("--out-dir", default=".", help="directory for the CSV output")
    ap.add_argument("--out-prefix", default="fm_tone_passband")
    args = ap.parse_args()

    freq_list = freqs()
    freq_chunks = chunks(freq_list)
    stepped_s = LEAD_PAD_SECONDS + NOISE_SECONDS + TAIL_PAD_SECONDS + \
        CHUNK_SIZE * (TONE_SECONDS + TONE_GAP_SECONDS)
    comb_s = LEAD_PAD_SECONDS + COMB_SECONDS + TAIL_PAD_SECONDS
    print(f"{len(freq_list)} tones, {FREQ_LO_HZ:.0f}-{FREQ_HI_HZ:.0f} Hz step "
          f"{FREQ_STEP_HZ:.0f} Hz, drive {DRIVE}, {len(freq_chunks)} stepped keyings "
          f"(~{stepped_s:.1f}s each) + 1 comb keying (~{comb_s:.1f}s), "
          f"{args.trials} trials/direction")

    results = {}
    with bench.radio_pair(a=args.a, b=args.b) as (t_a, t_b):
        for tx, rx, label in ((t_a, t_b, f"{args.a}->{args.b}"),
                              (t_b, t_a, f"{args.b}->{args.a}")):
            print(f"\n{label}:")
            results[label] = run_direction(tx, rx, label, args.trials, freq_list,
                                           freq_chunks)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{args.out_prefix}.csv"
    save_csv(csv_path, results)

    print("\n== per-direction usable bands (worst-of-trials SNR within "
          f"{USABLE_MARGIN_DB:.0f} dB of plateau) ==")
    bands = []
    for label, res in results.items():
        if not res:
            print(f"  {label}: no measurement")
            continue
        band = res["usable_band_hz"]
        bands.append(band)
        print(f"  {label}: plateau {res['plateau_db']:.1f} dB, threshold "
              f"{res['threshold_db']:.1f} dB, band "
              f"{'NONE' if band is None else f'{band[0]:.0f}-{band[1]:.0f} Hz'} "
              f"({res['trials_ok']}/{args.trials} trials usable)")

    print("\n== table (every ~250 Hz) ==")
    print_table(results)

    if len(bands) == 2 and all(b is not None for b in bands):
        lo = max(b[0] for b in bands)
        hi = min(b[1] for b in bands)
        if lo < hi:
            print(f"\n== BOTH-DIRECTION INTERSECTION: {lo:.0f}-{hi:.0f} Hz ==")
        else:
            print(f"\n== BOTH-DIRECTION INTERSECTION: EMPTY "
                  f"({lo:.0f} > {hi:.0f}) ==")
    else:
        print("\n== BOTH-DIRECTION INTERSECTION: not computable "
              "(a direction had no usable band) ==")

    print(f"\nwrote {csv_path}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
