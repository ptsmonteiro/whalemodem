"""Measuring how much of a transmitted lead-in survived.

The link asks each mode for a leading guard long enough to cover the
receiving station's squelch blackout, and adapts that guard from what the
far end reports actually arriving.  This is the measurement behind that
feedback.

The head is one known block repeated up to the header, so the receiver
knows its shape but neither its length nor its phase -- the transmitter's
repeat ends wherever the requested duration fell.  So the block
immediately before the header is circularly correlated against the
reference to recover that phase, and blocks are then counted backwards
while they keep matching at the same phase.  The count stops at the first
block that does not, which is where the blackout (or the buffer) begins.
"""

from __future__ import annotations

import numpy as np

MATCH_THRESHOLD = 0.5
MIN_ENERGY_FRACTION = 0.75


def measure(samples: np.ndarray, start: int, reference: np.ndarray, *,
            match_threshold: float = MATCH_THRESHOLD,
            min_energy_fraction: float = MIN_ENERGY_FRACTION
            ) -> tuple[int, float]:
    """Count intact reference blocks immediately before `start`.

    Returns (blocks observed, the correlation of the block nearest the
    header).
    """
    reference = np.asarray(reference, dtype=np.float64)
    block_samples = len(reference)
    spectrum = np.conj(np.fft.rfft(reference))
    norm = float(np.linalg.norm(reference))
    first = 0.0
    count = 0
    at = start - block_samples
    phase = None
    reference_energy = None
    while at >= 0:
        block = np.asarray(samples[at:at + block_samples], dtype=np.float64)
        energy = float(np.linalg.norm(block))
        if energy <= 0.0:
            break
        # The correlation below is normalized by this block's own energy,
        # so it measures shape and is indifferent to the receiver's AGC --
        # but that also means a block which is mostly silence and only
        # partly head scores as well as a whole one.  Gate on energy
        # relative to the block nearest the header, which is the one
        # certain to be complete, so a partial block at the blackout edge
        # is not counted as received.
        if reference_energy is None:
            reference_energy = energy
        elif energy < min_energy_fraction * reference_energy:
            break
        circular = np.fft.irfft(np.fft.rfft(block) * spectrum, block_samples)
        circular /= energy * norm
        best = int(np.argmax(circular))
        score = float(circular[best])
        if count == 0:
            first, phase = score, best
        elif best != phase:
            break
        if score < match_threshold:
            break
        count += 1
        at -= block_samples
    return count, first
