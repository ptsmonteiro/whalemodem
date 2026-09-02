"""Diagnostic: does a noise lead-in (audio-chain settling time) reduce
HC2-32QAM's high post-equalization EVM on real hardware?

Two prior investigations (see `hardware_test.py` and `diag_alc_agc.py`
docstrings) established: acquisition locks fine (metric ~0.6-0.7), but
per-carrier EVM is 18.5-32.6% -- far too high for 32QAM -- while broadband
300-3000 Hz passband SNR in the captures is ~38-40 dB, a 25dB+ gap. A TX
drive-level sweep ruled out ALC compression (EVM flat 20-25% across
0.3x-1.0x drive) and a within-frame first-half/second-half EVM split plus
RX envelope inspection found no AGC ramp/sag/pumping asymmetry. That agent
noted `channel_gain_min` sitting notably below `channel_gain_rms` in every
trial, suggesting a non-flat channel response across the 2.25 kHz occupied
span.

This script tests a further hypothesis: maybe the TX/RX audio chain (DAC/
ADC settling, radio audio path filters, PLL/clock settling after PTT keys)
has not settled by the time HC2's frame starts, even though the captured
envelope looked flat. `scripts/bench.py` has `noise_pad()` / `PAD_SECONDS`
for exactly this reasoning, built for AFSK frames via `run_trials(...,
pad=True)`. This script reuses `bench.noise_pad()` but wires it around an
HC2 frame directly (HC2 uses `hc2.modulate()`/`hc2.demodulate()`, not the
`afsk` module `run_trials` drives), prepending 0s / 1s / 2s of low-level
white noise before the real frame and comparing EVM.

`hc2.demodulate()` performs its own acquisition/correlation search over the
whole capture, so it should find the frame regardless of where the pad
puts it -- but the pad adds real keyed airtime, so the capture-tail sleep
must be extended by the pad length or the frame's tail will be truncated.

Run (from the repository root), one radio pair, IC-7300 TX -> IC-705 RX only:
    python experiments/hc2_32qam/diag_noise_leadin.py --trials 3
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
from experiments.hc2_32qam.benchmark_hc2_snr import frame_metrics
from whale.transport import RX_SAMPLE_RATE

UPSAMPLE_RATIO = hc2.SAMPLE_RATE // RX_SAMPLE_RATE  # 48000 / 12000 = 4

DEFAULT_LEADINS = (0.0, 1.0, 2.0)
DEFAULT_TRIALS = 3
DEFAULT_CAPTURE_TAIL = 1.5
DEFAULT_INTER_TRIAL = 0.5
# bench.noise_pad()'s default amplitude -- see its module-level PAD_AMPLITUDE
# comment for why 0.1 was chosen for AFSK. Not re-derived here; noted as a
# starting point rather than a validated choice for HC2.
NOISE_AMPLITUDE = bench.PAD_AMPLITUDE


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


def run_leadin_sweep(tx, rx, leadins, trials, seed, capture_tail, inter_trial,
                     amplitude):
    print(f"\n== Noise lead-in sweep (settling-transient hypothesis): "
          f"leadins={leadins}s, amplitude={amplitude} ==")
    rows = []
    for leadin_s in leadins:
        for trial in range(1, trials + 1):
            stale = rx.snapshot_rx()
            rx.consume_rx(len(stale))

            payload = _payload_for(seed, trial)
            frame_audio = hc2.modulate(payload)
            if leadin_s > 0:
                # noise_pad() uses bench.SAMPLE_RATE, which matches hc2's
                # 48 kHz native rate -- see module docstring.
                pad = bench.noise_pad(seconds=leadin_s, amplitude=amplitude)
                tx_audio = np.concatenate([pad, frame_audio])
            else:
                tx_audio = frame_audio

            keyed = tx.send(tx_audio)
            # The pad adds real keyed airtime ahead of the frame, so the
            # capture window needs to run that much longer or the frame's
            # tail gets truncated before hc2's correlation search sees it.
            time.sleep(capture_tail + leadin_s)
            captured_12k = rx.snapshot_rx()
            captured_48k = _upsample(captured_12k)

            decoded, diagnostics, metrics, error = _decode(captured_48k, payload)
            acq = diagnostics.get("acquisition_metric")
            evm = metrics.get("evm_percent")
            gain_min = metrics.get("channel_gain_min")
            gain_rms = metrics.get("channel_gain_rms")
            row = {
                "leadin_s": leadin_s, "trial": trial, "keyed_s": keyed,
                "decoded": bool(decoded), "error": error,
                "acquisition_metric": acq, "evm_percent": evm,
                "channel_gain_min": gain_min, "channel_gain_rms": gain_rms,
            }
            rows.append(row)
            acq_text = "n/a" if acq is None else f"{acq:.3f}"
            evm_text = "n/a" if evm is None else f"{evm:.1f}%"
            gain_text = ("n/a" if gain_min is None
                        else f"min={gain_min:.3f} rms={gain_rms:.3f}")
            print(f"  leadin={leadin_s:.1f}s trial={trial}/{trials}: "
                  f"keyed={keyed:.2f}s acq={acq_text} evm={evm_text} "
                  f"gain=({gain_text}) decoded={decoded}"
                  + (f" ({error})" if error else ""))
            time.sleep(inter_trial)

    print("\n  -- lead-in EVM summary --")
    print(f"  {'leadin_s':>8} {'n':>3} {'evm_mean%':>10} {'evm_min%':>9} "
          f"{'evm_max%':>9} {'gain_min_mean':>13} {'gain_rms_mean':>13} {'decoded':>8}")
    for leadin_s in leadins:
        subset = [r for r in rows if r["leadin_s"] == leadin_s]
        evms = [r["evm_percent"] for r in subset if r["evm_percent"] is not None]
        gmins = [r["channel_gain_min"] for r in subset if r["channel_gain_min"] is not None]
        grms = [r["channel_gain_rms"] for r in subset if r["channel_gain_rms"] is not None]
        decoded_n = sum(1 for r in subset if r["decoded"])
        if evms:
            print(f"  {leadin_s:8.1f} {len(subset):3d} {np.mean(evms):10.1f} "
                  f"{np.min(evms):9.1f} {np.max(evms):9.1f} "
                  f"{np.mean(gmins) if gmins else float('nan'):13.3f} "
                  f"{np.mean(grms) if grms else float('nan'):13.3f} {decoded_n:8d}")
        else:
            print(f"  {leadin_s:8.1f} {len(subset):3d} {'n/a':>10} {'n/a':>9} "
                  f"{'n/a':>9} {'n/a':>13} {'n/a':>13} {decoded_n:8d}")
    return rows


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300", help="TX station (default: ic7300)")
    ap.add_argument("--b", default="ic705", help="RX station (default: ic705)")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS,
                    help="trials per lead-in duration")
    ap.add_argument("--leadins", type=float, nargs="+", default=list(DEFAULT_LEADINS),
                    help="lead-in noise durations in seconds to test")
    ap.add_argument("--amplitude", type=float, default=NOISE_AMPLITUDE,
                    help="lead-in noise amplitude (default: bench.PAD_AMPLITUDE)")
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    args = ap.parse_args(argv)

    print("HC2-32QAM noise lead-in diagnostic (undeclared mode, real hardware only)")
    print(f"radios: TX={args.a} -> RX={args.b}")

    with pair_factory(args.a, args.b, warmup=3.0) as (tx, rx):
        run_leadin_sweep(tx, rx, args.leadins, args.trials, args.seed,
                         args.capture_tail, args.inter_trial, args.amplitude)
    return 0


if __name__ == "__main__":
    sys.exit(main())
