"""Shared direct-frame runner for bounded regressions and Monte Carlo sweeps."""

from __future__ import annotations

from typing import Callable

import numpy as np

from . import afsk, framing, rx_audio
from .channel import (AudioChannel, AwgnChannel, ChannelChain, SnrSpec,
                      WattersonChannel)
from .fm_channel import ComplexFmChannel
from .trials import (TrialOutcome, TrialResult, classify_decode,
                     common_decoder_metrics)


ChannelFactory = Callable[[int], AudioChannel]


def channel_factory(model: str, point_db: float, *,
                    watterson_preset: str = "mid_latitude_moderate",
                    fm_preset: str = "vhf_bench_conservative",
                    fm_profile: str | None = None) -> ChannelFactory:
    """Build the canonical seeded channel factory used by qualification tools."""
    if model == "awgn":
        return lambda seed: AwgnChannel(48_000, SnrSpec(point_db), seed)
    if model == "fm":
        if fm_profile is not None:
            return lambda seed: ComplexFmChannel.from_profile(
                48_000, fm_profile, point_db, seed)
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
                        fm_preset: str = "vhf_bench_conservative",
                        fm_profile: str | None = None) -> str:
    if model == "fm":
        recipe = fm_profile if fm_profile is not None else fm_preset
        return f"{recipe}, RF C/N {point_db:g} dB"
    if model == "watterson":
        return f"{watterson_preset}, SNR/3 kHz {point_db:g} dB"
    if model == "awgn":
        return f"AWGN SNR/3 kHz {point_db:g} dB"
    raise ValueError(f"unknown channel model {model!r}")


def full_packet_bytes(mode) -> int:
    return framing.AIR_HEADER_BYTES + mode.chunk_size


def trial_seed(master_seed: int, mode_id: int, point_index: int, trial: int) -> int:
    """Stable independent seed, unaffected by which other points are selected."""

    sequence = np.random.SeedSequence([master_seed, mode_id, point_index, trial])
    return int(sequence.generate_state(1, dtype=np.uint64)[0])


def _add_bit_error_evidence(result, payload: bytes, mode) -> None:
    """Add bounded CPFSK hard-decision evidence when the decoder provides it."""

    hard_bits = result.pop("_hard_bits", None)
    if hard_bits is None or not hasattr(mode, "baud"):
        return
    expected = framing.build_frame_bits(payload, baud=mode.baud,
                                        include_head=False)
    compared = min(len(hard_bits), len(expected))
    positions = [index for index in range(compared)
                 if hard_bits[index] != expected[index]]
    missing = max(0, len(expected) - len(hard_bits))
    total_errors = len(positions) + missing
    result["ber"] = total_errors / len(expected)
    result["total_bit_errors"] = total_errors
    result["compared_bits"] = compared
    result["missing_bits"] = missing
    result["bit_error_positions"] = positions[:128]
    result["bit_error_positions_truncated"] = len(positions) > 128


def run_frame_trial(mode, channel: AudioChannel, seed: int, trial: int,
                    direction: str, payload_bytes: int | None = None) -> TrialResult:
    """Encode, impair, downsample and decode one mode packet.

    ``payload_bytes`` is the complete physical-layer payload, including any
    link header. Omitting it preserves the historical full-capacity run.
    """

    rng = np.random.default_rng(seed)
    payload_bytes = (full_packet_bytes(mode) if payload_bytes is None
                     else payload_bytes)
    payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
    transmitted = np.zeros(0, np.float32)
    captured = np.zeros(0, np.float32)
    channel_measurements = {}
    result = {}
    error = None
    try:
        transmitted = np.asarray(mode.encode(payload), dtype=np.float32)
        impaired = channel.process(transmitted)
        drained = channel.drain()
        channel_measurements = dict(impaired.measurements)
        channel_measurements["drain"] = dict(drained.measurements)
        capture_audio = np.concatenate((
            np.asarray(impaired.audio, dtype=np.float32),
            np.asarray(drained.audio, dtype=np.float32)))
        captured = rx_audio.downsample(np.concatenate((
            capture_audio,
            np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32),
        )))
        if isinstance(getattr(mode, "codec", None), afsk.CpfskCodec):
            result = mode.decode(captured, diagnostics=True)
        else:
            result = mode.decode(captured)
        _add_bit_error_evidence(result, payload, mode)
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
                     master_seed: int, point_index: int, direction: str,
                     payload_bytes: int | None = None):
    if trials < 1:
        raise ValueError("trials must be positive")
    records = []
    for trial in range(1, trials + 1):
        seed = trial_seed(master_seed, mode.mode_id, point_index, trial)
        records.append(run_frame_trial(
            mode, channel_factory(seed), seed, trial, direction,
            payload_bytes=payload_bytes))
    return records
