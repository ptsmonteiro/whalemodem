"""Focused production contracts for the HF most-robust HR0 mode."""

import numpy as np
import pytest

from whale import framing, modes, rx_audio
from whale.modes import hf_lead, hr0
from whale.modes.hc0_mode import HC0
from whale.modes.hr0_mode import HR0


def _capture(audio):
    return rx_audio.downsample(np.asarray(audio, np.float32))


def test_hr0_geometry_meets_hf_level_zero_speed_contract():
    assert HR0.mode_id == 10
    assert hr0.TONE_COUNT == 128
    assert hr0.BANK.bandwidth_hz <= 2_300
    assert hr0.SYMBOL_SAMPLES == 2_688
    assert hr0.CODEC.code is hr0.dsp.K9
    assert HR0.chunk_size == hr0.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES == 32
    assert HR0.airtime(HR0.chunk_size) == pytest.approx(7.316)
    assert HR0.chunk_size * 8 / HR0.airtime(HR0.chunk_size) >= 20


def test_hr0_clean_round_trip_and_common_lead():
    payload = bytes(range(hr0.MAX_PAYLOAD_BYTES))
    capture = _capture(HR0.encode(payload))
    label, score = hf_lead.detect_label(capture)
    result = HR0.decode(capture)
    assert label == hf_lead.HR0_LABEL
    assert score >= hf_lead.MATCH_THRESHOLD
    assert result["payload"] == payload
    assert result["head_blocks_observed"] >= hf_lead.MIN_BLOCKS


def test_hr0_decodes_deterministic_full_band_awgn_at_minus_15_db():
    """Pin the requested software boundary without claiming qualification."""
    payload = bytes(range(hr0.MAX_PAYLOAD_BYTES))
    audio = np.asarray(HR0.encode(payload), np.float64)
    signal_rms = np.sqrt(np.mean(audio ** 2))
    noise_rms = signal_rms / 10.0 ** (-15.0 / 20.0)
    noisy = audio + np.random.default_rng(20260901).normal(
        0.0, noise_rms, len(audio))
    assert HR0.decode(_capture(noisy))["payload"] == payload


def test_hr0_is_new_hf_control_and_bottom_rung():
    registry = modes.hf_registry()
    assert registry.control is HR0
    assert registry.supported_ids[:3] == (HR0.mode_id, HC0.mode_id, 4)
    assert registry.step(HR0, -1) is None
    assert registry.step(HR0, +1) is HC0


@pytest.mark.parametrize("bad", [np.zeros(0), np.zeros(20_000),
                                  np.full(20_000, np.nan),
                                  np.zeros((100, 2))])
def test_hr0_rejects_invalid_or_signal_free_input(bad):
    assert HR0.decode(bad)["payload"] is None


def test_hr0_payload_limit_is_enforced():
    with pytest.raises(ValueError, match="carries at most"):
        HR0.encode(bytes(hr0.MAX_PAYLOAD_BYTES + 1))
