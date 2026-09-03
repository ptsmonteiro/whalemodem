"""C/N0 (carrier-to-noise-density ratio) probe for the IC-705 (transmit) ->
IC-7300 (receive) HF path at minimum IC-705 RF power.

This is not a modem test. It exists to measure the one number that sets a
hard ceiling on any waveform's bit rate over this path: C/N0 in dB-Hz. A
tone can be detected, and C/N0 measured, far below the level at which any
modem actually decodes.

Method: transmit a long steady single tone (default 30 s). Split the
receive capture into --block-seconds blocks (default 1.0 s => 1 Hz bins at
the 12 kHz receive rate), take each block's power spectrum, and average the
power spectra INCOHERENTLY across blocks. This path loses phase coherence
after roughly 0.3-1.0 s, so a single 30 s coherent FFT would smear the tone
across many bins and under-report it; incoherent averaging keeps the per-bin
noise mean the same while shrinking its variance by ~1/M (M = block count),
so a tone well below the single-block noise floor becomes statistically
visible.

From the averaged spectrum:
  * tone power C: the tone bin's averaged power minus the local noise floor
    (i.e. the excess over noise, not the bin's total power);
  * noise PSD N0: mean power per Hz from bins near the tone but outside a
    guard band around it;
  * C/N0 in dB-Hz = 10*log10(C) - 10*log10(N0);
  * detection significance in sigma: for M averaged periodograms of a bin
    that contains only noise, the per-bin power is (noise variance) times a
    Gamma(M, 1/M) variate, so its mean is the noise floor and its standard
    deviation is (noise floor)/sqrt(M). sigma = C / (noise_floor/sqrt(M)).

An idle (transmitter not keyed) capture of the same duration is analysed
identically as a control: if its "tone bin" shows a comparable excess, nothing
was detected in the keyed trials either and the script says so rather than
reporting a fabricated C/N0.

From the measured C/N0, the maximum information bit rate at a stated Eb/N0
follows from Rb = C/N0 (dB-Hz) - Eb/N0 (dB), i.e.
Rb_bps = 10**((CN0_dBHz - EbN0_dB) / 10). This is an upper bound: it ignores
implementation loss, fading margin, and FEC overhead beyond the assumed
Eb/N0 operating point.

SAFETY: the receiving station is always opened structurally receive-only
(RadioTransport(..., receive_only=True) -- no PTT backend is even
constructed, so nothing in this process can key it). Transmitting on the
IC-705 requires the explicit --allow-ic705-tx flag, matching
experiments/hf15_lowsnr_ofdm/sounder.py and
experiments/hf16_mfsk_lowsnr/hardware_test.py.

whale.transport.RX_BUFFER_SECONDS defaults to 10.0 and would silently
truncate a 30 s capture; this script raises it before opening any
transport, exactly as hardware_test.py does.
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

EB_N0_TABLE_DB = [4.0, 6.0, 8.0, 10.0]


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


def analyze_cn0(capture, rate, freq_hz, block_seconds, guard_bins=5,
                noise_halfwidth_hz=200.0):
    """Incoherent-average C/N0 analysis. See module docstring for the
    statistics behind each field.

    Returns a dict with block_len, n_blocks, bin_hz, tone_bin,
    tone_bin_power_raw (mean power in the tone bin, not noise-subtracted),
    noise_floor_per_bin (mean power per bin among the noise bins),
    tone_power_c (noise-subtracted excess -- may be negative, meaning no
    excess was seen), n0_per_hz, cn0_db_hz (None if tone_power_c <= 0),
    sigma (detection significance), and detection_limit_cn0_db_hz (the
    C/N0 a 3-sigma detection would have corresponded to, given the noise
    floor actually measured and the number of blocks actually averaged --
    i.e. how sensitive this particular capture was).
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
    spec = np.fft.rfft(blocks, axis=1)
    power = (np.abs(spec) ** 2) / (block_len ** 2)  # per-block one-sided power spectrum
    avg_power = power.mean(axis=0)

    bin_hz = rate / block_len
    tone_bin = int(round(freq_hz * block_len / rate))
    noise_half_bins = max(guard_bins + 1, int(round(noise_halfwidth_hz / bin_hz)))
    lo = max(1, tone_bin - noise_half_bins)
    hi = min(len(avg_power) - 2, tone_bin + noise_half_bins)
    all_bins = np.arange(lo, hi + 1)
    mask = np.abs(all_bins - tone_bin) > guard_bins
    noise_bins = all_bins[mask]

    tone_raw = float(avg_power[tone_bin])
    noise_floor = float(np.mean(avg_power[noise_bins]))
    noise_std_of_mean = noise_floor / np.sqrt(n_blocks)  # Gamma(M,1/M) statistic
    tone_c = tone_raw - noise_floor
    n0 = noise_floor / bin_hz
    sigma = tone_c / noise_std_of_mean if noise_std_of_mean > 0 else None

    cn0_db_hz = float(10 * np.log10(tone_c / n0)) if tone_c > 0 and n0 > 0 else None
    # 3-sigma detection threshold in the same units, given this capture's
    # actual noise floor and block count -- the C/N0 this measurement setup
    # could have seen, whether or not it actually did.
    threshold_c = 3.0 * noise_std_of_mean
    detection_limit_cn0_db_hz = (
        float(10 * np.log10(threshold_c / n0)) if threshold_c > 0 and n0 > 0 else None
    )

    result.update({
        "bin_hz": float(bin_hz), "tone_bin": tone_bin,
        "noise_bins_used": int(noise_bins.size),
        "tone_bin_power_raw": tone_raw,
        "noise_floor_per_bin": noise_floor,
        "tone_power_c": tone_c,
        "n0_per_hz": n0,
        "sigma": float(sigma) if sigma is not None else None,
        "cn0_db_hz": cn0_db_hz,
        "detection_limit_cn0_db_hz": detection_limit_cn0_db_hz,
        "detected": bool(cn0_db_hz is not None and sigma is not None and sigma >= 3.0),
    })
    return result


