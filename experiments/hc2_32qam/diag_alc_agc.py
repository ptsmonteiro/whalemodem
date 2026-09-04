"""Diagnostic: separate ALC (TX) vs AGC (RX) as the cause of HC2-32QAM's
high post-equalization EVM on real hardware.

Two prior `hardware_test.py` runs (3 trials each, IC-7300 TX -> IC-705 RX)
both got 0/3 decoded with healthy acquisition (metric ~0.61-0.71) but
per-carrier EVM of 18.5-32.6%, while the captured audio's broadband
300-3000 Hz passband SNR measured ~38-40 dB -- far better than the ~10-15 dB
effective per-carrier SNR the EVM implies. That gap points at a gain-control
nonlinearity (ALC on TX and/or AGC on RX) distorting the wideband 49-carrier
OFDM signal's relative carrier amplitudes/phases, rather than genuine
channel noise.

This script runs two evidence-gathering experiments, reusing
`hc2_32qam.modulate`/`demodulate` and `benchmark_hc2_snr.frame_metrics`
exactly as `hardware_test.py` does (same 12 kHz capture -> 48 kHz upsample
path), without touching either module's core logic:

1. --mode sweep: TX drive-level sweep. Scales `hc2.modulate()`'s output by
   a handful of amplitude multipliers before sending. If EVM improves
   substantially at lower drive, that is evidence of TX-side ALC
   compression (ALC clips/compresses the high-PAPR OFDM peaks). If EVM
   stays flat with drive level, ALC is probably not the (sole) cause.

2. --mode within-frame: single-trial deep-dive. Splits the decoded frame's
   120 payload OFDM symbols into first-half / second-half and computes EVM
   for each half separately (adapting `_phase_tracked_grid` from
   `benchmark_hc2_snr.py` to operate on a symbol subrange), looking for the
   AGC-settling signature of higher EVM early in the frame. Also prints the
   raw captured audio's RMS envelope in 100ms windows across the whole
   capture (idle -> frame -> tail), looking for a level ramp/sag pattern
   that would indicate AGC gain changing during receipt (cf.
   `scripts/diag_rx_levels.py`).

Run (from the repository root), one radio pair, IC-7300 TX -> IC-705 RX only:
    python experiments/hc2_32qam/diag_alc_agc.py --mode sweep --trials 1
    python experiments/hc2_32qam/diag_alc_agc.py --mode within-frame --trials 3
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import numpy as np
from scipy import signal

import bench
from experiments.hc2_32qam import hc2_32qam as hc2
from experiments.hc2_32qam.benchmark_hc2_snr import frame_metrics, _rms_evm
from whale.transport import RX_SAMPLE_RATE

UPSAMPLE_RATIO = hc2.SAMPLE_RATE // RX_SAMPLE_RATE  # 48000 / 12000 = 4

DEFAULT_DRIVE_LEVELS = (0.3, 0.5, 0.7, 1.0)


def _payload_for(seed, trial):
    rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
    return rng.integers(0, 256, hc2.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()


def _upsample(captured_12k):
    return signal.resample_poly(captured_12k, UPSAMPLE_RATIO, 1)


def _decode(captured_48k, payload):
    try:
        result, diagnostics = hc2.demodulate(captured_48k, return_diagnostics=True)
        error = None
    except Exception as exc:
        result, diagnostics = None, {}
        error = f"{type(exc).__name__}: {exc}"
    metrics = {}
    if error is None and diagnostics.get("start_sample") is not None:
        try:
            metrics = frame_metrics(captured_48k, diagnostics, payload)
        except Exception as exc:
            metrics = {"metrics_error": f"{type(exc).__name__}: {exc}"}
    decoded = result == payload
    return decoded, diagnostics, metrics, error


# --- Experiment 1: TX drive-level sweep (ALC hypothesis) -------------------

def run_sweep(tx, rx, levels, trials, seed, capture_tail, inter_trial):
    print(f"\n== TX drive-level sweep (ALC hypothesis): levels={levels} ==")
    rows = []
    for level in levels:
        for trial in range(1, trials + 1):
            stale = rx.snapshot_rx()
            rx.consume_rx(len(stale))

            payload = _payload_for(seed, trial)
            audio = hc2.modulate(payload) * np.float32(level)

            keyed = tx.send(audio)
            time.sleep(capture_tail)
            captured_12k = rx.snapshot_rx()
            captured_48k = _upsample(captured_12k)

            decoded, diagnostics, metrics, error = _decode(captured_48k, payload)
            acq = diagnostics.get("acquisition_metric")
            evm = metrics.get("evm_percent")
            gain_min = metrics.get("channel_gain_min")
            gain_rms = metrics.get("channel_gain_rms")
            row = {
                "drive_level": level, "trial": trial, "keyed_s": keyed,
                "decoded": bool(decoded), "error": error,
                "acquisition_metric": acq, "evm_percent": evm,
                "channel_gain_min": gain_min, "channel_gain_rms": gain_rms,
            }
            rows.append(row)
            acq_text = "n/a" if acq is None else f"{acq:.3f}"
            evm_text = "n/a" if evm is None else f"{evm:.1f}%"
            gain_text = ("n/a" if gain_min is None
                        else f"min={gain_min:.3f} rms={gain_rms:.3f}")
            print(f"  level={level:.2f} trial={trial}/{trials}: keyed={keyed:.2f}s "
                  f"acq={acq_text} evm={evm_text} gain=({gain_text}) "
                  f"decoded={decoded}" + (f" ({error})" if error else ""))
            time.sleep(inter_trial)

    print("\n  -- drive-level EVM summary --")
    print(f"  {'level':>6} {'n':>3} {'evm_mean%':>10} {'evm_min%':>9} {'evm_max%':>9} {'decoded':>8}")
    for level in levels:
        subset = [r for r in rows if r["drive_level"] == level]
        evms = [r["evm_percent"] for r in subset if r["evm_percent"] is not None]
        decoded_n = sum(1 for r in subset if r["decoded"])
        if evms:
            print(f"  {level:6.2f} {len(subset):3d} {np.mean(evms):10.1f} "
                  f"{np.min(evms):9.1f} {np.max(evms):9.1f} {decoded_n:8d}")
        else:
            print(f"  {level:6.2f} {len(subset):3d} {'n/a':>10} {'n/a':>9} {'n/a':>9} {decoded_n:8d}")
    return rows


# --- Experiment 2: within-frame EVM split + RX envelope (AGC hypothesis) --

def _phase_tracked_grid_range(rx, start, frequency_hz, symbol_lo, symbol_hi):
    """`_phase_tracked_grid` from benchmark_hc2_snr.py, adapted to run the
    decision-directed phase track over all payload symbols (so tracking
    state is identical to the real receiver's) but return only the tracked
    rows in payload-symbol range [symbol_lo, symbol_hi)."""

    samples = np.asarray(rx, dtype=float).reshape(-1)
    analytic = signal.hilbert(samples)
    index = np.arange(len(samples), dtype=float)
    corrected = analytic * np.exp(-2j * np.pi * frequency_hz * index / hc2.SAMPLE_RATE)
    grid = hc2._analytic_carriers(corrected, start)
    channel = np.mean(grid[:hc2.TRAINING_SYMBOLS] / hc2._TRAINING, axis=0)
    if not np.all(np.abs(channel) >= 1e-8):
        return None, channel
    equalized = grid[hc2.TRAINING_SYMBOLS:] / channel
    tracked = np.empty_like(equalized)
    phase = 0.0
    for row_index, row in enumerate(equalized):
        provisional = row * np.exp(-1j * phase)
        decisions = hc2.qam32_from_bits(hc2.bits_from_qam32(provisional))
        phase += np.angle(np.sum(provisional * np.conj(decisions)))
        tracked[row_index] = row * np.exp(-1j * phase)
    return tracked[symbol_lo:symbol_hi], channel


def _evm_for_range(rx, diagnostics, payload, symbol_lo, symbol_hi):
    tracked, channel = _phase_tracked_grid_range(
        rx, int(diagnostics["start_sample"]),
        float(diagnostics["frequency_offset_hz"]), symbol_lo, symbol_hi)
    if tracked is None:
        return None
    decisions = hc2.qam32_from_bits(hc2.bits_from_qam32(tracked)).reshape(tracked.shape)
    reference_bits = hc2._encode_packet(payload).reshape(hc2.PAYLOAD_SYMBOLS, -1)
    reference_full = hc2.qam32_from_bits(hc2._encode_packet(payload)).reshape(
        hc2.PAYLOAD_SYMBOLS, hc2.N_CARRIERS)
    reference = reference_full[symbol_lo:symbol_hi]
    return {
        "evm_percent": 100.0 * _rms_evm(tracked - decisions, decisions),
        "true_evm_percent": 100.0 * _rms_evm(tracked - reference, reference),
    }


def _envelope_stats(name, audio, sr, win_s=0.1):
    n = len(audio)
    if n == 0:
        print(f"   {name}: EMPTY")
        return
    peak = float(np.max(np.abs(audio)))
    clipped = float(np.mean(np.abs(audio) >= 0.999))
    win = max(1, int(sr * win_s))
    n_wins = n // win
    env = [float(np.sqrt(np.mean(audio[i * win:(i + 1) * win].astype(np.float64) ** 2)))
           for i in range(n_wins)]
    print(f"   {name}: {n / sr:.2f}s ({n} samples), peak={peak:.4f}, clipped_frac={clipped:.4%}")
    print(f"      rms envelope ({win_s * 1000:.0f}ms steps): "
          + " ".join(f"{v:.4f}" for v in env))


def run_within_frame(tx, rx, trials, seed, capture_tail, inter_trial):
    print("\n== Within-frame EVM split + RX envelope (AGC hypothesis) ==")
    half = hc2.PAYLOAD_SYMBOLS // 2
    rows = []
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        payload = _payload_for(seed, trial)
        audio = hc2.modulate(payload)

        keyed = tx.send(audio)
        time.sleep(capture_tail)
        captured_12k = rx.snapshot_rx()
        captured_48k = _upsample(captured_12k)

        decoded, diagnostics, metrics, error = _decode(captured_48k, payload)
        acq = diagnostics.get("acquisition_metric")
        print(f"\n  trial {trial}/{trials}: keyed={keyed:.2f}s "
              f"acq={'n/a' if acq is None else f'{acq:.3f}'} "
              f"decoded={decoded}" + (f" ({error})" if error else ""))

        # Whole-capture envelope (12kHz, as actually captured) -- shows any
        # AGC ramp/sag across idle -> frame -> tail.
        _envelope_stats("captured_12k (idle+frame+tail)", captured_12k, RX_SAMPLE_RATE)

        row = {"trial": trial, "decoded": bool(decoded),
               "acquisition_metric": acq, "whole_evm_percent": metrics.get("evm_percent")}

        if error is None and diagnostics.get("start_sample") is not None:
            first = _evm_for_range(captured_48k, diagnostics, payload, 0, half)
            second = _evm_for_range(captured_48k, diagnostics, payload, half, hc2.PAYLOAD_SYMBOLS)
            row["first_half_evm_percent"] = first["evm_percent"] if first else None
            row["second_half_evm_percent"] = second["evm_percent"] if second else None
            print(f"    whole-frame evm={metrics.get('evm_percent')}"
                  f"  first-half evm={row['first_half_evm_percent']}"
                  f"  second-half evm={row['second_half_evm_percent']}")

            # Envelope within just the frame span, at 48k, finer resolution.
            start = int(diagnostics["start_sample"])
            frame_slice = captured_48k[start:start + hc2.FRAME_SAMPLES]
            _envelope_stats("frame_span_48k", frame_slice, hc2.SAMPLE_RATE, win_s=0.05)
        else:
            row["first_half_evm_percent"] = None
            row["second_half_evm_percent"] = None
            print("    (no start_sample; skipping split-EVM)")

        rows.append(row)
        time.sleep(inter_trial)

    print("\n  -- within-frame EVM summary --")
    print(f"  {'trial':>5} {'whole%':>8} {'first%':>8} {'second%':>8}")
    for r in rows:
        def fmt(v):
            return "n/a" if v is None else f"{v:.1f}"
        print(f"  {r['trial']:5d} {fmt(r['whole_evm_percent']):>8} "
              f"{fmt(r['first_half_evm_percent']):>8} {fmt(r['second_half_evm_percent']):>8}")
    return rows


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300", help="TX station (default: ic7300)")
    ap.add_argument("--b", default="ic705", help="RX station (default: ic705)")
    ap.add_argument("--mode", choices=("sweep", "within-frame"), required=True)
    ap.add_argument("--trials", type=int, default=1,
                    help="trials per drive level (sweep) or total trials (within-frame)")
    ap.add_argument("--levels", type=float, nargs="+", default=list(DEFAULT_DRIVE_LEVELS))
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--capture-tail", type=float, default=1.5)
    ap.add_argument("--inter-trial", type=float, default=0.5)
    args = ap.parse_args(argv)

    print("HC2-32QAM ALC/AGC diagnostic (undeclared mode, real hardware only)")
    print(f"radios: TX={args.a} -> RX={args.b}; mode={args.mode}")

    with pair_factory(args.a, args.b, warmup=3.0) as (tx, rx):
        if args.mode == "sweep":
            run_sweep(tx, rx, args.levels, args.trials, args.seed,
                      args.capture_tail, args.inter_trial)
        else:
            run_within_frame(tx, rx, args.trials, args.seed,
                             args.capture_tail, args.inter_trial)
    return 0


if __name__ == "__main__":
    sys.exit(main())
