"""Focused checks for the default HC1W waveform."""

import numpy as np
import pytest

from whale import framing, mode_qualification, rx_audio
from whale.modes import hc1w
from whale.modes.hc1w_mode import HC1W


def test_hc1w_geometry_and_capacity():
    assert hc1w.N_CARRIERS == 23
    assert hc1w.CARRIER_HZ[[0, -1]].tolist() == [468.75, 2531.25]
    assert hc1w.CODEC.code is hc1w.dsp.K9
    assert HC1W.chunk_size == 995
    assert HC1W.airtime(HC1W.chunk_size) == pytest.approx(
        hc1w.frame_seconds())
    assert HC1W.chunk_size * 8 / HC1W.airtime(HC1W.chunk_size) == pytest.approx(
        HC1W.chunk_size * 8 / hc1w.frame_seconds()
    )


def test_hc1w_round_trips_a_full_data_frame():
    rng = np.random.default_rng(20260908)
    payload = rng.integers(
        0, 256, framing.AIR_HEADER_BYTES + HC1W.chunk_size, dtype=np.uint8
    ).tobytes()
    received = rx_audio.downsample(HC1W.encode(payload))
    result = HC1W.decode(received)
    assert result["payload"] == payload
    assert result["present_carriers"] == hc1w.N_CARRIERS


def test_hc1w_is_available_in_the_default_hf_ladder():
    default = mode_qualification.registry("hf-ssb", "default")
    experimental = mode_qualification.registry("hf-ssb", "experimental")
    assert HC1W.mode_id in default.supported_ids
    assert HC1W.mode_id in experimental.supported_ids
    assert 4 not in experimental.supported_ids
    assert default.resolve(HC1W.mode_id) is HC1W
    assert experimental.resolve(HC1W.mode_id) is HC1W
