import numpy as np
import pytest
from scipy import signal

from experiments.hc2_32qam import hc2_32qam as hc2
from whale.dsp import ofdm


def _benign_capture(payload, *, lead, frequency_hz, phase_wobble=0.0):
    original = hc2.modulate(payload)
    carriers = np.vstack([
        ofdm.symbol_carriers(
            hc2.GEOMETRY,
            original[i * hc2.SYMBOL_SAMPLES:(i + 1) * hc2.SYMBOL_SAMPLES])
        for i in range(hc2.TOTAL_SYMBOLS)
    ])
    # Smooth, deterministic frequency-selective amplitude and phase response.
    x = np.linspace(-1.0, 1.0, hc2.N_CARRIERS)
    channel = (0.78 + 0.17 * np.cos(2.3 * np.pi * x)) * np.exp(
        1j * (0.65 * x + 0.13 * np.sin(3 * np.pi * x)))
    shaped = np.concatenate([
        ofdm.build_symbol(hc2.GEOMETRY, row * channel) for row in carriers
    ])
    n = np.arange(len(shaped))
    symbol_position = n / hc2.SYMBOL_SAMPLES
    phase = (2 * np.pi * frequency_hz * n / hc2.SAMPLE_RATE
             + phase_wobble * np.sin(2 * np.pi * symbol_position / 37.0))
    impaired = signal.hilbert(shaped) * np.exp(1j * phase)
    return np.concatenate((np.zeros(lead), impaired.real, np.zeros(193)))


@pytest.mark.parametrize("lead,frequency_hz", [(173, -15.0), (731, 7.4), (37, 15.0)])
def test_acquires_cfo_equalizes_and_recovers_exact_payload(lead, frequency_hz):
    payload = np.random.default_rng(1000 + lead).integers(
        0, 256, hc2.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    capture = _benign_capture(payload, lead=lead, frequency_hz=frequency_hz)
    decoded, diagnostics = hc2.demodulate(capture, return_diagnostics=True)
    assert decoded == payload
    # A frequency-selective channel can move the matched-filter maximum a few
    # samples within the cyclic prefix without changing the FFT result.
    assert diagnostics["start_sample"] == pytest.approx(lead, abs=8)
    assert diagnostics["frequency_offset_hz"] == pytest.approx(frequency_hz, abs=0.08)


def test_tracks_smooth_common_phase_wobble_and_preserves_rate():
    payload = np.random.default_rng(44).integers(
        0, 256, hc2.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    capture = _benign_capture(payload, lead=419, frequency_hz=11.25,
                              phase_wobble=0.18)
    assert hc2.demodulate(capture) == payload
    assert hc2.SUSTAINED_USER_BIT_RATE == pytest.approx(7510.928961748634)
    assert hc2.SUSTAINED_USER_BIT_RATE > 7050
