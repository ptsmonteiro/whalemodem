"""Common adaptive lead for every HF waveform.

The wire word is a six-symbol HC0-grid MFSK block repeated at least twice.
Order identifies the following waveform; duration is extended in whole blocks
for adaptive leading-loss protection.  A hint may order decoders, but only the
following frame's checked payload authenticates it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..dsp import mfsk
from . import hc0

BLOCK_SYMBOLS = 6
BLOCK_SAMPLES = BLOCK_SYMBOLS * hc0.SYMBOL_SAMPLES
RX_BLOCK_SAMPLES = BLOCK_SYMBOLS * hc0.RX_SYMBOL_SAMPLES
MIN_BLOCKS = 2
MIN_SAMPLES = MIN_BLOCKS * BLOCK_SAMPLES
MIN_SECONDS = MIN_SAMPLES / hc0.SAMPLE_RATE
FADE_SAMPLES = 240
MATCH_THRESHOLD = hc0.ACQUISITION_THRESHOLD
MAX_SECONDS = 1.0
MIN_ENERGY_FRACTION = 0.60
MAX_CANDIDATE_BOUNDARIES = 32

# Balanced, band-spanning permutations selected by the signature128 screen.
#
# The third row (HF2_LABEL) was not run through that screen -- HF2 is an
# experiment-local waveform (experiments/hf2/hf2.py) that has not yet been
# promoted into whale/modes/ -- but follows the same shape: six tones out of
# the 16-tone HC0 bank, spread across the band and distinct from the other
# two rows so `candidates()` does not confuse the labels.
BLOCKS = np.asarray(((9, 6, 12, 15, 0, 3),
                     (12, 3, 15, 6, 9, 0),
                     (2, 11, 5, 14, 8, 0)), dtype=np.int64)
HC0_LABEL = 0
HC1_LABEL = 1
HF2_LABEL = 2


@dataclass(frozen=True)
class LeadCandidate:
    """One advisory body boundary; a checked frame must confirm it."""

    score: float
    label: int
    body_start: int


def candidates(audio: np.ndarray, limit: int = MAX_CANDIDATE_BOUNDARIES
               ) -> tuple[LeadCandidate, ...]:
    """Rank plausible body boundaries, trying both labels at each one.

    Closely spaced correlation peaks are one timing hypothesis, not separate
    opportunities to spend decoder work.  Both labels are returned because a
    damaged label must not prevent the other checked waveform from winning.
    ``limit`` counts boundaries, so the returned tuple has at most twice that
    many entries.
    """
    if limit < 0:
        raise ValueError("candidate limit must not be negative")
    if limit == 0:
        return ()
    scored = []
    score_sets = []
    step = None
    for label, block in enumerate(BLOCKS):
        scores, label_step = mfsk.correlate(
            hc0.RX_BANK, audio, np.tile(block, 2))
        step = label_step
        score_sets.append(scores)
        scored.extend((float(score), int(at))
                      for at, score in enumerate(scores)
                      if score >= MATCH_THRESHOLD)
    if not scored or step is None:
        return ()

    boundaries = []
    for score, at in sorted(scored, reverse=True):
        start = at * step
        if any(abs(start - old) <= step for _, old in boundaries):
            continue
        boundaries.append((score, start))
        if len(boundaries) == limit:
            break

    found = []
    for _, start in boundaries:
        at = int(round(start / step))
        labels = sorted(range(len(BLOCKS)),
                        key=lambda label: score_sets[label][at], reverse=True)
        for label in labels:
            found.append(LeadCandidate(
                float(score_sets[label][at]), label,
                start + 2 * RX_BLOCK_SAMPLES))
    return tuple(found)


def lead_samples(seconds: float | None = None) -> int:
    if seconds is None:
        wanted = MIN_SAMPLES
    elif seconds < 0:
        raise ValueError("head duration must not be negative")
    else:
        wanted = max(MIN_SAMPLES, int(round(seconds * hc0.SAMPLE_RATE)))
    return -(-wanted // BLOCK_SAMPLES) * BLOCK_SAMPLES


def modulate(label: int, seconds: float | None = None) -> np.ndarray:
    if not 0 <= label < len(BLOCKS):
        raise ValueError(f"unknown HF lead label {label}")
    blocks = lead_samples(seconds) // BLOCK_SAMPLES
    tones = np.tile(BLOCKS[label], blocks)
    audio = mfsk.modulate(hc0.BANK, tones, hc0.TX_AMPLITUDE)
    audio[:FADE_SAMPLES] *= np.linspace(0.0, 1.0, FADE_SAMPLES)
    return audio.astype(np.float32)


def detect_label(audio: np.ndarray) -> tuple[int | None, float]:
    """Return the strongest two-block label hint anywhere in a capture."""
    ranked = candidates(audio, limit=1)
    if not ranked:
        return None, 0.0
    return ranked[0].label, ranked[0].score


def measure(audio: np.ndarray, body_start: int, label: int,
            expected_seconds: float | None = None) -> tuple[int, float]:
    """Count contiguous complete lead blocks backwards from a checked body."""
    pattern = np.tile(BLOCKS[label], 2)
    blocks = 0
    scores = []
    end = body_start
    del expected_seconds  # acquisition, not local state, determines duration
    maximum = lead_samples(MAX_SECONDS) // BLOCK_SAMPLES
    if body_start >= 2 * RX_BLOCK_SAMPLES:
        aligned = mfsk.refine(hc0.RX_BANK, audio, pattern,
                              body_start - 2 * RX_BLOCK_SAMPLES,
                              radius=hc0.RX_SYMBOL_SAMPLES // 2, step=2)
        end = aligned + 2 * RX_BLOCK_SAMPLES
    while end >= 2 * RX_BLOCK_SAMPLES and blocks < maximum:
        start = end - 2 * RX_BLOCK_SAMPLES
        score = mfsk.pattern_score(hc0.RX_BANK, audio, pattern, start)
        energy = mfsk.matched_energy(hc0.RX_BANK, audio, pattern, start)
        if score < MATCH_THRESHOLD or (scores and energy < first_energy * MIN_ENERGY_FRACTION):
            break
        if not scores:
            first_energy = energy
        scores.append(score)
        blocks = 2 if blocks == 0 else blocks + 1
        end -= RX_BLOCK_SAMPLES
    return blocks, (min(scores) if scores else 0.0)


def seconds_received(blocks: int) -> float:
    return blocks * BLOCK_SAMPLES / hc0.SAMPLE_RATE
