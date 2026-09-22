"""Software conformance, PAPR and clean-loopback tests for FMHT4."""

import numpy as np
import pytest

from whale import framing, rx_audio, waveform
from whale.mode_qualification import QualificationLevel, qualification_level, registry
from whale.modes.fmht4 import FMHT4, Fmht4Mode, papr_db


def _payload():
    return np.arange(FMHT4.max_payload_bytes, dtype=np.uint8).tobytes()


def test_mode_contract():
    assert isinstance(FMHT4, waveform.WaveformMode)
    assert FMHT4.name == "fmht4"
    assert FMHT4.mode_id == 27
    assert FMHT4.fec_rate == "3/4"
    assert FMHT4.bits_per_carrier == 3  # 8-PSK, never QAM on this path
    assert FMHT4.chunk_size == FMHT4.max_payload_bytes - framing.AIR_HEADER_BYTES


def test_family_frame_geometry():
    """The geometry every FM-handheld rung shares, at this rung's rates."""
    assert FMHT4.carrier_spacing_hz == pytest.approx(31.25)
    assert FMHT4.fft_size / FMHT4.rx_sample_rate == pytest.approx(0.032)
    assert FMHT4.cp_len / FMHT4.rx_sample_rate == pytest.approx(0.002)
    assert FMHT4.symbol_samples / FMHT4.rx_sample_rate == pytest.approx(0.034)
    assert FMHT4.n_carriers == 64
    assert FMHT4.band_hz == (468.75, 2437.5)
    # 8 comb pilots of 64 bins is the family's ~12% overhead.
    assert len(FMHT4.pilot_positions) == 8
    assert len(FMHT4.data_positions) == 56
    assert FMHT4.pilot_positions[0] == 0
    assert FMHT4.pilot_positions[-1] == FMHT4.n_carriers - 1
    # 64 bins x 3 bits / 34 ms = 5,647 gross bit/s.
    assert FMHT4.n_carriers * 3 / 0.034 == pytest.approx(5647, abs=1)
    assert FMHT4.total_symbols == 8 + FMHT4.payload_symbols


def test_full_capacity_clean_loopback():
    payload = _payload()
    result = FMHT4.decode(rx_audio.downsample(FMHT4.encode(payload)))
    assert result["payload"] == payload
    assert result["crc_ok"] is True
    assert result["codewords_ok"] == FMHT4.n_codewords


def test_clip_and_filter_buys_peak_and_stays_in_band():
    """PAPR control is this rung's point, so both halves of it are asserted."""
    payload = _payload()
    unlimited = Fmht4Mode(papr_iterations=0)
    assert unlimited.modulated_papr_db(payload) > 12
    assert FMHT4.modulated_papr_db(payload) < 10

    # Filtering, not bare clipping: nothing outside the occupied bins.
    audio = np.asarray(FMHT4.encode(payload), dtype=float)
    head = round(FMHT4.lead_in_seconds * FMHT4.tx_sample_rate)
    body = audio[head:head + FMHT4.fft_size * 4]
    spectrum = np.abs(np.fft.rfft(body))
    occupied = spectrum[FMHT4.lo_bin:FMHT4.hi_bin + 1]
    outside = np.delete(spectrum, np.arange(FMHT4.lo_bin, FMHT4.hi_bin + 1))
    assert np.max(outside) < 0.05 * np.mean(occupied)


def test_preamble_length_is_a_parameter_and_both_shapes_round_trip():
    """The preamble is a robustness knob, so both shapes must actually work."""
    long_preamble = Fmht4Mode(sync_symbols=9, lead_in_seconds=0.5)
    assert long_preamble.preamble_symbols == 13
    assert long_preamble.total_symbols == 13 + long_preamble.payload_symbols
    # Longer preamble buys robustness out of payload, not out of airtime.
    assert long_preamble.bits_per_second < FMHT4.bits_per_second

    payload = np.arange(long_preamble.max_payload_bytes, dtype=np.uint8).tobytes()
    result = long_preamble.decode(rx_audio.downsample(long_preamble.encode(payload)))
    assert result["payload"] == payload
    assert result["codewords_ok"] == long_preamble.n_codewords


def test_papr_db_is_zero_for_silence():
    assert papr_db(np.zeros(100)) == 0.0


def test_registered_as_the_top_experimental_fm_rung():
    assert qualification_level("fm", FMHT4.mode_id) == QualificationLevel.EXPERIMENTAL
    names = [mode.name for mode in registry("fm", "experimental").modes]
    assert "fmht4" in names
    assert "fmht4" not in [mode.name for mode in registry("fm", "default").modes]


def test_rejects_higher_order_qam():
    with pytest.raises(ValueError):
        Fmht4Mode(bits_per_carrier=4)


def test_nonfinite_or_wrong_shaped_audio_is_rejected():
    for audio in (np.array([np.nan]), np.array([np.inf]), np.zeros((4, 2))):
        result = FMHT4.decode(audio)
        assert result["payload"] is None
        assert result["crc_ok"] is False


def test_silence_and_noise_do_not_decode():
    rng = np.random.default_rng(20260921)
    samples = 6 * rx_audio.CAPTURE_SAMPLE_RATE
    for audio in (np.zeros(samples), rng.normal(0.0, 0.2, samples)):
        result = FMHT4.decode(rx_audio.downsample(audio))
        assert result["payload"] is None
        assert result["crc_ok"] is False
