"""Transmit-amplitude sweep for the IC-705 (transmit) -> IC-7300 (receive)
path.

This is NOT a modem test. It exists to answer one narrow question: is
transmit audio amplitude a usable lever on received C/N0, or is the
radio's ALC/MOD gain already saturated so that extra drive buys nothing?

Method (validated against experiments/hf16_mfsk_lowsnr/cn0_run1/): work at
the 12 kHz receive rate, split the capture into 1-second blocks (12000
samples => 1 Hz bins), Hann-window each block, and average the power
spectra INCOHERENTLY across blocks -- this path loses phase coherence
after roughly 0.3-1.0 s, so a single long coherent FFT would smear the tone
across bins and under-report it.

Critically, there is a carrier frequency offset of several Hz between the
two radios (observed ~+8 Hz), so a 1500 Hz transmitted tone does NOT arrive
at exactly 1500 Hz. This script SEARCHES a +/-30 Hz window around the
nominal frequency for the peak and reports the frequency actually found; it
never assumes a fixed bin. The noise reference is taken from bins well
clear of the found peak (excluding a guard of >= +/-6 Hz around it), not
from bins merely adjacent to the nominal frequency -- an earlier probe that
did the latter folded the real (offset) signal into its own noise estimate
and wrongly concluded nothing was there.

Every raw capture is saved as a .npy next to the JSON record, so the
analysis can be redone offline without keying the radio again.

SAFETY: the receiving station is always opened structurally receive-only
(RadioTransport(..., receive_only=True) -- no PTT backend is even
constructed, so nothing in this process can key it). Transmitting on the
IC-705 requires the explicit --allow-ic705-tx flag, matching
experiments/hf15_lowsnr_ofdm/sounder.py and
experiments/hf16_mfsk_lowsnr/hardware_test.py.

whale.transport.RX_BUFFER_SECONDS defaults to 10.0 and would silently
truncate a longer capture; this script raises it before opening any
transport if needed, exactly as cn0_probe.py / hardware_test.py do.
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

import numpy as np

from whale import transport as _transport
from whale.transport import RadioTransport, TX_SAMPLE_RATE, RX_SAMPLE_RATE

BLOCK_SECONDS_DEFAULT = 1.0
NOISE_BANDS_DEFAULT = ((1200.0, 1400.0), (1650.0, 1850.0))


def make_tone(freq_hz, seconds, amplitude, samplerate, fade_ms=20.0):
    """Continuous sine, constant envelope except a short fade in/out to
    avoid a keying click."""
    n = int(round(seconds * samplerate))
    t = np.arange(n) / samplerate
    wave = amplitude * np.sin(2 * np.pi * freq_hz * t)
    fade_n = min(int(round(fade_ms / 1000.0 * samplerate)), n // 2)
    if fade_n > 0:
        ramp = np.linspace(0.0, 1.0, fade_n)
        wave[:fade_n] *= ramp
        wave[-fade_n:] *= ramp[::-1]
    return wave.astype(np.float32)


def analyze_cn0(capture, rate, nominal_freq_hz, block_seconds=BLOCK_SECONDS_DEFAULT,
                search_half_hz=30.0, guard_half_hz=6.0, tone_half_hz=5.0,
                noise_bands=NOISE_BANDS_DEFAULT):
    """Incoherent-average C/N0 analysis with peak search.

    Splits `capture` into `block_seconds`-long blocks, Hann-windows each,
    averages the one-sided PSD (V^2/Hz) incoherently across blocks, then:

      * searches +/- search_half_hz around nominal_freq_hz for the peak and
        reports the frequency actually found (never assumes a fixed bin);
      * takes N0 as the median PSD over `noise_bands` (clean bins well away
        from the tone), excluding anything within guard_half_hz of the
        found peak;
      * takes C as the sum over bins within tone_half_hz of the found peak
        of max(PSD - N0, 0) * 1 Hz (a Hann window spreads a tone over ~3
        bins, so a generous integration band is used);
      * C/N0 (dB-Hz) = 10*log10(C/N0); SNR in 2400 Hz = 10*log10(C/(N0*2400)).

    Returns None values for cn0/snr fields when no positive excess was
    found (a null result), and always returns the frequency actually found.
    """
    capture = np.asarray(capture, dtype=np.float64)
    block_len = int(round(block_seconds * rate))
    n_blocks = capture.size // block_len
    result = {"capture_samples": int(capture.size), "block_len": block_len,
              "n_blocks": int(n_blocks)}
    if n_blocks < 2:
        result.update({"error": "fewer than 2 blocks available"})
        return result

    blocks = capture[:n_blocks * block_len].reshape(n_blocks, block_len)
    w = np.hanning(block_len)
    norm = 2.0 / (rate * np.sum(w ** 2))
    PSD = (np.abs(np.fft.rfft(blocks * w, axis=1)) ** 2 * norm).mean(axis=0)
    freqs = np.fft.rfftfreq(block_len, d=1.0 / rate)
    bin_hz = rate / block_len

    search_mask = (freqs >= nominal_freq_hz - search_half_hz) & \
                  (freqs <= nominal_freq_hz + search_half_hz)
    idx_search = np.where(search_mask)[0]
    if idx_search.size == 0:
        result.update({"error": "search window empty"})
        return result
    peak_idx = idx_search[int(np.argmax(PSD[idx_search]))]
    peak_freq = float(freqs[peak_idx])

    noise_mask = np.zeros_like(freqs, dtype=bool)
    for lo, hi in noise_bands:
        noise_mask |= (freqs >= lo) & (freqs <= hi)
    noise_mask &= np.abs(freqs - peak_freq) > guard_half_hz
    if not np.any(noise_mask):
        result.update({"error": "no noise bins available"})
        return result
    n0_per_hz = float(np.median(PSD[noise_mask]))

    tone_mask = np.abs(freqs - peak_freq) <= tone_half_hz
    tone_power_c = float(np.sum(np.maximum(PSD[tone_mask] - n0_per_hz, 0.0) * bin_hz))

    cn0_db_hz = (float(10 * np.log10(tone_power_c / n0_per_hz))
                 if tone_power_c > 0 and n0_per_hz > 0 else None)
    snr_2400_db = (float(10 * np.log10(tone_power_c / (n0_per_hz * 2400.0)))
                   if tone_power_c > 0 and n0_per_hz > 0 else None)

    result.update({
        "bin_hz": float(bin_hz), "peak_freq_hz": peak_freq,
        "n0_per_hz": n0_per_hz, "tone_power_c": tone_power_c,
        "cn0_db_hz": cn0_db_hz, "snr_2400_db": snr_2400_db,
    })
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tx", default="ic705")
    ap.add_argument("--rx", default="ic7300")
    ap.add_argument("--allow-ic705-tx", action="store_true")
    ap.add_argument("--freq", type=float, default=1500.0)
    ap.add_argument("--amplitudes", type=float, nargs="+",
                    default=[0.1, 0.25, 0.5, 0.7, 0.9, 1.0])
    ap.add_argument("--passes", type=int, default=2,
                    help="number of interleaved sweeps over --amplitudes")
    ap.add_argument("--seconds", type=float, default=10.0,
                    help="duration of each keying")
    ap.add_argument("--gap", type=float, default=2.0,
                    help="silence between keyings")
    ap.add_argument("--idle-seconds", type=float, default=10.0,
                    help="idle noise capture before any transmission")
    ap.add_argument("--capture-tail", type=float, default=1.0,
                    help="extra capture time after un-keying, to catch the tail")
    ap.add_argument("--settle-seconds", type=float, default=1.0)
    ap.add_argument("--block-seconds", type=float, default=BLOCK_SECONDS_DEFAULT)
    ap.add_argument("--out", type=Path, required=True,
                    help="output directory for captures + JSON artifact")
    args = ap.parse_args(argv)

    if args.tx == "ic705" and not args.allow_ic705_tx:
        ap.error("transmitting on the IC-705 requires --allow-ic705-tx")

    wanted_buffer = max(args.seconds, args.idle_seconds) + args.capture_tail + 3.0
    if wanted_buffer > _transport.RX_BUFFER_SECONDS:
        print(f"  raising RX_BUFFER_SECONDS "
              f"{_transport.RX_BUFFER_SECONDS:.1f}s -> {wanted_buffer:.1f}s")
        _transport.RX_BUFFER_SECONDS = wanted_buffer

    args.out.mkdir(parents=True, exist_ok=True)

    plan = []
    for p in range(args.passes):
        for amp in args.amplitudes:
            plan.append((p + 1, amp))

    print(f"tone_probe: tx={args.tx} rx={args.rx} freq={args.freq}Hz "
          f"amplitudes={args.amplitudes} passes={args.passes} "
          f"seconds={args.seconds} gap={args.gap}")
    print(f"  interleaved plan: {plan}")

    record = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "tx": args.tx, "rx": args.rx, "freq_hz": args.freq,
        "amplitudes": args.amplitudes, "passes": args.passes,
        "seconds": args.seconds, "gap": args.gap,
        "block_seconds": args.block_seconds,
        "tx_sample_rate": TX_SAMPLE_RATE, "rx_sample_rate": RX_SAMPLE_RATE,
        "rx_buffer_seconds": _transport.RX_BUFFER_SECONDS,
        "idle": None, "rows": [],
    }

    txp = RadioTransport(args.tx)
    try:
        rxp = RadioTransport(args.rx, receive_only=True)
    except Exception:
        txp.close()
        raise
    try:
        rxp.start_receiving()
        time.sleep(args.settle_seconds)
        rxp.consume_rx(len(rxp.snapshot_rx()))

        # --- idle control ---
        print(f"\n  capturing idle control ({args.idle_seconds:.1f}s, tx not keyed)...")
        time.sleep(args.idle_seconds + args.capture_tail)
        idle_capture = np.asarray(rxp.snapshot_rx(), dtype=np.float64)
        np.save(args.out / "idle.npy", idle_capture.astype(np.float32))
        idle_analysis = analyze_cn0(idle_capture, RX_SAMPLE_RATE, args.freq,
                                    args.block_seconds)
        record["idle"] = {"capture_file": "idle.npy", **idle_analysis}
        pf = idle_analysis.get("peak_freq_hz")
        cn0v = idle_analysis.get("cn0_db_hz")
        print(f"    idle: peak_freq={pf} cn0_db_hz={cn0v}")

        # --- interleaved amplitude sweep, two passes ---
        for i, (pass_no, amp) in enumerate(plan):
            print(f"\n  [{i+1}/{len(plan)}] pass {pass_no} amplitude={amp} "
                  f"({args.seconds:.1f}s tone @ {args.freq}Hz)...")
            tx_audio = make_tone(args.freq, args.seconds, amp, TX_SAMPLE_RATE)
            tx_rms = float(np.sqrt(np.mean(tx_audio.astype(np.float64) ** 2)))
            rxp.consume_rx(len(rxp.snapshot_rx()))
            keyed = txp.send(tx_audio)
            time.sleep(args.capture_tail)
            cap = np.asarray(rxp.snapshot_rx(), dtype=np.float64)
            fname = f"pass{pass_no:02d}_amp{amp:.3g}.npy".replace("/", "_")
            np.save(args.out / fname, cap.astype(np.float32))
            analysis = analyze_cn0(cap, RX_SAMPLE_RATE, args.freq, args.block_seconds)
            row = {"pass": pass_no, "amplitude": amp, "keyed_seconds": keyed,
                  "tx_rms": tx_rms, "capture_file": fname, **analysis}
            record["rows"].append(row)
            pf = analysis.get("peak_freq_hz")
            cn0v = analysis.get("cn0_db_hz")
            snrv = analysis.get("snr_2400_db")
            pf_str = f"{pf:.2f}" if pf is not None else "n/a"
            cn0_str = f"{cn0v:.1f}" if cn0v is not None else "n/a"
            snr_str = f"{snrv:.1f}" if snrv is not None else "n/a"
            print(f"    pass={pass_no} amp={amp:<5g} keyed={keyed:.2f}s "
                  f"peak_freq={pf_str}Hz cn0_db_hz={cn0_str} snr_2400_db={snr_str}")
            if i != len(plan) - 1:
                time.sleep(args.gap)
    finally:
        txp.close()
        rxp.close()

    (args.out / "tone_probe.json").write_text(json.dumps(record, indent=1))
    print(f"\nwrote {args.out / 'tone_probe.json'}")

    print("\n== summary table ==")
    header = f"{'pass':>4} {'amp':>6} {'peak_Hz':>9} {'cn0_dBHz':>9} {'snr2400_dB':>11}"
    print(header)
    for row in record["rows"]:
        pf = row.get("peak_freq_hz")
        cn0v = row.get("cn0_db_hz")
        snrv = row.get("snr_2400_db")
        print(f"{row['pass']:>4} {row['amplitude']:>6g} "
              f"{(pf if pf is not None else float('nan')):>9.2f} "
              f"{(cn0v if cn0v is not None else float('nan')):>9.1f} "
              f"{(snrv if snrv is not None else float('nan')):>11.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
