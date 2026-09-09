"""Correctness gates for the HC1P arms, run before any sweep is believed.

The experiment's whole claim rests on the arms differing *only* in the payload
path, so these check exactly that: the baseline arm is HC1W bit for bit, every
arm round-trips on a clean channel, and the pilot layout is what it says it is.

    python -m pytest experiments/hc1p_pilots/test_waveform.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

EXPERIMENT_DIR = Path(__file__).resolve().parent
if str(EXPERIMENT_DIR) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_DIR))

from whale import rx_audio
from whale.modes import hc1w
from whale.modes.hc1w_mode import HC1W

from waveform import (ARMS, N_CARRIERS, PAYLOAD_SYMBOLS, TOTAL_SYMBOLS,
                      Variant, VariantMode, arm_by_name)


def clean_capture(mode: VariantMode, payload: bytes) -> np.ndarray:
    transmitted = np.asarray(mode.encode(payload), dtype=np.float32)
    return rx_audio.downsample(np.concatenate((
        transmitted,
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))


def test_baseline_arm_transmits_exactly_what_hc1w_transmits():
    """The control is the shipped mode, not a reimplementation of it.

    Without this the sweep would be comparing new pilot code against new
    no-pilot code, and a difference could live in either.
    """
    payload = np.random.default_rng(11).integers(
        0, 256, hc1w.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    baseline = VariantMode(arm_by_name("baseline"))
    assert baseline.variant.max_payload_bytes == hc1w.MAX_PAYLOAD_BYTES
    assert np.array_equal(np.asarray(baseline.encode(payload)),
                          np.asarray(HC1W.encode(payload)))


@pytest.mark.parametrize("variant", ARMS, ids=lambda v: v.name)
def test_every_arm_round_trips_on_a_clean_channel(variant: Variant):
    mode = VariantMode(variant)
    payload = np.random.default_rng(7).integers(
        0, 256, variant.max_payload_bytes, dtype=np.uint8).tobytes()
    result = mode.decode(clean_capture(mode, payload))
    assert result["payload"] == payload, result.get("failure")


@pytest.mark.parametrize("variant", ARMS, ids=lambda v: v.name)
def test_arms_share_hc1w_airtime_exactly(variant: Variant):
    """Frame length is held fixed so it cannot confound the comparison."""
    payload = bytes(variant.max_payload_bytes)
    assert len(VariantMode(variant).encode(payload)) == len(
        HC1W.encode(bytes(hc1w.MAX_PAYLOAD_BYTES)))


@pytest.mark.parametrize("variant", ARMS, ids=lambda v: v.name)
def test_pilot_layout_partitions_the_payload(variant: Variant):
    pilots, data = variant.pilot_positions, variant.data_positions
    assert len(pilots) + len(data) == PAYLOAD_SYMBOLS
    assert not set(pilots.tolist()) & set(data.tolist())
    assert variant.payload_bits == len(data) * N_CARRIERS * 2
    assert variant.codec.interleaver.is_valid()
    if variant.stride:
        gaps = np.diff(np.concatenate(([-1], pilots)))
        assert np.all(gaps <= variant.stride)


@pytest.mark.parametrize("variant", [v for v in ARMS if v.stride],
                         ids=lambda v: v.name)
def test_pilots_reach_the_receiver_where_they_were_placed(variant: Variant):
    """A pilot must survive as the known symbol the tracker assumes it is."""
    mode = VariantMode(variant)
    payload = np.random.default_rng(3).integers(
        0, 256, variant.max_payload_bytes, dtype=np.uint8).tobytes()
    grid = variant.payload_grid(payload)
    assert np.allclose(grid[variant.pilot_positions], variant.pilot_values)
    assert grid.shape == (PAYLOAD_SYMBOLS, N_CARRIERS)


def test_stride_choices_stay_inside_the_moderate_coherence_time():
    """0.5 Hz spread is a ~2 s coherence time; every stride must beat it."""
    symbol_seconds = hc1w.SYMBOL_SAMPLES / hc1w.SAMPLE_RATE
    for variant in ARMS:
        if not variant.stride:
            continue
        assert variant.stride * symbol_seconds < 1.0


def test_coherent_detection_requires_pilots():
    with pytest.raises(ValueError):
        Variant(stride=0, detection="coherent")


def test_total_symbols_is_hc1w_s():
    assert TOTAL_SYMBOLS == 365


@pytest.mark.parametrize("name", [
    "baseline", "diff-s24", "diff-s24-flatweight", "diff-s48-blind",
    "baseline-i4375", "cohe-s16",
])
def test_an_arm_survives_the_trip_into_a_worker_process(name: str):
    """Every field must reach the pool, or an ablation compares an arm to itself.

    The sweep sends variants to worker processes as a field tuple.  An early
    version sent only (stride, detection), so `-flatweight` and `-blind` arms
    were silently rebuilt as their plain counterparts and both ablations
    reported "no difference" -- for the two arms that were, in fact, the same
    arm.  Reconstruction is checked here rather than trusted.
    """
    from dataclasses import astuple
    import waveform

    variant = waveform.arm_by_name(name)
    rebuilt = waveform.Variant(*astuple(variant))
    assert rebuilt == variant
    assert rebuilt.name == name
    assert rebuilt.max_payload_bytes == variant.max_payload_bytes
    assert np.array_equal(rebuilt.codec.interleaver.permutation,
                          variant.codec.interleaver.permutation)


def test_ablation_arms_actually_differ_from_their_plain_counterparts():
    """A no-op ablation flag would make every ablation read as 'no effect'."""
    import waveform

    plain = waveform.arm_by_name("diff-s24")
    payload = np.random.default_rng(5).integers(
        0, 256, plain.max_payload_bytes, dtype=np.uint8).tobytes()

    blind = waveform.arm_by_name("diff-s24-blind")
    assert not np.allclose(blind.payload_grid(payload)[blind.pilot_positions],
                           plain.payload_grid(payload)[plain.pilot_positions])

    restrided = waveform.arm_by_name("baseline-i4375")
    baseline = waveform.arm_by_name("baseline")
    assert restrided.max_payload_bytes == baseline.max_payload_bytes
    assert not np.array_equal(
        restrided.codec.interleaver.permutation,
        baseline.codec.interleaver.permutation)
