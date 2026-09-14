"""Small fixed-seed channel sweeps intended to run on every CI invocation.

These are deliberately not statistical claims. They pin a handful of known
operating points with at most two frames each; large confidence-building runs
belong to scripts/benchmark_simulated_channels.py.
"""

import numpy as np
import pytest

from whale import framing, modes
from whale.channel import (AwgnChannel, ChannelChain, SnrSpec,
                           WattersonChannel)
from whale.fm_channel import ComplexFmChannel
from whale.modes.hf2_mode import HF2
from whale.qualification import run_frame_trial, run_frame_trials


MASTER_SEED = 20260829


@pytest.mark.channel_regression
@pytest.mark.parametrize("mode_name,trials,minimum", [
    ("vf14-4", 1, 1),
    ("vf16", 1, 1),
])
def test_vhf_modes_at_measured_fm_bench_point(mode_name, trials, minimum):
    mode = next(mode for mode in modes.default_registry().modes
                if mode.name == mode_name)
    records = run_frame_trials(
        mode,
        lambda seed: ComplexFmChannel.from_preset(
            48_000, "vhf_bench_conservative", carrier_to_noise_db=30,
            seed=seed),
        trials, MASTER_SEED, point_index=0, direction="FM C/N 30 dB")
    assert sum(record.decoded for record in records) >= minimum


@pytest.mark.channel_regression
def test_hc0_on_moderate_watterson_with_awgn():
    mode = next(mode for mode in modes.hf_registry().modes
                if mode.name == "hc0")

    def channel(seed):
        return ChannelChain((
            WattersonChannel.from_preset(
                48_000, "mid_latitude_moderate", seed),
            AwgnChannel(48_000, SnrSpec(14.0308998699), seed ^ 0x5A5A),
        ))

    records = run_frame_trials(
        mode, channel, 2, MASTER_SEED, point_index=1,
        direction="mid-latitude moderate, SNR/3 kHz 14.03 dB")
    assert all(record.decoded for record in records)


@pytest.mark.channel_regression
def test_hf2_on_quiet_watterson_at_14db_snr_3khz():
    """HF2's Level 2 quiet-Watterson boundary point (SPEED_LADDERS.md), the
    required +5 dB waveform-SNR envelope edge confirmed at 300 trials in
    experiments/hf2/RESULTS.md -- equivalently 5.0 + 10*log10(24000/3000) =
    14.0308998699 dB once SNR/3 kHz is the reference (see the SnrSpec(5.0)
    -> SnrSpec(14.0308998699) conversion 4c08978 already applied to
    test_hf_modes_on_moderate_watterson_with_awgn above; HF2's tests were
    added on a branch that forked before that commit and never got it)."""

    def channel(seed):
        return ChannelChain((
            WattersonChannel.from_preset(48_000, "mid_latitude_quiet", seed),
            AwgnChannel(48_000, SnrSpec(14.0308998699), seed ^ 0x5A5A),
        ))

    records = run_frame_trials(
        HF2, channel, 2, MASTER_SEED, point_index=4,
        direction="mid-latitude quiet, SNR/3 kHz 14.03 dB",
        payload_bytes=HF2.chunk_size + framing.AIR_HEADER_BYTES)
    assert sum(record.decoded for record in records) >= 1


@pytest.mark.channel_regression
def test_hf2_on_moderate_watterson_at_19db_snr_3khz():
    """HF2's Level 2 moderate-Watterson boundary point (SPEED_LADDERS.md),
    the required +10 dB waveform-SNR envelope edge confirmed at 300 trials
    in experiments/hf2/RESULTS.md -- equivalently 10.0 + 10*log10(24000/3000)
    = 19.0308998699 dB once SNR/3 kHz is the reference; see the sibling
    quiet-boundary test above for why this needed converting."""

    def channel(seed):
        return ChannelChain((
            WattersonChannel.from_preset(48_000, "mid_latitude_moderate", seed),
            AwgnChannel(48_000, SnrSpec(19.0308998699), seed ^ 0x5A5A),
        ))

    records = run_frame_trials(
        HF2, channel, 2, MASTER_SEED, point_index=5,
        direction="mid-latitude moderate, SNR/3 kHz 19.03 dB",
        payload_bytes=HF2.chunk_size + framing.AIR_HEADER_BYTES)
    assert sum(record.decoded for record in records) >= 1
