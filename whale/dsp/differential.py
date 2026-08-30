"""Differentially encoded M-PSK, per carrier.

Information rides in the phase *change* from one symbol to the next on the
same carrier, so a static phase error -- an uncorrected frequency offset, a
channel rotation the equalizer did not fully remove -- cancels instead of
destroying the constellation.  The cost is roughly 3 dB against coherent
QPSK and error pairing, since one bad symbol corrupts two differences.

This is what VF3 puts on the air, at the default M=4 (QPSK).  Every function
below takes the constellation as an optional `points`/`labels` pair so a
higher-order mode (HC2's differential 8-PSK) can reuse the same kernels
instead of duplicating them; omitting them reproduces the original QPSK
behaviour bit-for-bit, which is what VF3 and HC1 rely on.
"""

from __future__ import annotations

import numpy as np

# Phase increments, and the bit pair each one encodes.
POINTS = np.array([1.0 + 0j, 0.0 + 1j, 0.0 - 1j, -1.0 + 0j])
LABELS = np.array([[0, 0], [0, 1], [1, 0], [1, 1]], dtype=np.uint8)


def gray_psk(bits_per_symbol: int) -> tuple[np.ndarray, np.ndarray]:
    """A Gray-coded M-PSK constellation, M = 2**bits_per_symbol.

    Gray coding puts adjacent points one bit apart, so a phase slip to a
    neighbouring point -- the common error at high SNR -- costs one coded
    bit instead of risking both.  Point 0 is always +1, matching QPSK's own
    `POINTS[0]` and keeping `gray_psk(2)` numerically consistent with the
    hand-written table above (though not identical -- see below).
    """
    if bits_per_symbol < 1:
        raise ValueError("need at least one bit per symbol")
    m = 1 << bits_per_symbol
    index = np.arange(m)
    gray = index ^ (index >> 1)
    points = np.exp(2j * np.pi * gray / m)
    shifts = np.arange(bits_per_symbol - 1, -1, -1)
    labels = ((index[:, None] >> shifts[None, :]) & 1).astype(np.uint8)
    return points, labels


def encode(bits: np.ndarray, initial: np.ndarray, symbols: int,
           carriers: int, points: np.ndarray = POINTS,
           labels: np.ndarray = LABELS) -> np.ndarray:
    """Accumulate bit groups into absolute phases, independently per carrier."""
    bits_per_symbol = labels.shape[1]
    weights = (1 << np.arange(bits_per_symbol - 1, -1, -1)).astype(np.int64)
    groups = np.asarray(bits, dtype=np.uint8).reshape(
        symbols, carriers, bits_per_symbol)
    index = groups.astype(np.int64) @ weights
    increments = points[index]
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


def _scores(values: np.ndarray, points: np.ndarray = POINTS) -> np.ndarray:
    """Projection of each observation onto each constellation point.

    For unit-magnitude `points` this is proportional to Euclidean distance,
    so the argmax is the nearest-point decision; the QPSK default reduces to
    the original hand-written `(re, im, -im, -re)` table exactly, since
    `Re(v * conj(1))=re(v)`, `Re(v * conj(j))=im(v)`, and so on.
    """
    values = np.asarray(values)
    return (values[..., None] * np.conj(points)).real


def decisions(values: np.ndarray, points: np.ndarray = POINTS) -> np.ndarray:
    """Nearest constellation point to each observation."""
    return points[np.argmax(_scores(values, points), axis=-1)]


def hard_bits(values: np.ndarray, points: np.ndarray = POINTS,
             labels: np.ndarray = LABELS) -> np.ndarray:
    return labels[np.argmax(_scores(values, points), axis=-1)].reshape(-1)


def soft_bits(values: np.ndarray, weights: np.ndarray,
             points: np.ndarray = POINTS,
             labels: np.ndarray = LABELS) -> np.ndarray:
    """Max-log bit reliabilities; positive means bit zero.

    `weights` scales each carrier's contribution -- see
    `whale.dsp.equalize.carrier_weights` -- so a faded carrier informs the
    Viterbi decoder less than a clean one.  Generalizes the QPSK-specific
    two-line LLR to any labelled constellation: per bit position, the LLR is
    the best score among points with that bit 0 minus the best among points
    with that bit 1.  For the QPSK default this is exactly the original
    `llr0`/`llr1` pair (verified in `tests/test_dsp_kernels.py`).
    """
    scores = _scores(values, points)
    bits_per_symbol = labels.shape[1]
    llrs = np.empty(scores.shape[:-1] + (bits_per_symbol,))
    for bit in range(bits_per_symbol):
        zero = labels[:, bit] == 0
        llrs[..., bit] = (np.max(scores[..., zero], axis=-1)
                          - np.max(scores[..., ~zero], axis=-1))
    return (llrs * np.asarray(weights)[None, :, None]).reshape(-1)
