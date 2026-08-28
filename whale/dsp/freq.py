"""Carrier frequency offset estimation.

Two estimators, coarse then fine, both channel-blind:

`coarse_offset_hz` reads the *angle* of the cyclic-prefix correlation.
Over the core length that separates the prefix from its copy, a frequency
error accumulates a phase the correlation reports directly.  This is the
same quantity `whale.dsp.timing` already computes and throws away -- it
keeps only the magnitude, to find the boundary -- so the coarse estimate
is free once timing has run.  Its unambiguous range is one carrier
spacing either side of zero: a phase of +-pi over `core_samples`.

`fine_offset_hz` refines that from the header, once the symbol boundaries
are known, by measuring how much the per-carrier phase advances from one
known symbol to the next.  Averaging over carriers and over the header's
symbol span buys precision the single prefix correlation cannot.

"Fine" here means more precise, not less ambiguous: its phase step spans a
whole symbol, so its unambiguous range (+-sample_rate/2/symbol_samples) is
slightly *narrower* than the coarse estimator's, which spans only a core.
Correct with the coarse estimate before trusting the fine one on a signal
that may be off by more than half a carrier spacing.

Neither is currently in VF3's decode path: VF3 rides on a differential
payload and a per-carrier equalizer that absorb a static offset, so these
are reported as diagnostics.  A coherent HF waveform would correct with
them before analysis.
"""

from __future__ import annotations

import numpy as np

from .ofdm import Geometry


def coarse_offset_hz(geometry: Geometry, analytic: np.ndarray, start: int,
                     symbol_indices: np.ndarray,
                     shifts: np.ndarray | None = None) -> float:
    """Frequency offset from the cyclic-prefix correlation angle.

    Correlations from every measured symbol are summed *before* the angle
    is taken, so symbols with more energy weight the estimate more and no
    unwrapping is needed.  Returns 0.0 when nothing could be measured.
    """
    guard = geometry.guard_samples
    core = geometry.core_samples
    symbol = geometry.symbol_samples
    indices = np.asarray(symbol_indices, dtype=np.int64)
    if guard == 0 or not len(indices):
        return 0.0
    if shifts is None:
        shifts = np.zeros(len(indices), dtype=np.int64)
    shifts = np.asarray(shifts, dtype=np.int64)

    total = 0.0 + 0.0j
    for index, shift in zip(indices, shifts):
        at = start + int(index) * symbol + int(shift)
        if at < 0 or at + symbol > len(analytic):
            continue
        prefix = analytic[at:at + guard]
        trailing = analytic[at + core:at + symbol]
        total += np.vdot(prefix, trailing)
    if total == 0:
        return 0.0
    # The prefix and its copy are `core` samples apart, so the accumulated
    # phase is 2*pi*offset*core/sample_rate.
    return float(np.angle(total) * geometry.sample_rate
                 / (2.0 * np.pi * core))


def fine_offset_hz(geometry: Geometry, observed: np.ndarray,
                   reference: np.ndarray) -> float:
    """Frequency offset from the phase advance across known symbols.

    `observed` and `reference` are (symbols, carriers) grids of the
    received and transmitted header constellations.  The channel is
    unknown but static over the header, so it cancels in the
    symbol-to-symbol ratio and what is left is the offset's phase step per
    symbol -- weighted here by carrier strength, so notched carriers do not
    drag the estimate.
    """
    observed = np.asarray(observed, dtype=np.complex128)
    reference = np.asarray(reference, dtype=np.complex128)
    if observed.shape != reference.shape:
        raise ValueError("observed and reference grids must match")
    if observed.shape[0] < 2:
        raise ValueError("a phase step needs at least two symbols")
    # Strip the known modulation, then look at consecutive-symbol ratios.
    stripped = observed * np.conj(reference)
    steps = stripped[1:] * np.conj(stripped[:-1])
    total = np.sum(steps)
    if total == 0:
        return 0.0
    return float(np.angle(total) * geometry.sample_rate
                 / (2.0 * np.pi * geometry.symbol_samples))


def derotate(analytic: np.ndarray, offset_hz: float, sample_rate: int,
             start_sample: int = 0) -> np.ndarray:
    """Shift `analytic` down by `offset_hz`.

    `start_sample` is the index the first element corresponds to, so a
    slice of a longer capture stays phase-consistent with the rest of it.
    """
    n = np.arange(start_sample, start_sample + len(analytic))
    return analytic * np.exp(-2j * np.pi * offset_hz * n / sample_rate)
