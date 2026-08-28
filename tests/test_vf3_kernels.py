"""The two vectorized VF3 kernels, pinned to the scalar trellis and loop.

`convolutional_decode_soft` and `_estimate_timing` used to be pure-Python
loops; they were the bulk of VF3's decode cost.  Both were rewritten as
numpy, and both are required to be *bit-for-bit* what they were, since the
whole decode -- survivor selection, timing regression, CRC -- hangs off
them.  The scalar originals live here as references and the vectorized
versions are held to them exactly, on real frames, on noise, and on the
degenerate soft-bit vectors (zeros, integer ties) where tie-breaking is the
only thing that decides the answer.

Software only -- no radios, no sound cards.
"""

import numpy as np
import pytest
from scipy.signal import hilbert

from whale import rx_audio
from whale.dsp.fec import ConvolutionalCode
from whale.modes import vf3
from whale.modes.vf3 import (RX_CORE_SAMPLES as CORE_SAMPLES,
                             RX_GUARD_SAMPLES as GUARD_SAMPLES,
                             RX_SYMBOL_SAMPLES as SYMBOL_SAMPLES,
                             SYNC_SYMBOLS, TOTAL_SYMBOLS)

RNG = np.random.default_rng(20260828)


def _scalar_decode_soft(soft_bits):
    """The original per-transition Viterbi walk."""
    soft_bits = np.asarray(soft_bits, dtype=np.float64).reshape(-1)
    steps = len(soft_bits) // 2
    metrics = np.full(64, np.inf)
    metrics[0] = 0.0
    previous = np.empty((steps, 64), dtype=np.uint8)
    inputs = np.empty((steps, 64), dtype=np.uint8)
    transitions = []
    for state in range(64):
        for bit in (0, 1):
            register = ((state << 1) | bit) & 0x7F
            pair = ((register & 0o171).bit_count() & 1,
                    (register & 0o133).bit_count() & 1)
            transitions.append((state, bit, register & 0x3F,
                                (1 - 2 * pair[0], 1 - 2 * pair[1])))
    for t in range(steps):
        received0, received1 = soft_bits[2 * t:2 * t + 2]
        new_metrics = np.full(64, np.inf)
        for state, bit, next_state, signs in transitions:
            metric = (metrics[state] - signs[0] * received0
                      - signs[1] * received1)
            if metric < new_metrics[next_state]:
                new_metrics[next_state] = metric
                previous[t, next_state] = state
                inputs[t, next_state] = bit
        metrics = new_metrics
    decoded = np.empty(steps, dtype=np.uint8)
    state = 0
    for t in range(steps - 1, -1, -1):
        decoded[t] = inputs[t, state]
        state = int(previous[t, state])
    return decoded


def _scalar_estimate_timing(analytic, start):
    """The original symbol-by-symbol, shift-by-shift prefix correlation."""
    indices = np.arange(SYNC_SYMBOLS, TOTAL_SYMBOLS, dtype=np.int32)
    shifts = np.empty(len(indices))
    scores = np.empty(len(indices))
    for out_index, symbol_index in enumerate(indices):
        predicted = start + symbol_index * SYMBOL_SAMPLES
        best_score, best_shift = -1.0, 0
        for shift in range(-32, 33):
            at = predicted + shift
            if at < 0 or at + SYMBOL_SAMPLES > len(analytic):
                continue
            prefix = analytic[at:at + GUARD_SAMPLES]
            tail = analytic[at + CORE_SAMPLES:at + SYMBOL_SAMPLES]
            denominator = np.sqrt(
                np.vdot(prefix, prefix).real * np.vdot(tail, tail).real)
            score = float(abs(np.vdot(prefix, tail)) / max(denominator, 1e-30))
            if score > best_score:
                best_score, best_shift = score, shift
        shifts[out_index] = best_shift
        scores[out_index] = max(best_score, 0.0)
    slope, intercept = np.polyfit(indices, shifts, 1)
    return float(intercept), float(slope), float(np.median(scores))


def _frame(snr_db=None):
    payload = bytes(RNG.integers(0, 256, vf3.MAX_PAYLOAD_BYTES, dtype=np.uint8))
    audio = vf3.modulate(payload)
    if snr_db is not None:
        level = np.sqrt(np.mean(audio ** 2)) * 10 ** (-snr_db / 20)
        audio = audio + RNG.normal(0, level, len(audio))
    return payload, audio


