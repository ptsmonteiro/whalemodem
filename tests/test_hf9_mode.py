"""HF9 mode geometry, payload, and registry contracts."""

import numpy as np
import pytest

from whale import framing, rx_audio
from whale.mode_qualification import (QualificationLevel, qualification_level,
                                      registry)
from whale.modes.hf8_mode import HF8_PHY
from whale.modes.hf9_mode import HF9, HF9_PHY, BAND_HI_HZ, BAND_LO_HZ
from whale.phy import ofdm49


def _full_payload(salt=19):
    return bytes((i * 43 + salt) & 0xFF
                 for i in range(HF9.chunk_size + framing.AIR_HEADER_BYTES))


def test_hf9_geometry_and_capacity_are_fixed():
    assert HF9.mode_id == 17
    spacing = ofdm49.DESIGN_RATE / HF9_PHY.fft_size
    assert spacing == 50.0
    assert HF9_PHY.active_bins == tuple(
        ofdm49.bins_in_band(HF9_PHY.fft_size, BAND_LO_HZ, BAND_HI_HZ))
    assert HF9_PHY.n_active == 49
    assert [b * spacing for b in (min(HF9_PHY.active_bins),
                                  max(HF9_PHY.active_bins))] == [300.0, 2700.0]
    assert HF9_PHY.cp_len / ofdm49.DESIGN_RATE == 0.002
    assert HF9_PHY.bits_per_symbol == 2
    assert HF9_PHY.fec_rate == "1/2"
    assert HF9_PHY.pilot_interval == 5
    assert HF9_PHY.interleave
    assert HF9_PHY.equalizer == "gain"
    assert HF9_PHY.n_comb() == 0
    assert HF9_PHY.drive_scale == 0.008
    assert HF9_PHY.max_payload_bytes == 115
    assert HF9.chunk_size == 105
    assert HF9_PHY.n_codewords == 3
    assert HF9.airtime(HF9.chunk_size) == pytest.approx(0.836)
    assert 8 * HF9.chunk_size / HF9.airtime(HF9.chunk_size) == pytest.approx(
            1004.7846889952153)
    assert HF9_PHY.active_bins == HF8_PHY.active_bins
    assert HF9_PHY.cp_len == HF8_PHY.cp_len


def test_hf9_clean_full_frame_round_trip():
    payload = _full_payload()
    tx = HF9.encode(payload)
    captured = rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))
    result = HF9.decode(captured)
    assert result["payload"] == payload
    assert result["crc_ok"]
    assert result["ldpc_ok"]
    assert result["noise_estimator"] == "repeat"


def test_hf9_frequency_hint_uses_the_local_search_window(monkeypatch):
    payload = _full_payload(23)
    tx = HF9.encode(payload)
    captured = rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))
    calls = 0
    original = ofdm49.fftconvolve

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ofdm49, "fftconvolve", counted)
    result = HF9.decode(captured, freq_hint_hz=0.0)

    assert result["payload"] == payload
    assert calls == 5


def test_hf9_is_experimental_and_rate_ordered_below_hc1w():
    assert (qualification_level("hf", HF9.mode_id)
            is QualificationLevel.EXPERIMENTAL)
    assert HF9.mode_id not in registry("hf", "default").supported_ids
    experimental = registry("hf", "experimental")
    names = [mode.name for mode in experimental.modes]
    assert names.index("hc0") < names.index("hf9") < names.index("hc1w")


def test_hf9_rejects_oversize_payload():
    with pytest.raises(ValueError, match="carries at most"):
        HF9.encode(bytes(HF9_PHY.max_payload_bytes + 1))
