"""Software conformance for the configurable experimental FM OFDM mode."""

import numpy as np
import pytest
from scipy.signal import hilbert

from whale import framing, rx_audio, waveform
from whale.modes.vf12 import Vf12Mode, VF12, mode_for


def test_default_mode_contract_and_data_header_accounting():
    assert isinstance(VF12, waveform.WaveformMode)
    assert VF12.mode_id == 18
    assert VF12.carrier_spacing_hz == 50
    assert VF12.n_carriers == 51
    assert VF12.bits_per_carrier == 4
    assert VF12.lead_in_seconds == framing.FM_SETTLING_HEAD_SECONDS
    assert VF12.chunk_size + framing.AIR_HEADER_BYTES == VF12.max_payload_bytes
    assert VF12.airtime(VF12.max_payload_bytes) == pytest.approx(4.978, abs=.002)


def test_parameterized_geometries_are_independent():
    low = mode_for(bits_per_symbol=2, band_lo_hz=300, band_hi_hz=2000,
                   cp_len=24, n_codewords=10)
    high = mode_for(bits_per_symbol=6, band_lo_hz=1000, band_hi_hz=2900,
                    cp_len=48, n_codewords=10)
    assert low.n_carriers != high.n_carriers
    assert low.bits_per_carrier == 2 and high.bits_per_carrier == 6
    assert low.active_bins[0] == 6 and high.active_bins[0] == 20
    assert low.geometry()["cp_len"] == 24
    assert high.geometry()["cp_len"] == 48
    assert low.active_bins != high.active_bins


@pytest.mark.parametrize("order", range(2, 7))
def test_all_constellation_orders_loopback(order):
    mode = mode_for(bits_per_symbol=order, band_lo_hz=500,
                    band_hi_hz=2500, n_codewords=8)
    payload = np.random.default_rng(order).bytes(mode.max_payload_bytes)
    assert mode.decode(rx_audio.downsample(mode.encode(payload)))["payload"] == payload


def test_64qam_full_capacity_clean_loopback():
    # pilot_comb_stride=6: the FM-tuned default of 8 is too coarse for the
    # comb-pilot frequency interpolation to support 64-QAM (bits_per_carrier=6).
    mode = mode_for(bits_per_symbol=6, band_lo_hz=500, band_hi_hz=2900,
                    n_codewords=75, pilot_comb_stride=6)
    payload = np.arange(mode.max_payload_bytes, dtype=np.uint8).tobytes()
    result = mode.decode(rx_audio.downsample(mode.encode(payload)))
    assert result["payload"] == payload
    assert result["crc_ok"] is True


def test_preamble_has_300ms_startup_mute_and_500ms_keyed_lead_in():
    payload = np.random.default_rng(300).bytes(VF12.max_payload_bytes)
    audio = VF12.encode(payload)
    audio[:round(.3 * VF12.tx_sample_rate)] = 0
    captured = rx_audio.downsample(np.concatenate((np.zeros(3200), audio)))
    assert VF12.decode(captured)["payload"] == payload


def test_fixed_50hz_spacing_is_rejected():
    with pytest.raises(ValueError, match="50 Hz"):
        mode_for(carrier_spacing_hz=25)


def test_pilot_positions_present_every_symbol_and_at_band_edges():
    mode = VF12
    assert len(mode.pilot_positions) > 0
    assert mode.pilot_positions[0] == 0
    assert mode.pilot_positions[-1] == mode.n_carriers - 1
    payload = np.random.default_rng(7).bytes(mode.max_payload_bytes)
    audio = mode.encode(payload)
    result = mode.decode(rx_audio.downsample(audio))
    assert result["crc_ok"] is True
    # Every payload symbol carries the same known pilot BPSK sequence at the comb
    # bins: with a static, noiseless channel the per-bin ratio to the pilot value
    # (i.e. the fitted channel) should be near-constant across time.
    start = result["start_index"] + 4 * mode.symbol_samples
    block = mode._extract(rx_audio.downsample(audio), start, 4 + mode.payload_symbols)
    pilot_over_time = block[4:][:, mode.pilot_positions] / mode.pilot_values
    for column in pilot_over_time.T:
        np.testing.assert_allclose(column, column[0], rtol=0.15, atol=1e-2)
    assert mode.bits_per_symbol == len(mode.data_positions) * mode.bits_per_carrier


