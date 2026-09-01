"""Sweep every registered mode directly over a pair of radios.

This is a physical-layer benchmark: it bypasses Link, ARQ, negotiation and
sockets, sends deterministic full-capacity packets, and checks each capture
byte-for-byte. The selected ChannelPolicy supplies the ordered mode registry,
so adding a rung to a ladder automatically adds it to this sweep.

Examples:
    python scripts/sweep_modes.py --channel vhf-fm
    python scripts/sweep_modes.py --channel hf-ssb --trials 10
    python scripts/sweep_modes.py --channel hf-ssb --modes hc0 --direction ab
    python scripts/sweep_modes.py --channel hf-ssb --mode-level experimental --modes hf3 --direction ab
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Direct script execution puts scripts/, not the repository root, on
# sys.path. Experimental modes currently live under experiments/ and are
# intentionally not part of the installed package, so expose the source root
# before importing the qualification registry that lazily loads them.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

import bench
from whale import framing, mode_qualification, policy
from whale.trials import (TrialOutcome, TrialResult, TrialRun,
                          classify_decode, common_decoder_metrics)


DEFAULT_SEED = 20260829
DEFAULT_TRIALS = 5
DEFAULT_CAPTURE_TAIL = 1.5
DEFAULT_INTER_TRIAL = 0.5
DEFAULT_RADIOS = {
    "vhf-fm": ("ic705", "ht"),
    "hf-ssb": ("ic7300", "ic705"),
}


def registry_for(channel_name, mode_level="default"):
    channel_policy = policy.by_name(channel_name)
    if mode_level == "default":
        return channel_policy.mode_ladder(channel_policy.max_useful_frame_seconds)
    return mode_qualification.registry(
        channel_name, mode_level, channel_policy.max_useful_frame_seconds)


def select_modes(registry, requested):
    if not requested:
        return tuple(registry.modes)
    by_name = {mode.name: mode for mode in registry.modes}
    by_id = {str(mode.mode_id): mode for mode in registry.modes}
    selected = []
    for value in requested:
        mode = by_name.get(value, by_id.get(value))
        if mode is None:
            choices = ", ".join(f"{mode.name} ({mode.mode_id})"
                                for mode in registry.modes)
            raise ValueError(f"mode {value!r} is not in this channel; have {choices}")
        if mode not in selected:
            selected.append(mode)
    return tuple(selected)


def full_packet_bytes(mode):
    """Largest link packet this mode carries: air header plus DATA chunk."""
    return framing.AIR_HEADER_BYTES + mode.chunk_size


def _capture_path(capture_dir, mode, direction, trial, captured, payload):
    safe_direction = "".join(character if character.isalnum() else "_"
                             for character in direction)
    path = capture_dir / f"{mode.mode_id}_{mode.name}_{safe_direction}_{trial:03d}.npz"
    np.savez_compressed(path, audio=np.asarray(captured, dtype=np.float32),
                        payload=np.frombuffer(payload, dtype=np.uint8))
    return str(path)


def run_direction(tx, rx, mode, direction, trials, seed, *, capture_dir=None,
                  capture="failures", capture_tail=DEFAULT_CAPTURE_TAIL,
                  inter_trial=DEFAULT_INTER_TRIAL, sleep=time.sleep):
    records = []
    direction_code = 0 if direction.startswith("A:") else 1
    payload_bytes = full_packet_bytes(mode)
    print(f"\n  {direction}: {trials} x {payload_bytes} B")
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        rng = np.random.default_rng(np.random.SeedSequence(
            [seed, mode.mode_id, direction_code, trial]))
        payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
        audio = np.zeros(0, np.float32)
        captured = np.zeros(0, np.float32)
        keyed = 0.0
        result = {}
        error = None
        try:
            audio = np.asarray(mode.encode(payload), dtype=np.float32)
            keyed = float(tx.send(audio))
            sleep(capture_tail)
            captured = rx.snapshot_rx()
            result = mode.decode(captured)
            outcome = classify_decode(result, payload, mode.confidence_threshold)
        except Exception as exc:
            outcome = TrialOutcome.ERROR
            error = f"{type(exc).__name__}: {exc}"

        save = capture_dir is not None and (
            capture == "all" or (capture == "failures" and outcome is not TrialOutcome.DECODED))
        capture_path = (_capture_path(capture_dir, mode, direction, trial,
                                      captured, payload) if save else None)
        record = TrialResult(
            trial=trial, direction=direction, mode_id=mode.mode_id,
            mode_name=mode.name, payload_bytes=payload_bytes, outcome=outcome,
            tx_samples=len(audio), tx_sample_rate=mode.tx_sample_rate,
            rx_samples=len(captured), rx_sample_rate=mode.rx_sample_rate,
            keyed_seconds=keyed,
            decoder_metrics=common_decoder_metrics(result, captured),
            capture=capture_path, error=error)
        records.append(record)
        confidence = result.get("confidence")
        confidence_text = "n/a" if confidence is None else f"{float(confidence):.3f}"
        print(f"    {trial}/{trials}: keyed={keyed:.2f}s rx={len(captured)} "
              f"confidence={confidence_text} {outcome.value}"
              + (f" ({error})" if error else ""))
        if trial != trials:
            sleep(inter_trial)
    return records


def wilson_interval(passed, total, z=1.959963984540054):
    if total == 0:
        return 0.0, 0.0
    proportion = passed / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z / denominator * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total))
    return centre - margin, centre + margin


def summarize(records, data_chunk_bytes=None):
    data_chunk_bytes = data_chunk_bytes or {}
    groups = {}
    for record in records:
        key = (record.mode_id, record.mode_name, record.direction)
        groups.setdefault(key, []).append(record)
    rows = []
    for (mode_id, mode_name, direction), trials in groups.items():
        passed = sum(trial.decoded for trial in trials)
        low, high = wilson_interval(passed, len(trials))
        chunk_bytes = data_chunk_bytes.get(mode_id, trials[0].payload_bytes)
        payload = chunk_bytes * passed
        keyed = sum(trial.keyed_seconds for trial in trials)
        rows.append({
            "mode_id": mode_id, "mode_name": mode_name, "direction": direction,
            "passed": passed, "total": len(trials), "rate": passed / len(trials),
            "wilson_95": [low, high],
            "data_chunk_bytes": chunk_bytes,
            "useful_throughput_bps": 8 * payload / keyed if keyed else 0.0,
        })
    return rows


def _git_commit():
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True,
            text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_dirty():
    try:
        return bool(subprocess.run(
            ["git", "status", "--porcelain"], check=True, capture_output=True,
            text=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def _default_output_dir():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("logs") / "mode_sweeps" / stamp


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", choices=sorted(policy.CHANNELS), default="vhf-fm")
    ap.add_argument("--mode-level", choices=("default", "optional", "experimental"),
                    default="default",
                    help="highest qualification registry to expose (default: default)")
    ap.add_argument("--a", help="station A radio (channel-specific default)")
    ap.add_argument("--b", help="station B radio (channel-specific default)")
    ap.add_argument("--modes", nargs="+", metavar="MODE",
                    help="mode names or IDs (default: complete channel ladder)")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--direction", choices=("both", "ab", "ba"), default="both")
    ap.add_argument("--capture", choices=("none", "failures", "all"),
                    default="failures")
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--required-rate", type=float, default=1.0,
                    help="minimum success fraction for a passing exit status")
    ap.add_argument("--output-dir", type=Path,
                    help="result.json and captures destination (default: timestamped logs path)")
    args = ap.parse_args(argv)
    if args.trials < 1:
        ap.error("--trials must be positive")
    if not 0 <= args.required_rate <= 1:
        ap.error("--required-rate must be between zero and one")
    if args.capture_tail < 0 or args.inter_trial < 0:
        ap.error("capture and inter-trial delays must be non-negative")

    registry = registry_for(args.channel, args.mode_level)
    try:
        selected = select_modes(registry, args.modes)
    except ValueError as exc:
        ap.error(str(exc))
    default_a, default_b = DEFAULT_RADIOS[args.channel]
    radio_a, radio_b = args.a or default_a, args.b or default_b
    output_dir = args.output_dir or _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    capture_dir = None
    if args.capture != "none":
        capture_dir = output_dir / "captures"
        capture_dir.mkdir(exist_ok=True)

    print(f"channel {args.channel}: "
          + " -> ".join(f"{mode.name}({mode.mode_id})" for mode in registry.modes))
    print(f"radios A={radio_a}, B={radio_b}; seed={args.seed}; trials={args.trials}")
    records = []
    with pair_factory(radio_a, radio_b, warmup=3.0) as (transport_a, transport_b):
        for mode in selected:
            if mode.tx_sample_rate != 48_000 or mode.rx_sample_rate != 12_000:
                raise RuntimeError(
                    f"{mode.name} uses {mode.tx_sample_rate}/{mode.rx_sample_rate} Hz; "
                    "the hardware transport uses 48000/12000 Hz")
            print(f"\n== {mode.name} (ID {mode.mode_id}, DATA chunk {mode.chunk_size} B) ==")
            if args.direction in ("both", "ab"):
                records.extend(run_direction(
                    transport_a, transport_b, mode, f"A:{radio_a}->B:{radio_b}",
                    args.trials, args.seed, capture_dir=capture_dir,
                    capture=args.capture, capture_tail=args.capture_tail,
                    inter_trial=args.inter_trial))
            if args.direction in ("both", "ba"):
                records.extend(run_direction(
                    transport_b, transport_a, mode, f"B:{radio_b}->A:{radio_a}",
                    args.trials, args.seed, capture_dir=capture_dir,
                    capture=args.capture, capture_tail=args.capture_tail,
                    inter_trial=args.inter_trial))

    rows = summarize(records, {mode.mode_id: mode.chunk_size for mode in selected})
    print("\n== RESULTS ==")
    for row in rows:
        low, high = row["wilson_95"]
        print(f"{row['mode_name']:>10} {row['direction']}: "
              f"{row['passed']}/{row['total']} ({row['rate'] * 100:.1f}%), "
              f"95% CI {low * 100:.1f}-{high * 100:.1f}%, "
              f"{row['useful_throughput_bps']:.0f} useful bit/s")

    run = TrialRun(
        channel={"type": "hardware", "policy": args.channel,
                 "radio_a": radio_a, "radio_b": radio_b},
        trials=records, seed=args.seed,
        metadata={"git_commit": _git_commit(), "git_dirty": _git_dirty(),
                  "mode_level": args.mode_level,
                  "registry_mode_ids": list(registry.supported_ids),
                  "selected_mode_ids": [mode.mode_id for mode in selected],
                  "trials_per_direction": args.trials,
                  "capture": args.capture, "summary_by_mode_direction": rows})
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(run.to_dict(), indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if all(row["rate"] >= args.required_rate for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
