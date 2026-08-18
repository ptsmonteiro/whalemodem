---
name: ofdm-clock-headroom
description: Implement and measure split (front and trailing) OFDM training symbols to widen sample-clock-offset tolerance, which currently binds hardest at 8PSK. Spawn with isolation "worktree" — it edits experiments/ofdm/ofdm.py, which the main session also works in.
tools: Read, Edit, Write, Bash, Grep, Glob
model: opus
---

You are working in the whalemodem repo (`c:\Users\ptsm\Projects\whalemodem`), a
from-scratch amateur radio data modem.

**You should have been spawned with `isolation: worktree`.** You edit
`experiments/ofdm/ofdm.py`, which the main session is also working in. If you
are not in a worktree, say so in your report and produce a patch rather than
editing in place.

# Read first

  - `experiments/ofdm/ofdm.py`, especially the module docstring's section on
    sample-clock offset.
  - `test_sample_clock_offset_tolerance_is_set_by_the_top_carrier` in
    `experiments/ofdm/test_ofdm.py`.

# The measured finding you are acting on

This OFDM mode's sample-clock tolerance is ~40 ppm at BPSK, 20 at QPSK and 8 at
8PSK, and it follows

    max_ppm ~ 1 / (2^(bits + 1) * top_frequency * frame_seconds)

because drift rotates subcarrier `k` by `2*pi*k*tau/n_fft`, and the frame dies
when the top carrier's rotation reaches the constellation's decision boundary.
Note what this is *not*: the cyclic prefix absorbs the drift as timing without
difficulty. It is the phase that kills the frame.

The two sound cards on this bench measure 3.4 ppm apart
(`scripts/measure_clock_offset.py`). So QPSK has ~6x margin, but 8PSK has only
~2.4x — and 8PSK is where the throughput is. For comparison, the shipped FSK
profiles tolerate 235–745 ppm.

# Your task

Implement and measure the fix the docstring names but does not build: put one
training symbol at the **end** of the frame instead of both at the front, so
the linear phase drift is bounded by two anchors and can be interpolated per
symbol. It costs no airtime — same symbol count, different position.

The receiver does not know the frame length until it decodes the length field,
so this needs a two-pass decode: front-training only to read the length, then
re-equalise with interpolation across the frame.

Measure honestly:

  - ppm tolerance before and after, at BPSK / QPSK / 8PSK / 16QAM;
  - the cost in decode time and in code complexity.

If it does not buy at least a few times the tolerance, **say so plainly and
recommend against it.** `experiments/mfsk` kept a preamble training equaliser
that "did not earn its place" and had to justify it afterwards; do not repeat
that pattern.

# Constraints

  - **No hardware, no transmitting.**
  - Every existing test in `experiments/ofdm/test_ofdm.py` must still pass
    (22 of them). Update the clock-offset test's expectations only if the
    change genuinely moves them, and keep the closed-form model check.
  - This is an **on-air format change**: stations built either side of it do
    not interoperate. Flag that explicitly and prominently in your report — the
    repo has been bitten by unflagged format changes before (see
    `framing.LENGTH_FIELD_BITS` and `framing.SYNC_SECONDS`).
  - Match the repo's documentation style: reasoning and measurements written
    down, including anything you tried that did not work.

# Report back

  - The before/after ppm table across all four constellations.
  - Whether you recommend adopting it, with the reasoning.
  - The diff as a patch that can be applied to the main worktree.