def _live_soft_bits(audio):
    """The soft bits the real decode path hands to the Viterbi decoder.

    The spy goes on `ConvolutionalCode.decode_soft` itself rather than on
    a name re-exported by `vf3`, so it stays attached to the kernel the
    decode path actually calls no matter how the mode wires itself up.
    Patching a re-export silently captured nothing once VF3 started
    reaching the decoder through its `PacketCodec`, which turned this
    whole test into a skip.
    """
    captured = []
    original = ConvolutionalCode.decode_soft

    def spy(self, soft_bits):
        captured.append(np.asarray(soft_bits, np.float64).reshape(-1).copy())
        return original(self, soft_bits)

    ConvolutionalCode.decode_soft = spy
    try:
        vf3.demodulate(rx_audio.downsample(audio))
    finally:
        ConvolutionalCode.decode_soft = original
    return captured


# -- the soft Viterbi decoder ---------------------------------------------

@pytest.mark.parametrize("snr_db", [None, 6.0, 0.0])
def test_viterbi_matches_the_scalar_trellis_on_a_real_frame(snr_db):
    _, audio = _frame(snr_db)
    captured = _live_soft_bits(audio)
    if not captured:
        # A clean frame must always reach the decoder.  Skipping there
        # would hide a broken decode path -- or a spy that stopped being
        # attached to it -- behind a green run.
        assert snr_db is not None, "a noiseless frame failed to decode"
        pytest.skip("acquisition failed at this SNR, so nothing was decoded")
    for soft_bits in captured:
        assert np.array_equal(vf3.convolutional_decode_soft(soft_bits),
                              _scalar_decode_soft(soft_bits))


def test_viterbi_matches_the_scalar_trellis_on_degenerate_inputs():
    vectors = [np.zeros(600),                        # every branch ties
               RNG.integers(-1, 2, 600).astype(float),   # ties everywhere
               np.tile([1.0, -1.0, 0.0, 0.0], 150),  # ties on half the steps
               np.full(600, -3.0),
               RNG.normal(size=2)]
    vectors += [RNG.normal(size=2 * int(RNG.integers(1, 400)))
                for _ in range(8)]
    for soft_bits in vectors:
        assert np.array_equal(vf3.convolutional_decode_soft(soft_bits),
                              _scalar_decode_soft(soft_bits))


def test_viterbi_still_rejects_an_odd_soft_bit_count():
    with pytest.raises(ValueError):
        vf3.convolutional_decode_soft(np.zeros(7))


# -- the cyclic-prefix timing estimator -----------------------------------

@pytest.mark.parametrize("snr_db", [None, 12.0, 3.0])
def test_timing_matches_the_scalar_loop_on_a_real_frame(snr_db):
    _, audio = _frame(snr_db)
    analytic = hilbert(rx_audio.downsample(audio))
    nominal = (vf3.LEAD_IN_SAMPLES // rx_audio.DECIMATION
               + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    for start in (nominal, nominal + 7):
        assert (vf3._estimate_timing(analytic, start)
                == _scalar_estimate_timing(analytic, start))


def test_timing_matches_the_scalar_loop_when_windows_fall_off_the_signal():
    """Starts where some -- or every -- shift is outside the buffer."""
    _, audio = _frame()
    analytic = hilbert(rx_audio.downsample(audio))
    for start in (0, 3, 40, len(analytic) - 1_200, len(analytic) - 100,
                  len(analytic)):
        assert (vf3._estimate_timing(analytic, start)
                == _scalar_estimate_timing(analytic, start))


@pytest.mark.parametrize("signal", ["silence", "noise"])
def test_timing_matches_the_scalar_loop_on_signals_with_no_prefix(signal):
    samples = (np.zeros(50_000) if signal == "silence"
               else RNG.normal(size=50_000))
    analytic = hilbert(samples)
    for start in (0, vf3.LEAD_IN_SAMPLES // rx_audio.DECIMATION, 40_000):
        assert (vf3._estimate_timing(analytic, start)
                == _scalar_estimate_timing(analytic, start))
