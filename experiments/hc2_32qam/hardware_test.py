"""Direct radio-to-radio smoke test for the HC2-32QAM frame.

HC2-32QAM (`hc2_32qam.py`) has no `whale/modes/` registration, mode ID, or
manifest entry -- see `MODE_QUALIFICATION.md`'s HC2 section -- so
`scripts/sweep_modes.py` cannot drive it (`--modes` only resolves names
against the registered ladder). This script bypasses that registry entirely
and talks to `bench.radio_pair()` directly, the same rig `scripts/bench.py`
and `scripts/sweep_modes.py` share, encoding/decoding with the bare
`hc2_32qam.modulate`/`demodulate` functions instead of a `WaveformMode`.

One sample-rate wrinkle those wrappers normally hide: `RadioTransport`
captures at 48 kHz but decimates to `rx_audio.DECODE_SAMPLE_RATE` (12 kHz)
before `snapshot_rx()` returns it (see `whale/transport.py`). Registered
modes like HF3 carry a second FFT geometry designed natively at 12 kHz for
this reason. HC2-32QAM has only the one 48 kHz geometry, and its carriers
top out at 2,765.6 Hz -- comfortably inside the 12 kHz capture's 6 kHz
Nyquist -- so this script upsamples the capture 12 kHz -> 48 kHz with
`scipy.signal.resample_poly` before handing it to `demodulate()`. That is a
reasonable approximation, not a bit-exact 48 kHz capture; if HC2 is ever
promoted to a real mode it should get its own RX-native geometry instead.

This produces AT MOST provisional smoke evidence, never a qualification
result: `MODE_QUALIFICATION.md` requires a `logs/mode_qualification/`
campaign, >=40 retained-direction frames, and section 3's Monte Carlo/
Watterson gates before any promotion decision, none of which a handful of
manual trials satisfies.

Run (from the repository root):
    python experiments/hc2_32qam/hardware_test.py --a ic7300 --b ic705 --trials 3
"""

from __future__ import annotations

import argparse
import json
import subprocess
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
from scipy import signal

import bench
from experiments.hc2_32qam import hc2_32qam as hc2
from experiments.hc2_32qam.benchmark_hc2_snr import frame_metrics
from whale.transport import RX_SAMPLE_RATE

DEFAULT_TRIALS = 3
DEFAULT_CAPTURE_TAIL = 1.5
DEFAULT_INTER_TRIAL = 0.5
UPSAMPLE_RATIO = hc2.SAMPLE_RATE // RX_SAMPLE_RATE  # 48000 / 12000 = 4


