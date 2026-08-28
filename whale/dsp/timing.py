"""Symbol timing and sample-clock estimation from the cyclic prefix.

The prefix is a copy of the tail of its own core, so the correlation
between the two is maximized when the symbol boundary is found.  Sweeping
that correlation over a window of candidate shifts, once per symbol, gives
a per-symbol timing error; a straight-line fit across the frame separates a
constant offset (`intercept`) from a drifting sample clock (`slope`).

The vectorized search is required to be bit-for-bit what the original
per-symbol Python loop produced -- `tests/test_vf3_kernels.py` holds it
there -- which is why the winning candidate's score is deliberately
re-derived with the same reduction the scalar version used rather than
read out of the batched computation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .ofdm import Geometry

SEARCH_SAMPLES = 32


@dataclass(frozen=True)
class TimingFit:
    """Where each symbol's FFT window should start, as a line in symbol
    index: `intercept + slope * i` samples."""

    intercept: float
    slope: float
    confidence: float

    def shift_at(self, symbol_index: int) -> int:
        return int(round(self.intercept + self.slope * symbol_index))

    def drift_samples(self, symbol_count: int) -> float:
        return self.slope * (symbol_count - 1)

    def clock_offset_ppm(self, geometry: Geometry) -> float:
        return float(self.slope / geometry.symbol_samples * 1e6)


def estimate(geometry: Geometry, analytic: np.ndarray, start: int,
             symbol_indices: np.ndarray,
             search: int = SEARCH_SAMPLES) -> TimingFit:
    """Fit symbol timing across the frame.

    `symbol_indices` are the symbols to measure, relative to `start`.
    Modes normally skip the sync symbols, whose prefixes are identical to
    their neighbours' and so correlate everywhere.
    """
    guard = geometry.guard_samples
    core = geometry.core_samples
    symbol = geometry.symbol_samples
    indices = np.asarray(symbol_indices, dtype=np.int32)
    shifts = np.zeros(len(indices))
    scores = np.zeros(len(indices))
    offsets = np.arange(-search, search + 1)

    # Every guard-length window of the signal, once; the prefix window of a
    # candidate starts at `at` and its tail window at `at + core`.
    if len(analytic) >= symbol and guard > 0:
        windows = np.lib.stride_tricks.sliding_window_view(analytic, guard)
        starts = start + indices[:, None] * symbol + offsets[None, :]
        usable = (starts >= 0) & (starts + symbol <= len(analytic))
        flat = starts[usable]
        prefix = windows[flat]
        tail = windows[flat + core]
        conjugate = prefix.conj()
        correlation = np.abs(np.sum(conjugate * tail, axis=1))
        energy = np.sum((conjugate * prefix).real, axis=1)
        tail_energy = np.sum((tail.conj() * tail).real, axis=1)
        candidates = np.full(starts.shape, -1.0)
        candidates[usable] = correlation / np.maximum(
            np.sqrt(energy * tail_energy), 1e-30)
        # argmax keeps the first shift on an exact tie, as the scalar `>` did.
        best = np.argmax(candidates, axis=1)
        found = usable[np.arange(len(indices)), best]
        shifts = np.where(found, offsets[best], 0).astype(float)
        # The winner's score is re-derived with the same np.vdot reduction
        # the scalar loop used, so the reported median is unchanged.
        for row, (at, hit) in enumerate(
                zip(starts[np.arange(len(indices)), best], found)):
            if not hit:
                continue
            window = analytic[at:at + guard]
            trailing = analytic[at + core:at + symbol]
            denominator = np.sqrt(np.vdot(window, window).real
                                  * np.vdot(trailing, trailing).real)
            scores[row] = max(
                float(abs(np.vdot(window, trailing)) / max(denominator, 1e-30)),
                0.0)

    slope, intercept = np.polyfit(indices, shifts, 1)
    return TimingFit(float(intercept), float(slope), float(np.median(scores)))
