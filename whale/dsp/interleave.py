"""Bit interleaving, as a permutation the mode owns.

A fading HF channel takes out runs of adjacent bits -- a deep notch on one
carrier, or a whole symbol lost to a burst.  The convolutional code is
good at scattered errors and poor at bursts, so the coded bits are spread
before transmission and gathered back before decoding.

Every VF mode so far uses the *multiplicative* interleaver here.  The
*block* interleaver is the classic alternative and is the one to reach for
when the burst length is known in advance -- an HF waveform that knows how
many bits ride on one carrier can choose a column count that puts adjacent
coded bits on different carriers by construction.

Both are the same object: a permutation plus its inverse, applied to hard
bits or to soft reliabilities identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from math import gcd

import numpy as np


@dataclass(frozen=True)
class Interleaver:
    """A fixed bit permutation and its inverse.

    `spread` gathers -- `output[i] = input[permutation[i]]` -- and
    `gather` scatters it back, matching the order the VF modes have always
    applied theirs in.  Both preserve dtype, so the same object serves the
    hard-bit and soft-bit paths.
    """

    permutation: np.ndarray

    def __post_init__(self) -> None:
        permutation = np.asarray(self.permutation, dtype=np.int64)
        permutation.flags.writeable = False
        object.__setattr__(self, "permutation", permutation)

    @property
    def size(self) -> int:
        return len(self.permutation)

    @cached_property
    def inverse(self) -> np.ndarray:
        inverse = np.empty(self.size, dtype=np.int64)
        inverse[self.permutation] = np.arange(self.size, dtype=np.int64)
        return inverse

    def _check(self, values: np.ndarray) -> np.ndarray:
        values = np.asarray(values).reshape(-1)
        if len(values) != self.size:
            raise ValueError(
                f"interleaver is {self.size} bits wide, got {len(values)}")
        return values

    def spread(self, values: np.ndarray) -> np.ndarray:
        return self._check(values)[self.permutation]

    def gather(self, values: np.ndarray) -> np.ndarray:
        values = self._check(values)
        out = np.empty_like(values)
        out[self.permutation] = values
        return out

    def is_valid(self) -> bool:
        """True when the permutation really is one -- every index once."""
        return np.array_equal(np.sort(self.permutation),
                              np.arange(self.size, dtype=np.int64))


def multiplicative(size: int, stride: int) -> Interleaver:
    """`i -> (i * stride) mod size`.

    A permutation exactly when `stride` is coprime with `size`, which is
    checked here: a stride sharing a factor collapses the mapping and
    silently drops bits.  VF2 through VF5 all use stride 8101.
    """
    if size <= 0:
        raise ValueError("interleaver size must be positive")
    if gcd(stride, size) != 1:
        raise ValueError(
            f"stride {stride} is not coprime with size {size}, so it is not "
            "a permutation")
    return Interleaver((np.arange(size, dtype=np.int64) * stride) % size)


def block(rows: int, columns: int) -> Interleaver:
    """Write down the columns, read across the rows.

    Guarantees that bits within `rows` of each other in the coded stream
    end up at least `columns` apart on the air, which is what makes the
    burst length a design parameter rather than a hope.
    """
    if rows <= 0 or columns <= 0:
        raise ValueError("block interleaver needs positive dimensions")
    grid = np.arange(rows * columns, dtype=np.int64).reshape(rows, columns)
    return Interleaver(grid.T.reshape(-1))
