"""Preamble correlation and frame acquisition.

A frame that opens with repeated identical sync symbols is periodic at the
symbol length, so the receiver can find it without knowing the channel:
correlate the signal against itself one symbol apart and the repeat shows
up as a normalized peak.

The peak is a plateau, not a spike -- the correlation stays high for as
long as the repeat lasts -- so the peak sample alone is not the answer.
Contiguous runs above threshold are grouped, each group's best sample is
taken, and the groups are ranked by an external scorer (in practice the
header channel fit in `whale.dsp.equalize`).  That ranking is what keeps
acquisition off periodic energy that is not the header.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from .ofdm import Geometry

# Defaults carried over from VF3.  The proposal threshold has slack -- it
# can be moved a long way without changing the answer on any recorded
# capture -- so it is a knob for how many groups get ranked, not a
# decision boundary.  `confidence` is what callers actually gate on.
PROPOSAL_THRESHOLD = 0.68
RMS_FLOOR_FRACTION = 0.03


def rolling_sum(values: np.ndarray, width: int) -> np.ndarray:
    prefix = np.concatenate((np.zeros(1, dtype=values.dtype),
                             np.cumsum(values)))
    return prefix[width:] - prefix[:-width]


def correlate_repeat(analytic: np.ndarray, lag: int, span: int
                     ) -> tuple[np.ndarray, np.ndarray]:
    """Normalized self-correlation at `lag`, over a window of `span`.

    Returns (scores in [0, 1], per-window RMS).  The RMS is what lets a
    caller ignore correlation computed on near-silence, where the
    normalization makes noise look like a perfect repeat.
    """
    left, right = analytic[:-lag], analytic[lag:]
    cross = rolling_sum(right * np.conj(left), span)
    e_left = rolling_sum(np.abs(left) ** 2, span).real
    e_right = rolling_sum(np.abs(right) ** 2, span).real
    scores = np.abs(cross) / np.sqrt(np.maximum(e_left * e_right, 1e-30))
    rms = np.sqrt(0.5 * (e_left + e_right) / span)
    return scores, rms


def _contiguous_groups(samples: np.ndarray) -> list[tuple[int, int]]:
    groups = []
    group_start = previous = int(samples[0])
    for sample in samples[1:]:
        sample = int(sample)
        if sample != previous + 1:
            groups.append((group_start, previous))
            group_start = sample
        previous = sample
    groups.append((group_start, previous))
    return groups


def acquire(geometry: Geometry, analytic: np.ndarray, *, sync_symbols: int,
            rank: Callable[[int], float] | None = None,
            proposal_threshold: float = PROPOSAL_THRESHOLD,
            rms_floor_fraction: float = RMS_FLOOR_FRACTION
            ) -> tuple[int | None, float]:
    """Locate the first sample of the header.

    `rank` scores a candidate start; the highest-ranked group wins, ties
    broken by correlation.  With no `rank` the strongest group is taken,
    which is enough when nothing else in the capture is symbol-periodic.
    """
    lag = geometry.symbol_samples
    span = (sync_symbols - 1) * geometry.symbol_samples
    if len(analytic) < span + lag:
        return None, 0.0
    scores, rms = correlate_repeat(analytic, lag, span)
    if not np.any(rms > 0.0):
        return None, 0.0
    mask = ((scores >= proposal_threshold)
            & (rms >= np.max(rms) * rms_floor_fraction))
    proposal_samples = np.flatnonzero(mask)
    if not len(proposal_samples):
        index = int(np.argmax(scores))
        return index, float(np.clip(scores[index], 0.0, 1.0))
    best = None
    for low, high in _contiguous_groups(proposal_samples):
        candidate = low + int(np.argmax(scores[low:high + 1]))
        score = float(scores[candidate])
        key = (rank(candidate) if rank is not None else score, score)
        if best is None or key > best[0]:
            best = (key, candidate)
    index = best[1]
    return index, float(np.clip(scores[index], 0.0, 1.0))
