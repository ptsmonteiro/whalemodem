"""Shared direct-frame runner for bounded regressions and Monte Carlo sweeps."""

from __future__ import annotations

from typing import Callable

import numpy as np

from . import framing, rx_audio
from .channel import (AudioChannel, AwgnChannel, ChannelChain, SnrSpec,
                      WattersonChannel)
from .fm_channel import ComplexFmChannel
from .trials import (TrialOutcome, TrialResult, classify_decode,
                     common_decoder_metrics)


ChannelFactory = Callable[[int], AudioChannel]


def channel_factory(model: str, point_db: float, *,
                    watterson_preset: str = "mid_latitude_moderate",
                    fm_preset: str = "vhf_bench_conservative") -> ChannelFactory:
    """Build the canonical seeded channel factory used by qualification tools."""
    if model == "awgn":
        return lambda seed: AwgnChannel(48_000, SnrSpec(point_db), seed)
    if model == "fm":
        return lambda seed: ComplexFmChannel.from_preset(
            48_000, fm_preset, point_db, seed)
    if model == "watterson":
        return lambda seed: ChannelChain((
            WattersonChannel.from_preset(48_000, watterson_preset, seed),
            AwgnChannel(48_000, SnrSpec(point_db), seed ^ 0x5A5A),
        ))
    raise ValueError(f"unknown channel model {model!r}")


def channel_point_label(model: str, point_db: float, *,
                        watterson_preset: str = "mid_latitude_moderate",
                        fm_preset: str = "vhf_bench_conservative") -> str:
    if model == "fm":
        return f"{fm_preset}, RF C/N {point_db:g} dB"
    if model == "watterson":
        return f"{watterson_preset}, waveform SNR {point_db:g} dB"
    if model == "awgn":
        return f"AWGN waveform SNR {point_db:g} dB"
    raise ValueError(f"unknown channel model {model!r}")


def full_packet_bytes(mode) -> int:
    return framing.AIR_HEADER_BYTES + mode.chunk_size


def trial_seed(master_seed: int, mode_id: int, point_index: int, trial: int) -> int:
    """Stable independent seed, unaffected by which other points are selected."""

    sequence = np.random.SeedSequence([master_seed, mode_id, point_index, trial])
    return int(sequence.generate_state(1, dtype=np.uint64)[0])


def run_frame_trial(mode, channel: AudioChannel, seed: int, trial: int,
                    direction: str) -> TrialResult:
    """Encode, impair, downsample and decode one full-capacity mode packet."""

    rng = np.random.default_rng(seed)
    payload_bytes = full_packet_bytes(mode)
    payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
    transmitted = np.zeros(0, np.float32)
    captured = np.zeros(0, np.float32)
    channel_measurements = {}
    result = {}
    error = None
    try:
        transmitted = np.asarray(mode.encode(payload), dtype=np.float32)
        impaired = channel.process(transmitted)
        channel_measurements = impaired.measurements
        capture_audio = np.asarray(impaired.audio, dtype=np.float32)
        captured = rx_audio.downsample(np.concatenate((
            capture_audio,
            np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32),
        )))
        result = mode.decode(captured)
        outcome = classify_decode(result, payload, mode.confidence_threshold)
    except Exception as exc:
        outcome = TrialOutcome.ERROR
        error = f"{type(exc).__name__}: {exc}"
    return TrialResult(
        trial=trial, direction=direction, mode_id=mode.mode_id,
        mode_name=mode.name, payload_bytes=payload_bytes, outcome=outcome,
        tx_samples=len(transmitted), tx_sample_rate=mode.tx_sample_rate,
        rx_samples=len(captured), rx_sample_rate=mode.rx_sample_rate,
        keyed_seconds=len(transmitted) / mode.tx_sample_rate,
        channel_measurements=channel_measurements,
        decoder_metrics=common_decoder_metrics(result, captured), error=error)


def run_frame_trials(mode, channel_factory: ChannelFactory, trials: int,
                     master_seed: int, point_index: int, direction: str):
    if trials < 1:
        raise ValueError("trials must be positive")
    records = []
    for trial in range(1, trials + 1):
        seed = trial_seed(master_seed, mode.mode_id, point_index, trial)
        records.append(run_frame_trial(
            mode, channel_factory(seed), seed, trial, direction))
    return records
