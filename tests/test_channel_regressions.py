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
from whale.modes import vf6
from whale.modes.vf6_mode import VF6
from whale.qualification import run_frame_trial, run_frame_trials, trial_seed


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
def test_vf6_full_capacity_on_very_good_flat_nbfm_channel():
    """Pin VF6 through the complete complex-IQ FM path, not audio AWGN."""
    records = run_frame_trials(
        VF6,
        lambda seed: ComplexFmChannel.from_profile(
            48_000, "flat_nbfm", carrier_to_noise_db=40, seed=seed),
        2, 20260831, point_index=0,
        direction="flat_nbfm, RF C/N 40 dB")
    assert all(record.decoded for record in records)
    assert all(record.payload_bytes == vf6.MAX_PAYLOAD_BYTES
               for record in records)


@pytest.mark.channel_regression
@pytest.mark.parametrize("mode_name", ["hc0", "hc1"])
def test_hf_modes_on_moderate_watterson_with_awgn(mode_name):
    mode = next(mode for mode in modes.hf_registry().modes
                if mode.name == mode_name)

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
@pytest.mark.parametrize("preset", [
    "ic705_to_kg_uv9d", "kg_uv9d_to_ic705", "vhf_bench_conservative"])
@pytest.mark.parametrize("trial,terminal_bit", [(1, 0), (2, 1)])
def test_mode2_recorded_frame_boundary_replays_exactly(preset, trial,
                                                        terminal_bit):
    """Pin both terminal-bit values, including the recorded trial-1 failure."""
    mode = next(mode for mode in modes.default_registry().modes
                if mode.mode_id == 2)
    seed = trial_seed(MASTER_SEED, mode.mode_id, 0, trial)
    payload = np.random.default_rng(seed).integers(
        0, 256, 412, dtype=np.uint8).tobytes()
    assert framing.build_frame_bits(
        payload, baud=mode.baud, include_head=False)[-1] == terminal_bit
    record = run_frame_trial(
        mode, ComplexFmChannel.from_preset(48_000, preset, 10, seed),
        seed, trial, preset, payload_bytes=412)
    assert record.decoded
    assert record.decoder_metrics["total_bit_errors"] == 0