def test_pilot_comb_stride_zero_restores_static_header_path():
    mode = mode_for(bits_per_symbol=4, band_lo_hz=500, band_hi_hz=2900,
                    n_codewords=30, pilot_comb_stride=0)
    assert len(mode.pilot_positions) == 0
    assert len(mode.data_positions) == mode.n_carriers
    assert mode.bits_per_symbol == mode.n_carriers * mode.bits_per_carrier
    payload = np.random.default_rng(8).bytes(mode.max_payload_bytes)
    result = mode.decode(rx_audio.downsample(mode.encode(payload)))
    assert result["crc_ok"] is True
    assert result["payload"] == payload


@pytest.mark.parametrize("order", range(2, 7))
def test_comb_pilot_loopback_all_constellation_orders(order):
    mode = mode_for(bits_per_symbol=order, band_lo_hz=500, band_hi_hz=2500,
                    n_codewords=8, pilot_comb_stride=6, pilot_time_span=3)
    payload = np.random.default_rng(100 + order).bytes(mode.max_payload_bytes)
    result = mode.decode(rx_audio.downsample(mode.encode(payload)))
    assert result["crc_ok"] is True
    assert result["payload"] == payload


def _drift_and_noise(audio, lead_samples, max_deg, snr_db, seed):
    n = len(audio)
    t = np.clip(np.arange(n) - lead_samples, 0, None) / max(n - lead_samples, 1)
    phase = np.deg2rad(max_deg) * t
    drifted = (hilbert(audio) * np.exp(1j * phase)).real
    rng = np.random.default_rng(seed)
    sig_power = np.mean(drifted ** 2)
    noise = rng.normal(0, np.sqrt(sig_power / (10 ** (snr_db / 10))), n)
    return (drifted + noise).astype(np.float32)


def test_comb_pilot_tracking_survives_phase_drift_and_noise():
    tracked = mode_for(bits_per_symbol=4, band_lo_hz=500, band_hi_hz=2900, n_codewords=30)
    payload = np.random.default_rng(4).bytes(tracked.max_payload_bytes)
    audio = tracked.encode(payload)
    lead_samples = round(tracked.lead_in_seconds * tracked.tx_sample_rate)
    impaired = _drift_and_noise(audio, lead_samples, 120.0, 15.0, 1)
    result = tracked.decode(rx_audio.downsample(impaired))
    assert result["crc_ok"] is True
    assert result["payload"] == payload

    static = mode_for(bits_per_symbol=4, band_lo_hz=500, band_hi_hz=2900,
                      n_codewords=30, pilot_comb_stride=0)
    static_audio = static.encode(payload)
    static_impaired = _drift_and_noise(static_audio, lead_samples, 120.0, 15.0, 1)
    static_result = static.decode(rx_audio.downsample(static_impaired))
    assert static_result["crc_ok"] is False


def test_pilot_time_span_one_reports_plausible_snr_not_the_self_referential_floor():
    # Regression for the noise estimator's self-referential bias: at
    # pilot_time_span == 1 the tracked channel R exactly reproduces the observed
    # pilot, so a noise estimate derived from residuals against R is zero by
    # construction and floors at snr_db == 50.0. The fix estimates noise from
    # consecutive-symbol pilot residual differences instead, which stays
    # plausible regardless of the smoothing window.
    mode = mode_for(bits_per_symbol=4, band_lo_hz=500, band_hi_hz=2900,
                    n_codewords=30, pilot_time_span=1)
    payload = np.random.default_rng(4).bytes(mode.max_payload_bytes)
    audio = mode.encode(payload)
    lead_samples = round(mode.lead_in_seconds * mode.tx_sample_rate)
    impaired = _drift_and_noise(audio, lead_samples, 60.0, 16.0, 1)
    result = mode.decode(rx_audio.downsample(impaired))
    assert result["snr_db"] < 35.0
