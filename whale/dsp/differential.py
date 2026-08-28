"""Differentially encoded QPSK, per carrier.

Information rides in the phase *change* from one symbol to the next on the
same carrier, so a static phase error -- an uncorrected frequency offset, a
channel rotation the equalizer did not fully remove -- cancels instead of
destroying the constellation.  The cost is roughly 3 dB against coherent
QPSK and error pairing, since one bad symbol corrupts two differences.

This is what VF3 puts on the air.
"""

from __future__ import annotations

import numpy as np

# Phase increments, and the bit pair each one encodes.
POINTS = np.array([1.0 + 0j, 0.0 + 1j, 0.0 - 1j, -1.0 + 0j])
LABELS = np.array([[0, 0], [0, 1], [1, 0], [1, 1]], dtype=np.uint8)


def encode(bits: np.ndarray, initial: np.ndarray, symbols: int,
           carriers: int) -> np.ndarray:
    """Accumulate bit pairs into absolute phases, independently per carrier."""
    pairs = np.asarray(bits, dtype=np.uint8).reshape(symbols, carriers, 2)
    increments = POINTS[2 * pairs[:, :, 0] + pairs[:, :, 1]]
    output = np.empty_like(increments)
    previous = np.asarray(initial, dtype=np.complex128)
    for i in range(symbols):
        output[i] = previous * increments[i]
        previous = output[i]
    return output


def observations(values: np.ndarray, initial: np.ndarray) -> np.ndarray:
    """Recover unit-magnitude phase increments from received symbols."""
    values = np.asarray(values, dtype=np.complex128)
    previous = np.vstack((np.asarray(initial)[None, :], values[:-1]))
    differential = values * np.conj(previous)
    return differential / np.maximum(np.abs(differential), 1e-30)


def _scores(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    return np.stack(
        (values.real, values.imag, -values.imag, -values.real), axis=-1)


def decisions(values: np.ndarray) -> np.ndarray:
    """Nearest constellation point to each observation."""
    return POINTS[np.argmax(_scores(values), axis=-1)]


def hard_bits(values: np.ndarray) -> np.ndarray:
    return LABELS[np.argmax(_scores(values), axis=-1)].reshape(-1)


def soft_bits(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Max-log bit reliabilities; positive means bit zero.

    `weights` scales each carrier's contribution -- see
    `whale.dsp.equalize.carrier_weights` -- so a faded carrier informs the
    Viterbi decoder less than a clean one.
    """
    scores = _scores(values)
    llr0 = np.maximum(scores[..., 0], scores[..., 1]) - np.maximum(
        scores[..., 2], scores[..., 3])
    llr1 = np.maximum(scores[..., 0], scores[..., 2]) - np.maximum(
        scores[..., 1], scores[..., 3])
    return (np.stack((llr0, llr1), axis=-1)
            * np.asarray(weights)[None, :, None]).reshape(-1)
