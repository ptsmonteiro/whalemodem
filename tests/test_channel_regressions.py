"""Small fixed-seed channel sweeps intended to run on every CI invocation.

These are deliberately not statistical claims. They pin a handful of known
operating points with at most two frames each; large confidence-building runs
belong to scripts/benchmark_simulated_channels.py.
"""

import pytest

from whale import modes
from whale.channel import (AwgnChannel, ChannelChain, SnrSpec,
                           WattersonChannel)
from whale.fm_channel import ComplexFmChannel
from whale.qualification import run_frame_trials


MASTER_SEED = 20260829


@pytest.mark.channel_regression
@pytest.mark.parametrize("mode_name,trials,minimum", [
    ("300baud", 1, 1),
    ("600baud", 1, 1),
    # The conservative measured response is marginal at 2200 Hz. Keep two
    # fixed realizations and require one, so a genuine collapse is caught
    # while an eventual decoder improvement is allowed.
    ("1200baud", 2, 1),
    ("vf3", 1, 1),
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
@pytest.mark.parametrize("mode_name", ["hc0", "hc1"])
def test_hf_modes_on_moderate_watterson_with_awgn(mode_name):
    mode = next(mode for mode in modes.hf_registry().modes
                if mode.name == mode_name)

    def channel(seed):
        return ChannelChain((
            WattersonChannel.from_preset(
                48_000, "mid_latitude_moderate", seed),
            AwgnChannel(48_000, SnrSpec(5.0), seed ^ 0x5A5A),
        ))

    records = run_frame_trials(
        mode, channel, 2, MASTER_SEED, point_index=1,
        direction="mid-latitude moderate, waveform SNR 5 dB")
    assert all(record.decoded for record in records)