def bitrate_table(cn0_db_hz, eb_n0_list=EB_N0_TABLE_DB):
    rows = []
    for ebn0 in eb_n0_list:
        rb_bps = 10 ** ((cn0_db_hz - ebn0) / 10.0)
        rows.append({"eb_n0_db": ebn0, "rb_bps": rb_bps})
    return rows


def print_bitrate_table(cn0_db_hz, label):
    print(f"\n  max bit rate upper bound from {label} = {cn0_db_hz:.1f} dB-Hz "
          f"(Rb = C/N0 - Eb/N0, ignores fading margin and implementation loss):")
    print(f"    {'Eb/N0 (dB)':>10} {'Rb (bps)':>12}")
    for row in bitrate_table(cn0_db_hz):
        print(f"    {row['eb_n0_db']:>10.0f} {row['rb_bps']:>12.2f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tx", default="ic705")
    ap.add_argument("--rx", default="ic7300")
    ap.add_argument("--allow-ic705-tx", action="store_true")
    ap.add_argument("--freq", type=float, default=1500.0)
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="duration of the steady tone (and of the idle control)")
    ap.add_argument("--block-seconds", type=float, default=1.0,
                    help="incoherent-averaging block length")
    ap.add_argument("--trials", type=int, default=2,
                    help="number of keyed trials")
    ap.add_argument("--amplitude", type=float, default=1.0,
                    help="software transmit amplitude (0-1); the IC-705's "
                         "RF power setting itself is deliberately at minimum "
                         "and is never touched by this script")
    ap.add_argument("--capture-tail", type=float, default=1.0,
                    help="extra capture time after un-keying")
    ap.add_argument("--inter-trial", type=float, default=2.0)
    ap.add_argument("--settle-seconds", type=float, default=1.0)
    ap.add_argument("--out", type=Path, required=True,
                    help="output directory for captures + JSON artifact")
    args = ap.parse_args(argv)

    if args.tx == "ic705" and not args.allow_ic705_tx:
        ap.error("transmitting on the IC-705 requires --allow-ic705-tx")

    wanted_buffer = args.seconds + args.capture_tail + 3.0
    if wanted_buffer > _transport.RX_BUFFER_SECONDS:
        print(f"  raising RX_BUFFER_SECONDS "
              f"{_transport.RX_BUFFER_SECONDS:.1f}s -> {wanted_buffer:.1f}s "
              f"for a {args.seconds:.1f}s capture")
        _transport.RX_BUFFER_SECONDS = wanted_buffer

    args.out.mkdir(parents=True, exist_ok=True)

    print(f"cn0_probe: tx={args.tx} rx={args.rx} freq={args.freq}Hz "
          f"seconds={args.seconds} block_seconds={args.block_seconds} "
          f"trials={args.trials} amplitude={args.amplitude}")

    record = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "tx": args.tx, "rx": args.rx, "freq_hz": args.freq,
        "seconds": args.seconds, "block_seconds": args.block_seconds,
        "trials": args.trials, "amplitude": args.amplitude,
        "tx_sample_rate": TX_SAMPLE_RATE, "rx_sample_rate": RX_SAMPLE_RATE,
        "rx_buffer_seconds": _transport.RX_BUFFER_SECONDS,
        "idle": None, "keyed": [],
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

        # --- idle control: transmitter never keyed ---
        print(f"\n  capturing idle control ({args.seconds:.1f}s, tx not keyed)...")
        time.sleep(args.seconds + args.capture_tail)
        idle_capture = np.asarray(rxp.snapshot_rx(), dtype=np.float64)
        np.save(args.out / "idle.npy", idle_capture.astype(np.float32))
        idle_analysis = analyze_cn0(idle_capture, RX_SAMPLE_RATE, args.freq,
                                    args.block_seconds)
        record["idle"] = {"capture_file": "idle.npy", **idle_analysis}
        cn0_str = (f"{idle_analysis['cn0_db_hz']:.1f}"
                  if idle_analysis.get("cn0_db_hz") is not None else "n/a (no excess)")
        print(f"    idle: n_blocks={idle_analysis['n_blocks']} "
              f"sigma={idle_analysis.get('sigma')} cn0_db_hz={cn0_str} "
              f"detected={idle_analysis.get('detected')}")

        # --- keyed trials ---
        for trial in range(1, args.trials + 1):
            print(f"\n  keyed trial {trial}/{args.trials} "
                  f"({args.seconds:.1f}s tone @ {args.freq}Hz, "
                  f"amplitude={args.amplitude})...")
            tx_audio = make_tone(args.freq, args.seconds, args.amplitude,
                                 TX_SAMPLE_RATE)
            rxp.consume_rx(len(rxp.snapshot_rx()))
            keyed = txp.send(tx_audio)
            time.sleep(args.capture_tail)
            cap = np.asarray(rxp.snapshot_rx(), dtype=np.float64)
            fname = f"keyed_t{trial:02d}.npy"
            np.save(args.out / fname, cap.astype(np.float32))
            analysis = analyze_cn0(cap, RX_SAMPLE_RATE, args.freq, args.block_seconds)
            row = {"trial": trial, "capture_file": fname, "keyed_seconds": keyed,
                  **analysis}
            record["keyed"].append(row)
            cn0_str = (f"{analysis['cn0_db_hz']:.1f}"
                      if analysis.get("cn0_db_hz") is not None else "n/a (no excess)")
            print(f"    trial {trial}: keyed={keyed:.2f}s n_blocks={analysis['n_blocks']} "
                  f"sigma={analysis.get('sigma')} cn0_db_hz={cn0_str} "
                  f"detected={analysis.get('detected')}")
            if trial != args.trials:
                time.sleep(args.inter_trial)
    finally:
        txp.close()
        rxp.close()

    (args.out / "cn0_probe.json").write_text(json.dumps(record, indent=1))
    print(f"\nwrote {args.out / 'cn0_probe.json'}")

    print("\n== summary ==")
    idle = record["idle"]
    print(f"  idle control: sigma={idle.get('sigma')} "
          f"cn0_db_hz={idle.get('cn0_db_hz')} detected={idle.get('detected')}")
    for row in record["keyed"]:
        print(f"  keyed trial {row['trial']}: sigma={row.get('sigma')} "
              f"cn0_db_hz={row.get('cn0_db_hz')} detected={row.get('detected')}")

    detected_rows = [r for r in record["keyed"] if r.get("detected")]
    if idle.get("detected"):
        print("\n  idle control ALSO shows a comparable excess in the tone bin -- "
              "this invalidates a naive reading of the keyed trials as a "
              "detection. Treat any keyed 'detection' with suspicion; the "
              "measured cn0 may reflect a fixed spur, not the transmitted tone.")

    if detected_rows and not idle.get("detected"):
        cn0_values = [r["cn0_db_hz"] for r in detected_rows]
        mean_cn0 = float(np.mean(cn0_values))
        print(f"\n  TONE DETECTED in {len(detected_rows)}/{len(record['keyed'])} "
              f"keyed trials (>=3 sigma), idle control clean.")
        print(f"  mean measured C/N0 over detected trials = {mean_cn0:.1f} dB-Hz")
        print_bitrate_table(mean_cn0, "mean measured C/N0")
    else:
        # No detection: report the sensitivity limit this measurement setup
        # actually had, from the keyed trials' own noise floors.
        limits = [r["detection_limit_cn0_db_hz"] for r in record["keyed"]
                  if r.get("detection_limit_cn0_db_hz") is not None]
        print("\n  NO TONE DETECTED: no keyed trial showed a >=3-sigma excess "
              "over its local noise floor" +
              (" (or the idle control showed a comparable excess, "
               "invalidating any that did)." if idle.get("detected") else "."))
        if limits:
            best_limit = float(np.max(limits))
            print(f"  detection limit of this measurement (3-sigma, given the "
                  f"actual noise floor and {args.seconds:.0f}s / "
                  f"{record['keyed'][0]['n_blocks']}-block averaging): "
                  f"C/N0 <~ {best_limit:.1f} dB-Hz would have been visible.")
            print_bitrate_table(best_limit, "3-sigma detection limit")
        print("\n  This is a null result: report it as such, do not present the "
              "detection limit as a measured C/N0.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
