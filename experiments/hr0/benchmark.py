#!/usr/bin/env python3
"""Matched, replayable frame benchmark for HC0 and future HR0 candidates.

This runner is intentionally experiment-local.  It does not register HR0 or
change a production channel.  A future candidate can be supplied as
``package.module:OBJECT`` when it exposes the ordinary WaveformMode surface.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib
import inspect
import json
import math
import os
import platform
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import scipy

from whale import framing, rx_audio
from whale.channel import (AwgnChannel, SnrKind, SnrSpec, WATTERSON_PRESETS,
                           WattersonChannel, waveform_power)
from whale.trials import (TrialOutcome, classify_decode,
                          common_decoder_metrics)


SCHEMA_ID = "whalemodem.hr0.frame_benchmark"
SCHEMA_VERSION = 2
SAMPLE_RATE = 48_000
PHYSICAL_PAYLOAD_BYTES = 64
AIR_HEADER_BYTES = framing.AIR_HEADER_BYTES
DATA_BODY_BYTES = PHYSICAL_PAYLOAD_BYTES - AIR_HEADER_BYTES
MODELS = ("awgn", "watterson_canonical", "watterson_fixed_n0")
DEFAULT_MASTER_SEED = 20260830


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_point_key(model: str, preset: str | None, snr_db: float) -> str:
    """Stable point identity, independent of CLI point ordering."""

    return f"{model}|{preset or '-'}|{float(snr_db):.12g}"


def derive_seed(master_seed: int, namespace: str, point_key: str,
                trial: int) -> int:
    """Derive a stable 63-bit seed shared by every matched mode."""

    encoded = json.dumps(
        [int(master_seed), namespace, point_key, int(trial)],
        separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big") & (
        (1 << 63) - 1)


def wilson_interval(successes: int, total: int,
                    z: float = 1.959963984540054) -> list[float] | None:
    if total == 0:
        return None
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2.0 * total)) / denominator
    margin = z / denominator * math.sqrt(
        proportion * (1.0 - proportion) / total
        + z * z / (4.0 * total * total))
    return [centre - margin, centre + margin]


def proportion(successes: int, total: int) -> dict:
    return {
        "count": successes,
        "total": total,
        "rate": successes / total if total else None,
        "wilson_95": wilson_interval(successes, total),
    }


def load_mode(selector: str):
    if selector == "hc0":
        from whale.modes.hc0_mode import HC0
        return HC0
    if ":" not in selector:
        raise ValueError(
            f"unknown mode {selector!r}; use hc0 or package.module:OBJECT")
    module_name, attribute = selector.rsplit(":", 1)
    return getattr(importlib.import_module(module_name), attribute)


def source_metadata(mode) -> dict:
    source = inspect.getsourcefile(type(mode)) or inspect.getsourcefile(mode)
    if source is None:
        return {"path": None, "sha256": None}
    path = Path(source).resolve()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def payload_for(seed: int,
                physical_payload_bytes: int = PHYSICAL_PAYLOAD_BYTES) -> bytes:
    return np.random.default_rng(seed).integers(
        0, 256, physical_payload_bytes, dtype=np.uint8).tobytes()


def _occupied_bandwidth_99(audio: np.ndarray, sample_rate: int) -> dict:
    """Return the 0.5--99.5% cumulative one-sided power interval."""

    values = np.asarray(audio, dtype=np.float64)
    spectrum = np.fft.rfft(values)
    power = np.abs(spectrum) ** 2
    cumulative = np.cumsum(power)
    if not len(power) or cumulative[-1] <= 0:
        return {"low_hz": 0.0, "high_hz": 0.0, "width_hz": 0.0,
                "definition": "0.5_to_99.5_percent_cumulative_rfft_power"}
    frequencies = np.fft.rfftfreq(len(values), 1.0 / sample_rate)
    low = float(frequencies[np.searchsorted(cumulative, .005 * cumulative[-1])])
    high = float(frequencies[np.searchsorted(cumulative, .995 * cumulative[-1])])
    return {"low_hz": low, "high_hz": high, "width_hz": high - low,
            "definition": "0.5_to_99.5_percent_cumulative_rfft_power"}


def mode_metadata(selector: str, master_seed: int,
                  physical_payload_bytes: int = PHYSICAL_PAYLOAD_BYTES,
                  useful_application_bytes: int | None = None) -> dict:
    mode = load_mode(selector)
    if useful_application_bytes is None:
        useful_application_bytes = physical_payload_bytes - AIR_HEADER_BYTES
    point_key = canonical_point_key("metadata", None, 0.0)
    payload = payload_for(
        derive_seed(master_seed, "workload", point_key, 0),
        physical_payload_bytes)
    transmitted = np.asarray(mode.encode(payload), dtype=np.float32)
    squared = np.asarray(transmitted, dtype=np.float64) ** 2
    keyed_seconds = len(transmitted) / mode.tx_sample_rate
    useful_rate = useful_application_bytes * 8 / keyed_seconds
    declared_airtime = None
    try:
        declared_airtime = float(mode.airtime(physical_payload_bytes))
    except Exception:
        pass
    rms = float(np.sqrt(np.mean(squared)))
    peak = float(np.max(np.abs(transmitted)))
    return {
        "selector": selector,
        "name": str(mode.name),
        "mode_id": int(mode.mode_id),
        "candidate_revision": source_metadata(mode),
        "tx_sample_rate": int(mode.tx_sample_rate),
        "rx_sample_rate": int(mode.rx_sample_rate),
        "confidence_threshold": float(mode.confidence_threshold),
        "physical_payload_bytes": physical_payload_bytes,
        "air_header_bytes": AIR_HEADER_BYTES,
        "data_body_bytes": physical_payload_bytes - AIR_HEADER_BYTES,
        "useful_application_bytes": useful_application_bytes,
        "tx_samples": len(transmitted),
        "snr_reference_samples": [0, len(transmitted)],
        "keyed_seconds": keyed_seconds,
        "declared_airtime_seconds": declared_airtime,
        "frame_useful_rate_bps": useful_rate,
        "waveform_snr_to_useful_eb_n0_offset_db": 10.0 * math.log10(
            (mode.tx_sample_rate / 2.0) / useful_rate),
        "mean_square_power": float(np.mean(squared)),
        "energy_mean_square_seconds": float(np.sum(squared) / mode.tx_sample_rate),
        "energy_per_useful_bit": float(
            np.sum(squared) / mode.tx_sample_rate
            / (useful_application_bytes * 8)),
        "rms": rms,
        "peak": peak,
        "crest_factor": peak / rms,
        "occupied_bandwidth_99": _occupied_bandwidth_99(
            transmitted, mode.tx_sample_rate),
    }


def _snr_spec(snr_db: float, tx_samples: int) -> SnrSpec:
    return SnrSpec(float(snr_db), SnrKind.WAVEFORM,
                   reference_start=0, reference_stop=tx_samples)


def _add_fixed_noise(audio: np.ndarray, *, sample_rate: int, noise_power: float,
                     seed: int, snr_db: float, reference_samples: int,
                     calibration_power: float) -> tuple[np.ndarray, dict, dict]:
    """Add fixed-variance real AWGN without pretending it is AwgnChannel."""

    samples = np.asarray(audio, dtype=np.float64)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, math.sqrt(noise_power), len(samples))
    reference = slice(0, reference_samples)
    realized_noise = float(np.mean(noise[reference] ** 2))
    faded_power = float(np.mean(samples[reference] ** 2))
    measurements = {
        "requested_unfaded_waveform_snr_db": float(snr_db),
        "unfaded_calibration_signal_power": calibration_power,
        "fixed_noise_power": noise_power,
        "realized_noise_power": realized_noise,
        "faded_signal_power_in_reference": faded_power,
        "realized_post_fade_waveform_snr_db": float(
            10.0 * np.log10(faded_power / realized_noise))
            if faded_power > 0 and realized_noise > 0 else None,
        "reference_samples": [0, reference_samples],
    }
    description = {
        "type": "fixed_power_awgn_experiment_local",
        "sample_rate": sample_rate,
        "seed": seed,
        "noise_power": noise_power,
        "calibration": "unfaded_tx_complete_keying_mean_square",
        "requested_unfaded_waveform_snr_db": float(snr_db),
        "reference_samples": [0, reference_samples],
        "noise_band_hz": [0.0, sample_rate / 2.0],
    }
    return (samples + noise).astype(np.float32), measurements, description


def apply_channel(task: Mapping[str, object], transmitted: np.ndarray):
    model = str(task["model"])
    snr_db = float(task["snr_db"])
    tx_samples = len(transmitted)
    awgn_seed = int(task["derived_seeds"]["awgn"])
    watterson_seed = int(task["derived_seeds"]["watterson"])
    spec = _snr_spec(snr_db, tx_samples)
    if model == "awgn":
        awgn = AwgnChannel(SAMPLE_RATE, spec, awgn_seed)
        result = awgn.process(transmitted)
        return result.audio, {"stage_0": dict(result.measurements)}, {
            "type": "chain", "sample_rate": SAMPLE_RATE,
            "stages": [dict(awgn.describe())],
            "stage_order": ["awgn"],
            "snr_reference_samples": [0, tx_samples],
        }

    preset = str(task["preset"])
    fading = WattersonChannel.from_preset(
        SAMPLE_RATE, preset, watterson_seed)
    faded = fading.process(transmitted)
    if model == "watterson_canonical":
        awgn = AwgnChannel(SAMPLE_RATE, spec, awgn_seed)
        noisy = awgn.process(faded.audio)
        return noisy.audio, {
            "stage_0": dict(faded.measurements),
            "stage_1": dict(noisy.measurements),
        }, {
            "type": "chain", "sample_rate": SAMPLE_RATE,
            "stages": [dict(fading.describe()), dict(awgn.describe())],
            "stage_order": ["watterson", "awgn"],
            "normalization": "post_watterson_per_frame",
            "fading_continuity": "independent_reset_per_frame",
            "snr_reference_samples": [0, tx_samples],
        }
    if model == "watterson_fixed_n0":
        calibration_power = waveform_power(transmitted, spec)
        noise_power = calibration_power / 10.0 ** (snr_db / 10.0)
        noisy, noise_measurements, noise_description = _add_fixed_noise(
            faded.audio, sample_rate=SAMPLE_RATE, noise_power=noise_power,
            seed=awgn_seed, snr_db=snr_db, reference_samples=tx_samples,
            calibration_power=calibration_power)
        return noisy, {
            "stage_0": dict(faded.measurements),
            "stage_1": noise_measurements,
        }, {
            "type": "chain", "sample_rate": SAMPLE_RATE,
            "stages": [dict(fading.describe()), noise_description],
            "stage_order": ["watterson", "fixed_power_awgn_experiment_local"],
            "normalization": "unfaded_tx_calibration_fixed_within_frame",
            "fading_continuity": "independent_reset_per_frame_not_continuous",
            "snr_reference_samples": [0, tx_samples],
        }
    raise ValueError(f"unknown model {model!r}")


def execute_trial(task: Mapping[str, object], *, include_capture: bool = False):
    mode = load_mode(str(task["mode_selector"]))
    # Defaults retain deterministic replay of schema-v1 full-frame artifacts.
    physical_payload_bytes = int(task.get(
        "physical_payload_bytes", PHYSICAL_PAYLOAD_BYTES))
    useful_application_bytes = int(task.get(
        "useful_application_bytes",
        physical_payload_bytes - AIR_HEADER_BYTES))
    payload = payload_for(int(task["derived_seeds"]["workload"]),
                          physical_payload_bytes)
    transmitted = np.zeros(0, np.float32)
    channel_audio = np.zeros(0, np.float32)
    captured = np.zeros(0, np.float32)
    channel_measurements: dict = {}
    channel_description: dict = {}
    decoder_result: dict = {}
    error = None
    trial_wall_start = time.perf_counter()
    trial_cpu_start = time.process_time()
    decoder_wall = None
    decoder_cpu = None
    try:
        transmitted = np.asarray(mode.encode(payload), dtype=np.float32)
        if len(transmitted) <= 0 or mode.tx_sample_rate != SAMPLE_RATE:
            raise ValueError("benchmark requires a non-empty 48 kHz waveform")
        channel_audio, channel_measurements, channel_description = apply_channel(
            task, transmitted)
        captured = rx_audio.downsample(np.concatenate((
            np.asarray(channel_audio, dtype=np.float32),
            np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32),
        )))
        decoder_wall_start = time.perf_counter()
        decoder_cpu_start = time.process_time()
        decoder_result = mode.decode(captured)
        decoder_cpu = time.process_time() - decoder_cpu_start
        decoder_wall = time.perf_counter() - decoder_wall_start
        outcome = classify_decode(
            decoder_result, payload, float(mode.confidence_threshold))
    except Exception as exc:
        outcome = TrialOutcome.ERROR
        error = f"{type(exc).__name__}: {exc}"
    trial_cpu = time.process_time() - trial_cpu_start
    trial_wall = time.perf_counter() - trial_wall_start
    confidence = decoder_result.get("confidence")
    acquired = (confidence is not None
                and float(confidence) >= float(mode.confidence_threshold))
    returned_payload = decoder_result.get("payload")
    checked_wrong_payload = (returned_payload is not None
                             and returned_payload != payload)
    record = {
        "trial": int(task["trial"]),
        "mode_selector": str(task["mode_selector"]),
        "mode_name": str(mode.name),
        "mode_id": int(mode.mode_id),
        "point_key": str(task["point_key"]),
        "model": str(task["model"]),
        "preset": task["preset"],
        "waveform_snr_db": float(task["snr_db"]),
        "derived_seeds": dict(task["derived_seeds"]),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "physical_payload_bytes": physical_payload_bytes,
        "air_header_bytes": AIR_HEADER_BYTES,
        "data_body_bytes": physical_payload_bytes - AIR_HEADER_BYTES,
        "useful_application_bytes": useful_application_bytes,
        "outcome": outcome.value,
        "acquired_above_threshold": acquired,
        "checked_wrong_payload": checked_wrong_payload,
        "tx_samples": len(transmitted),
        "tx_sample_rate": int(mode.tx_sample_rate),
        "snr_reference_samples": [0, len(transmitted)],
        "keyed_seconds": (len(transmitted) / mode.tx_sample_rate
                          if len(transmitted) else None),
        "rx_samples": len(captured),
        "rx_sample_rate": int(mode.rx_sample_rate),
        "channel_description": channel_description,
        "channel_measurements": channel_measurements,
        "decoder_metrics": {
            **common_decoder_metrics(decoder_result, captured),
            **{
                key: decoder_result[key]
                for key in (
                    "candidate_limit", "candidate_count", "candidates_tried",
                    "candidate_rank", "search_cells_evaluated",
                    "refinement_cells_evaluated",
                    "total_viterbi_branch_metrics", "body_tone_correlations",
                    "acquired_class", "pilot_correct", "pilot_symbols",
                    "viterbi_steps", "viterbi_states",
                    "viterbi_branch_metrics", "fec_tail_ok", "zero_fill_ok",
                    "gf16_viterbi_steps", "gf16_viterbi_states",
                    "gf16_viterbi_branches", "total_gf16_viterbi_branches",
                    "rs_ok", "rs_corrected_bytes", "likelihood_scale",
                    "observation_combining",
                    "capture_truncated_to_limit",
                )
                if key in decoder_result
            },
            "process_cpu_seconds": decoder_cpu,
            "wall_seconds": decoder_wall,
        },
        "trial_work": {
            "process_cpu_seconds": trial_cpu,
            "wall_seconds": trial_wall,
        },
        "error": error,
    }
    capture = None
    if include_capture:
        capture = {
            "expected_payload": np.frombuffer(payload, dtype=np.uint8),
            "transmitted": transmitted,
            "channel_audio": channel_audio,
            "decode_audio": captured,
        }
    return record, capture


def _execute_worker(task):
    return execute_trial(task)[0]


def summarize(records: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for record in records:
        key = (record["mode_selector"], record["point_key"])
        groups.setdefault(key, []).append(record)
    summaries = []
    for (selector, point_key), rows in groups.items():
        total = len(rows)
        acquired = sum(row["acquired_above_threshold"] for row in rows)
        delivered = sum(row["outcome"] == TrialOutcome.DECODED.value
                        for row in rows)
        delivered_after_acquisition = sum(
            row["outcome"] == TrialOutcome.DECODED.value
            and row["acquired_above_threshold"] for row in rows)
        payload_failed = sum(row["outcome"] == TrialOutcome.PAYLOAD_FAILED.value
                             for row in rows)
        acquisition_failed = sum(
            row["outcome"] == TrialOutcome.ACQUISITION_FAILED.value
            for row in rows)
        errors = sum(row["outcome"] == TrialOutcome.ERROR.value for row in rows)
        wrong = sum(row["checked_wrong_payload"] for row in rows)
        summaries.append({
            "mode_selector": selector,
            "mode_name": rows[0]["mode_name"],
            "mode_id": rows[0]["mode_id"],
            "point_key": point_key,
            "model": rows[0]["model"],
            "preset": rows[0]["preset"],
            "waveform_snr_db": rows[0]["waveform_snr_db"],
            "trial_count": total,
            "outcomes": {
                "decoded": delivered,
                "acquisition_failed": acquisition_failed,
                "payload_failed": payload_failed,
                "error": errors,
            },
            "acquisition_probability": proportion(acquired, total),
            "payload_success_conditional_on_acquisition": proportion(
                delivered_after_acquisition, acquired),
            "payload_failure_conditional_on_acquisition": proportion(
                payload_failed, acquired),
            "frame_error_rate": proportion(total - delivered, total),
            "verified_delivery_probability": proportion(delivered, total),
            "checked_wrong_payloads": wrong,
            "exploration_only": total < 100,
            "qualification_gate_eligible": total >= 100,
        })
    return summaries


def _git_metadata() -> dict:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True).stdout.strip()
        dirty_output = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=ROOT, check=True,
            capture_output=True, text=True).stdout.splitlines()
        return {"commit": commit, "dirty": bool(dirty_output),
                "dirty_paths": dirty_output}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None, "dirty_paths": []}


def _environment() -> dict:
    return {
        "python": sys.version,
        "python_executable": sys.executable,
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
    }


def build_tasks(args) -> list[dict]:
    tasks = []
    preset = args.watterson_preset if args.model != "awgn" else None
    for selector in args.modes:
        for snr_db in args.points:
            point_key = canonical_point_key(args.model, preset, snr_db)
            for trial in range(1, args.trials + 1):
                seeds = {
                    namespace: derive_seed(args.seed, namespace, point_key, trial)
                    for namespace in ("workload", "watterson", "awgn")
                }
                tasks.append({
                    "mode_selector": selector,
                    "model": args.model,
                    "preset": preset,
                    "snr_db": float(snr_db),
                    "point_key": point_key,
                    "trial": trial,
                    "physical_payload_bytes": args.payload_bytes,
                    "useful_application_bytes": args.useful_application_bytes,
                    "derived_seeds": seeds,
                })
    return tasks


def sweep(args) -> int:
    started = utc_now()
    for selector in args.modes:
        mode = load_mode(selector)
        if args.payload_bytes > AIR_HEADER_BYTES + int(mode.chunk_size):
            raise ValueError(
                f"{selector} cannot carry the {args.payload_bytes}-byte "
                "physical workload")
    tasks = build_tasks(args)
    if args.workers == 1:
        records = [_execute_worker(task) for task in tasks]
    else:
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=args.workers) as executor:
            records = list(executor.map(_execute_worker, tasks))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    capture_directory = args.out.parent / f"{args.out.stem}_captures"
    failures_saved = 0
    for index, (task, record) in enumerate(zip(tasks, records)):
        record["record_index"] = index
        record["replay_command"] = shlex.join([
            sys.executable, str(Path(__file__).resolve()), "replay",
            "--artifact", str(args.out.resolve()), "--record-index", str(index),
        ])
        record["capture"] = None
        if (record["outcome"] != TrialOutcome.DECODED.value
                and failures_saved < args.save_failures):
            replayed, capture = execute_trial(task, include_capture=True)
            if replayed["outcome"] != record["outcome"]:
                raise RuntimeError("failure changed while creating replay capture")
            capture_directory.mkdir(parents=True, exist_ok=True)
            capture_path = capture_directory / f"record_{index:06d}.npz"
            np.savez_compressed(capture_path, **capture)
            record["capture"] = str(capture_path.resolve())
            failures_saved += 1

    mode_rows = [mode_metadata(
        selector, args.seed, args.payload_bytes, args.useful_application_bytes)
        for selector in args.modes]
    artifact = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "claim_scope": "exploration_not_qualification",
        "command": shlex.join([sys.executable, str(Path(__file__).resolve()),
                               *sys.argv[1:]]),
        "started_utc": started,
        "completed_utc": utc_now(),
        "master_seed": args.seed,
        "seed_policy": {
            "algorithm": "sha256_first_63_bits",
            "inputs": ["master_seed", "namespace", "canonical_point_key",
                       "one_based_trial"],
            "namespaces": ["workload", "watterson", "awgn"],
            "matched_across_modes": True,
            "point_order_independent": True,
        },
        "worker_processes": args.workers,
        "trials_per_mode_point": args.trials,
        "workload": {
            "name": args.workload_name,
            "physical_payload_bytes": args.payload_bytes,
            "air_header_bytes": AIR_HEADER_BYTES,
            "data_body_bytes": args.payload_bytes - AIR_HEADER_BYTES,
            "useful_application_bytes": args.useful_application_bytes,
            "useful_application_bits": args.useful_application_bytes * 8,
        },
        "snr_convention": {
            "kind": "waveform",
            "signal": "mean_square_over_complete_half_open_keying_interval",
            "reference_samples": "[0, tx_samples)",
            "noise": "real_awgn_over_0_to_24000_hz_nyquist_band",
        },
        "model": args.model,
        "watterson_preset": (args.watterson_preset
                              if args.model != "awgn" else None),
        "points_db": [float(point) for point in args.points],
        "mode_metadata": mode_rows,
        "summaries": summarize(records),
        "trials": records,
        "git": _git_metadata(),
        "environment": _environment(),
        "limitations": [
            "No radio or VARA implementation was exercised.",
            "All Watterson trials reset fading independently per frame.",
            ("watterson_fixed_n0 fixes AWGN power from each mode's unfaded "
             "transmit reference; it is not a continuous-fade campaign."),
        ],
    }
    args.out.write_text(json.dumps(_jsonable(artifact), indent=2,
                                   allow_nan=False) + "\n")
    for row in artifact["summaries"]:
        delivery = row["verified_delivery_probability"]
        print(f"{row['mode_name']} {row['point_key']}: "
              f"{delivery['count']}/{delivery['total']} decoded "
              f"(95% Wilson {delivery['wilson_95'][0]:.3f}-"
              f"{delivery['wilson_95'][1]:.3f})")
    print(f"wrote {args.out}")
    return 0


def replay(args) -> int:
    artifact = json.loads(args.artifact.read_text())
    if artifact.get("schema_id") != SCHEMA_ID:
        raise ValueError("not an HR0 frame benchmark artifact")
    expected = artifact["trials"][args.record_index]
    task = {
        "mode_selector": expected["mode_selector"],
        "model": expected["model"],
        "preset": expected["preset"],
        "snr_db": expected["waveform_snr_db"],
        "point_key": expected["point_key"],
        "trial": expected["trial"],
        "derived_seeds": expected["derived_seeds"],
        "physical_payload_bytes": expected.get(
            "physical_payload_bytes", PHYSICAL_PAYLOAD_BYTES),
        "useful_application_bytes": expected.get(
            "useful_application_bytes",
            expected.get("data_body_bytes", DATA_BODY_BYTES)),
    }
    actual, capture = execute_trial(task, include_capture=args.capture_out is not None)
    keys = ("outcome", "payload_sha256", "derived_seeds",
            "acquired_above_threshold", "checked_wrong_payload")
    matched = all(actual[key] == expected[key] for key in keys)
    if args.capture_out is not None:
        args.capture_out.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(args.capture_out, **capture)
    print(json.dumps({"matched": matched, "record_index": args.record_index,
                      "expected_outcome": expected["outcome"],
                      "actual_outcome": actual["outcome"],
                      "derived_seeds": actual["derived_seeds"]}, indent=2))
    return 0 if matched else 1


def _jsonable(value):
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    run = subparsers.add_parser("sweep", help="run a matched frame sweep")
    run.add_argument("--modes", nargs="+", default=["hc0"],
                     help="hc0 or package.module:OBJECT selectors")
    run.add_argument("--model", choices=MODELS, required=True)
    run.add_argument("--watterson-preset", choices=sorted(WATTERSON_PRESETS),
                     default="mid_latitude_disturbed")
    run.add_argument("--points", type=float, nargs="+", required=True)
    run.add_argument("--trials", type=int, default=30)
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--seed", type=int, default=DEFAULT_MASTER_SEED)
    run.add_argument("--payload-bytes", type=int,
                     default=PHYSICAL_PAYLOAD_BYTES,
                     help="physical checked payload bytes (12 selects HR0-B tiny class)")
    run.add_argument("--useful-application-bytes", type=int,
                     help="bytes counted as useful; defaults to payload minus air header")
    run.add_argument("--workload-name", default="full_data")
    run.add_argument("--save-failures", type=int, default=3,
                     help="maximum failed captures to retain; every failure remains replayable")
    run.add_argument("--out", type=Path, required=True)
    run.set_defaults(function=sweep)
    rerun = subparsers.add_parser("replay", help="replay one artifact record")
    rerun.add_argument("--artifact", type=Path, required=True)
    rerun.add_argument("--record-index", type=int, required=True)
    rerun.add_argument("--capture-out", type=Path)
    rerun.set_defaults(function=replay)
    args = parser.parse_args(argv)
    if args.command_name == "sweep":
        if args.trials < 1:
            parser.error("--trials must be positive")
        if args.workers < 1:
            parser.error("--workers must be positive")
        if args.save_failures < 0:
            parser.error("--save-failures must be non-negative")
        if not AIR_HEADER_BYTES <= args.payload_bytes <= PHYSICAL_PAYLOAD_BYTES:
            parser.error(
                f"--payload-bytes must be in {AIR_HEADER_BYTES}..{PHYSICAL_PAYLOAD_BYTES}")
        if args.useful_application_bytes is None:
            args.useful_application_bytes = args.payload_bytes - AIR_HEADER_BYTES
        if not 0 < args.useful_application_bytes <= args.payload_bytes:
            parser.error("--useful-application-bytes must be in 1..payload-bytes")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    return int(args.function(args))


if __name__ == "__main__":
    arguments = parse_args()
    raise SystemExit(arguments.function(arguments))
