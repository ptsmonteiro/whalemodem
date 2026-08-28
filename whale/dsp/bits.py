"""Bit-level kernels: PN sequences, whitening and QPSK mapping.

Lifted from `whale/modes/_primitives.py`, which took them verbatim from
`experiments/vf2/vf2.py`.  The VF2..VF5 bench results and every capture
under `experiments/*/results/` were produced against these exact
mappings, so they are held bit-identical rather than tidied.
"""

from __future__ import annotations

import numpy as np


def pn_bits(count: int, seed: int) -> np.ndarray:
    """Deterministic order-17 PN sequence, returned as uint8 bits.

    Used for sync and training constellations and for payload whitening.
    The taps are fixed: changing them changes every mode's on-air signal.
    """
    state = seed & 0x1FFFF
    if state == 0:
        raise ValueError("LFSR seed must be non-zero")
    out = np.empty(count, dtype=np.uint8)
    for i in range(count):
        out[i] = state & 1
        feedback = ((state >> 0) ^ (state >> 3)) & 1
        state = (state >> 1) | (feedback << 16)
    return out


def qpsk_from_bits(bits: np.ndarray) -> np.ndarray:
    """Map pairs [real-sign bit, imag-sign bit] to unit-energy QPSK."""
    bits = np.asarray(bits, dtype=np.uint8)
    if bits.size % 2:
        raise ValueError("QPSK needs an even number of bits")
    pairs = bits.reshape(-1, 2)
    return ((1.0 - 2.0 * pairs[:, 0])
            + 1j * (1.0 - 2.0 * pairs[:, 1])) / np.sqrt(2.0)


def bits_from_qpsk(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    # Keep the final axis carrier-major: [Re0, Im0, Re1, Im1, ...].
    # column_stack would group all real bits before all imaginary bits when
    # `values` is a 2-D symbol/carrier grid.
    return np.stack((values.real < 0.0, values.imag < 0.0), axis=-1).astype(
        np.uint8).reshape(-1)


def slice_qpsk(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    real = np.where(values.real >= 0.0, 1.0, -1.0)
    imag = np.where(values.imag >= 0.0, 1.0, -1.0)
    return (real + 1j * imag) / np.sqrt(2.0)
