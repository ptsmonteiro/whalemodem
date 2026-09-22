"""Software conformance and clean loopback tests for VF16."""

import numpy as np
import pytest

from whale import framing, rx_audio, waveform
from whale.mode_qualification import QualificationLevel, qualification_level, registry
from whale.modes.vf12 import VF12
from whale.modes.vf16 import VF16


def test_mode_contract_and_requested_capacity():
    assert isinstance(VF16, waveform.WaveformMode)
    assert VF16.name == "vf16"
    assert VF16.mode_id == 22
    assert VF16.chunk_size == 1928
    assert VF16.max_payload_bytes == VF16.chunk_size + framing.AIR_HEADER_BYTES
    assert VF16.fec_rate == "2/3"
    assert VF16.bits_per_carrier == 3  # 8-PSK
    assert VF16.n_codewords == 36
    assert VF16.payload_symbols == 181
    assert VF12.payload_symbols == 178


def test_vf12_ofdm_geometry_and_airtime_are_preserved():
    for attribute in ("carrier_spacing_hz", "n_carriers", "fft_size", "cp_len",
                      "band_lo_hz", "band_hi_hz", "lead_in_seconds",
                      "pilot_comb_stride", "pilot_time_span"):
        assert getattr(VF16, attribute) == getattr(VF12, attribute)
    assert VF16.airtime(VF16.max_payload_bytes) == pytest.approx(5.047, abs=0.002)
    assert VF12.airtime(VF12.max_payload_bytes) == pytest.approx(4.978, abs=0.002)


def test_vf16_is_the_default_rung_immediately_below_vf12():
    names = [mode.name for mode in registry("fm", "default").modes]
    assert names[-2:] == ["vf16", "vf12"]
    assert qualification_level("fm", VF16.mode_id) == QualificationLevel.DEFAULT


def test_full_capacity_clean_loopback():
    payload = np.arange(VF16.max_payload_bytes, dtype=np.uint8).tobytes()
    audio = VF16.encode(payload)
    result = VF16.decode(rx_audio.downsample(audio))

    assert result["payload"] == payload
    assert result["crc_ok"] is True
    assert result["codewords_ok"] == VF16.n_codewords


def test_frame_geometry_accounts_for_header_and_coded_symbols():
    assert VF16.chunk_size == VF16.max_payload_bytes - framing.AIR_HEADER_BYTES
    assert VF16.packet_bytes == VF16.max_payload_bytes + 6
    assert VF16.coded_bits == VF16.n_codewords * 648
    assert VF16.total_symbols == 8 + VF16.payload_symbols
    assert VF16.bits_per_symbol == len(VF16.data_positions) * 3
    assert VF16.pilot_positions[0] == 0
    assert VF16.pilot_positions[-1] == VF16.n_carriers - 1


def test_nonfinite_or_wrong_shaped_audio_is_rejected():
    for audio in (np.array([np.nan]), np.array([np.inf]), np.zeros((4, 2))):
        result = VF16.decode(audio)
        assert result["payload"] is None
        assert result["crc_ok"] is False
