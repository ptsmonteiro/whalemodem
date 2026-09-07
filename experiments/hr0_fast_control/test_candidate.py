"""Offline candidate framing, acquisition, and corruption checks."""
import numpy as np
import pytest
from scipy.signal import hilbert
from whale import rx_audio
from experiments.hr0_fast_control.candidate import FAST16, FAST32, MARGIN32

@pytest.mark.parametrize('mode', [FAST16, FAST32, MARGIN32])
@pytest.mark.parametrize('length', [0, 12, 13, 42])
def test_round_trip_and_airtime(mode, length):
    payload = bytes(range(length))
    audio = mode.encode(payload)
    assert len(audio)/48000 == pytest.approx(mode.airtime(length))
    assert mode.decode(rx_audio.downsample(audio))['payload'] == payload
    assert mode.airtime(12) <= (1.82 if mode is MARGIN32 else 1.5)

@pytest.mark.parametrize('mode', [FAST16, FAST32, MARGIN32])
@pytest.mark.parametrize('offset', [-46, 46])
def test_blind_acquisition_with_delay_and_cfo(mode, offset):
    payload = bytes(range(12))
    audio = mode.encode(payload)
    shifted = np.real(hilbert(audio) * np.exp(2j*np.pi*offset*np.arange(len(audio))/48000))
    capture = np.concatenate((np.zeros(7137), shifted, np.zeros(1024)))
    assert mode.decode(rx_audio.downsample(capture.astype(np.float32)))['payload'] == payload

@pytest.mark.parametrize('mode', [FAST16, FAST32, MARGIN32])
def test_rejects_noise_truncation_and_oversize(mode):
    audio = mode.encode(bytes(range(12)))
    assert mode.decode(rx_audio.downsample(audio[:len(audio)//2]))['payload'] is None
    assert mode.decode(np.random.default_rng(76).normal(0,.1,12000))['payload'] is None
    with pytest.raises(ValueError):
        mode.encode(bytes(43))

@pytest.mark.parametrize('mode', [FAST16, FAST32, MARGIN32])
def test_ack_reports_head_feedback_for_handshake(mode):
    from whale.modes import hf_lead
    decoded = mode.decode(rx_audio.downsample(mode.encode(bytes(range(12)))))
    assert decoded['head_blocks_observed'] >= hf_lead.MIN_BLOCKS
    assert decoded['head_seconds_received'] > 0
    assert decoded['head_match'] >= hf_lead.MATCH_THRESHOLD

@pytest.mark.parametrize('mode', [FAST16, FAST32, MARGIN32])
def test_wrong_shape_is_rejected(mode):
    assert mode.decode(np.zeros((100, 2)))['payload'] is None

@pytest.mark.parametrize('mode', [FAST16, FAST32, MARGIN32])
def test_adapter_timing_surface(mode):
    from whale.modes import hf_lead
    assert mode.baud > 0
    assert mode.head_match_allowance_seconds == hf_lead.BLOCK_SAMPLES / 48000


@pytest.mark.parametrize('mode', [FAST16, FAST32, MARGIN32])
def test_full_prefix_is_pending_but_complete_corrupt_body_is_consumable(mode):
    audio = mode.encode(bytes(range(42)))
    prefix = audio[:round(mode.airtime(12) * mode.tx_sample_rate)]
    pending = mode.decode(rx_audio.downsample(prefix))
    assert pending['payload'] is None
    assert 'end_index' not in pending
    from whale.modes import hf_lead
    body_start = hf_lead.MIN_SAMPLES + mode.sync_symbols * mode.symbol_samples
    audio[body_start:] = 0
    failed = mode.decode(rx_audio.downsample(audio))
    assert failed['payload'] is None
    assert failed['end_index'] > failed['sync_end_index']
