"""Opt-in receiver experiments must preserve exact clean-channel delivery."""
import numpy as np
import pytest
from dataclasses import replace
from whale import rx_audio
from whale.phy import ofdm49 as phy


@pytest.mark.parametrize('bps', [4, 6])
def test_training_only_receiver_options(bps):
    mode = phy.OFDM49Mode(240, 60, tuple(phy.bins_in_band(240)), bps, 182,
                          pilot_interval=8, fec_rate='3/4', drive_scale=1)
    payload = np.random.default_rng(57).integers(0,256,176,dtype=np.uint8).tobytes()
    audio = rx_audio.downsample(np.pad(mode.modulate(payload), (480,480)))
    for width in (1,3):
        result = mode.demodulate(audio, diagnostics=True, gain_smoothing=width,
                                 noise_estimator='repeat')
        assert result['payload'] == payload
        assert np.isfinite(result['equalized_symbols']).all()
        assert result['noise_estimator'] == 'repeat'


@pytest.mark.parametrize('tracking', ['common', 'residual', 'confidence'])
def test_distributed_pilots_track_data_symbol_amplitude(tracking):
    mode = phy.OFDM49Mode(240,60,tuple(phy.bins_in_band(240)),6,350,
        pilot_interval=20,pilot_comb_stride=12,comb_tracking=tracking,drive_scale=1)
    payload = np.random.default_rng(61).integers(0,256,344,dtype=np.uint8).tobytes()
    tx = mode.modulate(payload).astype(float)
    grid = tx.reshape(-1,mode.symbol_len*4)
    # Change only data-symbol amplitude: block training cannot see this.
    grid[2:-1] *= np.resize([0.72,1.18],len(grid)-3)[:,None]
    audio = rx_audio.downsample(np.pad(tx,(480,480)))
    assert mode.demodulate(audio)['payload'] == payload
    assert replace(mode,comb_tracking='off').demodulate(audio)['payload'] is None


def test_repeat_noise_variance_uses_each_data_symbols_equalizer_gain(monkeypatch):
    mode = phy.OFDM49Mode(
        240, 24, tuple(phy.bins_in_band(240, 300, 2700)), 2, 121,
        pilot_interval=5, fec_rate='1/2', interleave=True, drive_scale=1)
    payload = np.random.default_rng(71).integers(
        0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()
    tx = mode.modulate(payload).astype(float)
    symbol_samples = mode.symbol_len * 4
    symbols = tx.reshape(-1, symbol_samples)
    # Give successive pilot blocks visibly different channel gains. The
    # receiver interpolates these anchors and must use that exact gain again
    # when translating raw repeated-preamble noise into LLR variance.
    symbols *= np.linspace(0.55, 1.45, len(symbols))[:, None]
    tx += np.random.default_rng(72).normal(0, 0.002, len(tx))
    audio = rx_audio.downsample(np.pad(tx, (480, 480)))

    observed = {}
    original = phy._soft_bit_llrs

    def capture(symbol_values, bits_per_symbol, noise_variance):
        observed['variance'] = np.asarray(noise_variance).copy()
        return original(symbol_values, bits_per_symbol, noise_variance)

    monkeypatch.setattr(phy, '_soft_bit_llrs', capture)
    result = mode.demodulate(
        audio, diagnostics=True, noise_estimator='repeat')
    assert 'variance' in observed

    data_symbol_indices = []
    cursor = mode.n_preamble_symbols
    for kind, count in mode._layout():
        if kind == 'data':
            data_symbol_indices.extend(range(cursor, cursor + count))
        cursor += count
    data_symbol_indices = np.asarray(data_symbol_indices)
    anchors_idx = result['anchor_index']
    anchors_gain = result['anchor_gain']
    expected_gain = np.empty((mode.n_data_ofdm_symbols, mode.n_active), complex)
    for bin_index in range(mode.n_active):
        expected_gain[:, bin_index] = (
            np.interp(data_symbol_indices, anchors_idx,
                      anchors_gain[:, bin_index].real)
            + 1j * np.interp(data_symbol_indices, anchors_idx,
                             anchors_gain[:, bin_index].imag))

    variance = observed['variance'].reshape(
        mode.n_data_ofdm_symbols, mode.n_data_bins)
    raw_units = variance * np.abs(expected_gain[:, mode._data_idx]) ** 2
    # Every row must recover the one raw repeat-variance vector. This checks
    # both per-symbol gain scaling and row-major alignment with data symbols.
    assert np.allclose(raw_units, raw_units[0], rtol=1e-10, atol=1e-12)
    assert not np.allclose(variance[0], variance[-1], rtol=0.05, atol=0)