def _git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_dirty():
    try:
        return bool(subprocess.run(["git", "status", "--porcelain"], check=True,
                                   capture_output=True, text=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def run_direction(tx, rx, direction, trials, seed, *, capture_dir,
                  capture_tail, inter_trial):
    payload_bytes = hc2.MAX_PAYLOAD_BYTES
    print(f"\n  {direction}: {trials} x {payload_bytes} B")
    records = []
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
        payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
        audio = hc2.modulate(payload)

        keyed = tx.send(audio)
        time.sleep(capture_tail)
        captured_12k = rx.snapshot_rx()
        # See the module docstring: the capture arrives decimated to
        # RX_SAMPLE_RATE and has to be brought back up to hc2's native
        # SAMPLE_RATE before its FFT bin math applies.
        captured_48k = signal.resample_poly(captured_12k, UPSAMPLE_RATIO, 1)

        try:
            result, diagnostics = hc2.demodulate(captured_48k, return_diagnostics=True)
            error = None
        except Exception as exc:
            result, diagnostics = None, {}
            error = f"{type(exc).__name__}: {exc}"

        decoded = result == payload
        outcome = "decoded" if decoded else ("crc_or_sync_fail" if error is None else "error")

        metrics = {}
        if error is None and diagnostics.get("start_sample") is not None:
            try:
                metrics = frame_metrics(captured_48k, diagnostics, payload)
            except Exception as exc:
                metrics = {"metrics_error": f"{type(exc).__name__}: {exc}"}

        capture_path = None
        if capture_dir is not None:
            safe_direction = "".join(c if c.isalnum() else "_" for c in direction)
            capture_path = str(capture_dir / f"hc2_32qam_{safe_direction}_{trial:03d}.npz")
            np.savez_compressed(capture_path,
                                audio_12k=captured_12k.astype(np.float32),
                                payload=np.frombuffer(payload, dtype=np.uint8))

        record = {
            "trial": trial, "direction": direction, "payload_bytes": payload_bytes,
            "keyed_seconds": keyed, "rx_samples_12k": len(captured_12k),
            "outcome": outcome, "error": error,
            "start_sample": diagnostics.get("start_sample"),
            "frequency_offset_hz": diagnostics.get("frequency_offset_hz"),
            "acquisition_metric": diagnostics.get("acquisition_metric"),
            "capture": capture_path,
            **metrics,
        }
        records.append(record)
        metric = diagnostics.get("acquisition_metric")
        metric_text = "n/a" if metric is None else f"{metric:.3f}"
        evm = metrics.get("evm_percent")
        gain_min = metrics.get("channel_gain_min")
        gain_rms = metrics.get("channel_gain_rms")
        evm_text = "" if evm is None else f" evm={evm:.1f}%"
        gain_text = ("" if gain_min is None else
                     f" gain_min={gain_min:.3f} gain_rms={gain_rms:.3f}")
        print(f"    {trial}/{trials}: keyed={keyed:.2f}s rx={len(captured_12k)} "
              f"metric={metric_text}{evm_text}{gain_text} {outcome}"
              + (f" ({error})" if error else ""))
        if trial != trials:
            time.sleep(inter_trial)
    return records


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300", help="station A radio (default: ic7300)")
    ap.add_argument("--b", default="ic705", help="station B radio (default: ic705)")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--direction", choices=("both", "ab", "ba"), default="ab")
    ap.add_argument("--capture", choices=("none", "all"), default="all")
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--output-dir", type=Path,
                    help="result.json and captures destination (default: timestamped logs path)")
    args = ap.parse_args(argv)
    if args.trials < 1:
        ap.error("--trials must be positive")
    if args.capture_tail < 0 or args.inter_trial < 0:
        ap.error("capture and inter-trial delays must be non-negative")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "mode_sweeps" / f"hc2_32qam-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    capture_dir = None
    if args.capture != "none":
        capture_dir = output_dir / "captures"
        capture_dir.mkdir(exist_ok=True)

    print(f"HC2-32QAM smoke test (undeclared mode, no mode ID / manifest entry)")
    print(f"radios A={args.a}, B={args.b}; seed={args.seed}; trials={args.trials}")
    print(f"frame: {hc2.MAX_PAYLOAD_BYTES} B payload, {hc2.FRAME_SECONDS:.3f}s @ "
          f"{hc2.SAMPLE_RATE} Hz; capture at {RX_SAMPLE_RATE} Hz, "
          f"upsampled x{UPSAMPLE_RATIO} before decode")

    records = []
    with pair_factory(args.a, args.b, warmup=3.0) as (transport_a, transport_b):
        if args.direction in ("both", "ab"):
            records.extend(run_direction(
                transport_a, transport_b, f"A:{args.a}->B:{args.b}",
                args.trials, args.seed, capture_dir=capture_dir,
                capture_tail=args.capture_tail, inter_trial=args.inter_trial))
        if args.direction in ("both", "ba"):
            records.extend(run_direction(
                transport_b, transport_a, f"B:{args.b}->A:{args.a}",
                args.trials, args.seed, capture_dir=capture_dir,
                capture_tail=args.capture_tail, inter_trial=args.inter_trial))

    decoded = sum(1 for r in records if r["outcome"] == "decoded")
    total = len(records)
    print(f"\n== RESULTS == {decoded}/{total} decoded")

    out = {
        "note": ("Provisional smoke evidence only. HC2-32QAM has no "
                "logs/mode_qualification/ campaign, no mode ID, and no manifest "
                "entry; this does not satisfy the >=40 retained-direction frame "
                "gate or any Monte Carlo/Watterson requirement in "
                "MODE_QUALIFICATION.md."),
        "channel": {"type": "hardware", "radio_a": args.a, "radio_b": args.b},
        "sample_rates": {"tx_hz": hc2.SAMPLE_RATE, "capture_hz": RX_SAMPLE_RATE,
                         "decode_hz": hc2.SAMPLE_RATE, "upsample_ratio": UPSAMPLE_RATIO},
        "seed": args.seed, "trials": records,
        "summary": {"decoded": decoded, "total": total},
        "metadata": {"git_commit": _git_commit(), "git_dirty": _git_dirty()},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if decoded == total else 1


if __name__ == "__main__":
    sys.exit(main())
