import numpy as np
import pytest
from scipy import signal

from experiments.hc2_32qam import hc2_32qam as hc2
from whale.channel import AwgnChannel, SnrKind, SnrSpec
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


def _awgn_capture(payload, *, lead, snr_db, seed, tail=4_096):
    """Padded capture through one seeded AWGN realization.

    Same shape and SNR reference convention as ``benchmark_hc2_snr``: the
    padding reaches the receiver as noise-only audio, and the requested
    waveform SNR is referenced to the signal-bearing span only.
    """
    frame = hc2.modulate(payload)
    capture = np.concatenate((np.zeros(lead, np.float32), frame,
                              np.zeros(tail, np.float32)))
    spec = SnrSpec(snr_db, SnrKind.WAVEFORM, reference_start=lead,
                   reference_stop=lead + len(frame))
    channel = AwgnChannel(hc2.SAMPLE_RATE, spec, seed)
    return np.asarray(channel.process(capture).audio, dtype=float)


def test_training_symbols_are_distinct_and_barely_cross_correlate():
    """The acquisition template must not match training symbol 2.

    HC2 originally sent one training sequence twice, so the matched filter
    had two near-equal peaks one symbol apart and needed an earliest-lag
    tie-break to choose between them.  Distinct sequences must leave the
    second symbol looking nothing like the template.
    """
    assert hc2.TRAINING_SYMBOLS == 2
    assert not np.allclose(hc2._TRAINING[0], hc2._TRAINING[1])
    # Both are constant-modulus QPSK on all 49 carriers, so neither training
    # symbol can leave a carrier unexcited for the channel estimate.
    assert np.allclose(np.abs(hc2._TRAINING), 1.0)

    template = signal.hilbert(ofdm.build_symbol(hc2.GEOMETRY, hc2._TRAINING[0]))
    second = signal.hilbert(ofdm.build_symbol(hc2.GEOMETRY, hc2._TRAINING[1]))
    correlation = signal.correlate(second, template, mode="valid")
    energy = signal.convolve(np.abs(second) ** 2, np.ones(len(template)),
                             mode="valid")
    metric = np.abs(correlation) ** 2 / (energy * np.vdot(template, template).real)
    # The retired tie-break treated anything above 0.995 as a tie; the true
    # peak measured only ~0.994 of the false one.  Symbol 2 now scores <1%.
    assert float(np.max(metric)) < 0.01


@pytest.mark.parametrize("seed", [15, 34, 39])
def test_acquisition_never_locks_onto_the_second_training_symbol(seed):
    """Regression for the one-symbol (1,152-sample) mis-acquisition.

    These three AWGN realizations at 13 dB waveform SNR all acquired
    ``lead + SYMBOL_SAMPLES`` under the identical-training design and lost
    the frame.  With distinct training symbols the matched filter has a
    single maximum, so the acquired start must be the true one.
    """
    lead = 4_096
    payload = np.random.default_rng(seed).integers(
        0, 256, hc2.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    capture = _awgn_capture(payload, lead=lead, snr_db=13.0, seed=seed)
    decoded, diagnostics = hc2.demodulate(
        capture, max_frequency_offset_hz=2.0, acquisition_step_hz=2.0,
        return_diagnostics=True)
    assert diagnostics["start_sample"] != lead + hc2.SYMBOL_SAMPLES
    assert diagnostics["start_sample"] == lead
    assert decoded == payload
