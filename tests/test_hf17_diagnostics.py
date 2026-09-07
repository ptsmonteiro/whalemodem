"""Opt-in receiver experiments must preserve exact clean-channel delivery."""
import numpy as np
import pytest
from dataclasses import replace
from whale import rx_audio
from experiments.hf10_ofdm49_v6 import ofdm49_v6 as phy


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
