"""Paired production-adapter comparison of HC1 and HF2.

Each invocation covers one named Watterson condition at one or more injected
SNR/3 kHz points.  HC1 and HF2 receive channel objects built from the same
per-trial seed, so their Watterson oscillators/phases and AWGN generator start
from the same realization.  Their unequal frame lengths necessarily expose
different-duration suffixes of that realization.

Examples::

    python -m scripts.compare_hc1_hf2 --condition quiet \
        --points 0 3 6 9 12 15 18 21 --trials 20 --workers 8 \
        --out logs/scratch/hc1-hf2/quiet-exploratory.json
    python -m scripts.compare_hc1_hf2 --condition quiet \
        --points 6 9 --trials 300 --workers 8 \
        --out logs/mode_qualification/hf-ssb/hc1-hf2/2026-09-07/quiet.json

The comparison does not alter either waveform or any mode registry.  It calls
the production ``HC1`` and ``HF2`` adapters directly.  A channel-front-end
stage applies only a scalar gain, chosen independently for each encoded frame,
so both modes enter the otherwise identical channel at the requested RMS.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import platform
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import scipy

from whale import framing
from whale.channel import (AwgnChannel, ChannelChain, ChannelResult,
                           ClippingChannel, FilterChannel,
                           FrequencyOffsetChannel, SampleClockChannel,
                           SnrSpec, WattersonChannel, WattersonPath)
from whale.modes.hc1_mode import HC1
from whale.modes.hf2_mode import HF2
from whale.modes.hr0_mode import HR0
from whale.qualification import net_data_frame_metrics, run_frame_trial
from whale.trials import TrialOutcome, TrialRun


DEFAULT_SEED = 20260907
TARGET_RMS = 0.128
ACK_PAYLOAD_BYTES = 12
QUALIFICATION_TRIALS = 300
FER_CEILING = 0.10
ACQUISITION_FLOOR = 0.90


@dataclass(frozen=True)
class Condition:
    name: str
    delay_seconds: float
    doppler_spread_hz: float

    def paths(self):
        return (WattersonPath(0.0, self.doppler_spread_hz),
                WattersonPath(self.delay_seconds, self.doppler_spread_hz))


CONDITIONS = {
    "static": Condition("static", 0.0001, 0.005),
    "quiet": Condition("quiet", 0.0005, 0.1),
    "moderate": Condition("moderate", 0.001, 0.5),
    "disturbed": Condition("disturbed", 0.002, 1.0),
}


class RmsNormalizationChannel:
    """Apply one frame-wide scalar without changing waveform geometry."""

    def __init__(self, sample_rate: int, target_rms: float):
        self.sample_rate = int(sample_rate)
        self.target_rms = float(target_rms)
        self.reset()

    def reset(self):
        self._gain = 1.0

    def process(self, audio):
        samples = np.asarray(audio, dtype=np.float64)
        rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
        self._gain = self.target_rms / rms if rms else 1.0
        return ChannelResult((samples * self._gain).astype(np.float32), {
            "input_rms": rms, "target_rms": self.target_rms,
            "voltage_gain": self._gain,
        })

    def drain(self, audio=None):
        samples = (np.zeros(0, np.float32) if audio is None
                   else np.asarray(audio, dtype=np.float32))
        return ChannelResult((samples * self._gain).astype(np.float32), {
            "continuation": True, "voltage_gain": self._gain,
        })

    def describe(self):
        return {"type": "frame_rms_normalization",
                "sample_rate": self.sample_rate,
                "target_rms": self.target_rms}


def paired_seed(master_seed: int, condition_index: int, point_index: int,
                trial: int) -> int:
    """A mode-independent seed for paired channel realizations."""

    sequence = np.random.SeedSequence(
        [master_seed, condition_index, point_index, trial])
    return int(sequence.generate_state(1, dtype=np.uint64)[0])


def make_channel(condition: Condition, snr_db: float, seed: int):
    """Build the fixed comparison path; only Watterson and SNR vary."""

    rate = 48_000
    return ChannelChain((
        RmsNormalizationChannel(rate, TARGET_RMS),
        FilterChannel(rate, low_hz=250.0, high_hz=3_100.0),
        ClippingChannel(rate, 0.95),
        FrequencyOffsetChannel(rate, 8.0, 0.05),
        WattersonChannel(rate, condition.paths(), seed,
                         preset_name=condition.name),
        AwgnChannel(rate, SnrSpec(snr_db), seed ^ 0x4157474E),
        FilterChannel(rate, low_hz=250.0, high_hz=3_100.0),
        SampleClockChannel(rate, 20.0),
    ))


def _worker(task):
    (mode_name, condition_name, snr_db, seed, trial, direction) = task
    mode = {"hc1": HC1, "hf2": HF2}[mode_name]
    condition = CONDITIONS[condition_name]
    return run_frame_trial(
        mode, make_channel(condition, snr_db, seed), seed, trial, direction,
        payload_bytes=framing.AIR_HEADER_BYTES + mode.chunk_size)


def wilson_interval(successes: int, total: int,
                    z: float = 1.959963984540054) -> list[float]:
    if total <= 0:
        raise ValueError("Wilson interval requires a positive total")
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z / denominator * math.sqrt(
        p * (1 - p) / total + z * z / (4 * total * total))
    return [centre - margin, centre + margin]


def proportion(successes: int, total: int) -> dict:
    return {"count": successes, "total": total,
            "rate": successes / total,
            "wilson_95": wilson_interval(successes, total)}


def failure_mechanism(trial) -> str:
    """Return one exclusive diagnostic bucket for a failed trial."""

    if trial.outcome is TrialOutcome.DECODED:
        return "decoded"
    if trial.outcome is TrialOutcome.ERROR:
        return "exception"
    metrics = trial.decoder_metrics
    failure = str(metrics.get("failure") or "").lower()
    if "incomplete" in failure or "short" in failure:
        return "incomplete_capture"
    if trial.outcome is TrialOutcome.ACQUISITION_FAILED:
        return "acquisition_failure"
    if metrics.get("fec_tail_ok") is False:
        return "payload_fec_failure"
    if metrics.get("crc_ok") is False or "crc" in failure:
        return "crc_failure"
    return "payload_other_failure"


def gate_status(total: int, acquisition: dict, fer: dict,
                errors: int) -> str:
    """Pass, confirmed-fail, or unresolved under the documented gates."""

    if errors:
        return "confirmed_fail"
    if total < QUALIFICATION_TRIALS:
        return "exploratory"
    if (fer["wilson_95"][1] <= FER_CEILING
            and acquisition["wilson_95"][0] >= ACQUISITION_FLOOR):
        return "pass"
    if (fer["wilson_95"][0] > FER_CEILING
            or acquisition["wilson_95"][1] < ACQUISITION_FLOOR):
        return "confirmed_fail"
    return "unresolved"


def summarize(mode, trials, ack_seconds: float) -> dict:
    total = len(trials)
    acquired_count = sum(t.outcome in (
        TrialOutcome.DECODED, TrialOutcome.PAYLOAD_FAILED) for t in trials)
    delivered = sum(t.decoded for t in trials)
    errors = sum(t.outcome is TrialOutcome.ERROR for t in trials)
    acquisition = proportion(acquired_count, total)
    delivery = proportion(delivered, total)
    fer = proportion(total - delivered, total)
    frame_seconds = trials[0].keyed_seconds
    chunk_bits = mode.chunk_size * 8
    mechanisms = {name: 0 for name in (
        "decoded", "acquisition_failure", "incomplete_capture",
        "payload_fec_failure", "crc_failure", "payload_other_failure",
        "exception")}
    for trial in trials:
        mechanisms[failure_mechanism(trial)] += 1
    delivered_goodput = chunk_bits * delivered / (total * frame_seconds)
    stop_wait_seconds = total * frame_seconds + delivered * ack_seconds
    stop_wait_goodput = (chunk_bits * delivered / stop_wait_seconds
                         if stop_wait_seconds else 0.0)
    return {
        "mode_id": mode.mode_id, "mode_name": mode.name,
        "data_chunk_bytes": mode.chunk_size,
        "physical_payload_bytes": framing.AIR_HEADER_BYTES + mode.chunk_size,
        "trials": total, "acquisition": acquisition,
        "frame_success": delivery, "frame_error_rate": fer,
        "error_count": errors, "failure_mechanisms": mechanisms,
        "frame_seconds": frame_seconds,
        "nominal_net_application_bps": chunk_bits / frame_seconds,
        "delivered_frame_goodput_bps": delivered_goodput,
        "stop_wait_goodput_bps": stop_wait_goodput,
        "qualification_gate": gate_status(total, acquisition, fer, errors),
    }


def git_state() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                                capture_output=True, text=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain"], check=True,
                                capture_output=True, text=True).stdout.splitlines()
        return {"commit": commit, "dirty": bool(status),
                "porcelain": status}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None, "porcelain": []}


def source_hashes() -> dict[str, str]:
    paths = (Path("whale/modes/hc1_mode.py"), Path("whale/modes/hc1.py"),
             Path("whale/modes/hf2_mode.py"), Path("whale/phy/hf2.py"))
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def run(args) -> dict:
    condition = CONDITIONS[args.condition]
    modes = (HC1, HF2)
    ack_seconds = len(HR0.encode(bytes(ACK_PAYLOAD_BYTES))) / HR0.tx_sample_rate
    tasks = []
    for point_index, point in enumerate(args.points):
        for trial in range(1, args.trials + 1):
            seed = paired_seed(args.seed, list(CONDITIONS).index(args.condition),
                               point_index, trial)
            for mode in modes:
                direction = f"{condition.name}, SNR/3 kHz {point:g} dB"
                tasks.append((mode.name, condition.name, point, seed, trial,
                              direction))

    executor = (concurrent.futures.ProcessPoolExecutor(max_workers=args.workers)
                if args.workers > 1 else None)
    try:
        records = (list(executor.map(_worker, tasks)) if executor else
                   [_worker(task) for task in tasks])
    finally:
        if executor:
            executor.shutdown()

    summaries = []
    for point in args.points:
        label = f"{condition.name}, SNR/3 kHz {point:g} dB"
        for mode in modes:
            selected = [r for r in records
                        if r.direction == label and r.mode_id == mode.mode_id]
            row = {"condition": condition.name, "point_db": point,
                   **summarize(mode, selected, ack_seconds)}
            summaries.append(row)
            print(f"{mode.name} {label}: "
                  f"{row['frame_success']['count']}/{row['trials']} delivered, "
                  f"acq {row['acquisition']['count']}/{row['trials']}, "
                  f"FER UB {row['frame_error_rate']['wilson_95'][1]:.4f}, "
                  f"{row['qualification_gate']}")

    channel_description = dict(make_channel(
        condition, args.points[0], args.seed).describe())
    run_document = TrialRun(
        channel={"type": "paired_hf_comparison", "condition": asdict(condition),
                 "points_db_snr_3khz": args.points,
                 "expanded_example": channel_description},
        trials=records, seed=args.seed,
        metadata={
            "benchmark": "hc1_hf2_paired_production_adapters",
            "completed_utc": datetime.now(timezone.utc).isoformat(),
            "exact_command": shlex.join([sys.executable, "-m",
                                          "scripts.compare_hc1_hf2",
                                          *sys.argv[1:]]),
            "trials_per_mode_point": args.trials,
            "worker_processes": args.workers,
            "pairing": ("mode-independent SeedSequence([master_seed, "
                        "condition_index, point_index, trial]); both modes use "
                        "the same Watterson and AWGN base seed"),
            "snr_convention": ("signal power / noise power in a 3000 Hz "
                               "reference bandwidth; no historical "
                               "waveform_snr_db values used"),
            "tx_rms_target": TARGET_RMS,
            "mode_geometry": {str(mode.mode_id): net_data_frame_metrics(mode)
                              for mode in modes},
            "mode_source_sha256": source_hashes(),
            "hr0_data_ack": {
                "physical_payload_bytes": ACK_PAYLOAD_BYTES,
                "minimum_airtime_seconds": ack_seconds,
                "assumptions": ("one minimum HR0 DATA_ACK after each successful "
                                "DATA frame; no turnaround, propagation delay, "
                                "retries, or ACK losses")},
            "qualification_gates": {
                "minimum_trials": QUALIFICATION_TRIALS,
                "fer_wilson_95_upper_max": FER_CEILING,
                "acquisition_wilson_95_lower_min": ACQUISITION_FLOOR,
                "errors_allowed": 0},
            "summaries": summaries,
            "git": git_state(),
            "software": {"python": sys.version, "numpy": np.__version__,
                         "scipy": scipy.__version__},
            "machine": {"platform": platform.platform(),
                        "machine": platform.machine(),
                        "processor": platform.processor(),
                        "cpu_count": os.cpu_count()},
        })
    document = run_document.to_dict()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
    print(f"wrote {args.out}")
    return document


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--condition", choices=CONDITIONS, required=True)
    parser.add_argument("--points", type=float, nargs="+", required=True)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.trials < 1:
        parser.error("--trials must be positive")
    if args.workers < 1:
        parser.error("--workers must be positive")
    return args


def main(argv=None) -> int:
    run(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
