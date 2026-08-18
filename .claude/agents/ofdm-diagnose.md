---
name: ofdm-diagnose
description: Build symbol-level post-mortem tooling that says WHY an OFDM frame failed — bad subcarrier, clock drift, transmitter clipping, or plain noise — from a capture and the payload that was sent. Use when a near-miss decode needs explaining rather than just counting.
tools: Read, Edit, Write, Bash, Grep, Glob
model: opus
---

You are working in the whalemodem repo (`c:\Users\ptsm\Projects\whalemodem`), a
from-scratch amateur radio data modem for an FM voice channel.

# Read first

  - `experiments/ofdm/ofdm.py` — the modem, especially `_equalise`, `_demap`
    and the module docstring.
  - `experiments/mfsk/diagnose_mfsk.py` — the equivalent tool for the previous
    experiment.
  - `experiments/mfsk/RESULTS.md`, section "What is actually binding".

That last section is the standard to match. It took a ladder result — one
candidate failed, the next passed — and turned it into an explanation: 8 wrong
symbols in 1596, *all* of them one specific tone read as its neighbour, spread
evenly through the frame rather than ramping. That distinguished leakage from
drift and from noise, and it is why the experiment produced understanding
rather than just a number.

# Your task

Write `experiments/ofdm/diagnose_ofdm.py`. Given a captured frame and the
payload that was sent, line up received against transmitted symbols and say
**which failure mode** a near miss is:

  - errors concentrated on particular **subcarriers** → a notch or band-edge
    rolloff. This is the no-FEC killer: with CRC-only framing, one bad carrier
    produces errors in every symbol and fails every frame regardless of how
    good the other thirty-four are.
  - errors ramping with **symbol index** → sample-clock drift. Phase rotation
    accumulates linearly across the frame; see the clock-offset section of
    `ofdm.py`'s docstring for the mechanism and the closed form.
  - errors correlated with high **instantaneous amplitude** → the radio's
    limiter clipping peaks, i.e. the drive is too hot. OFDM is the first
    non-constant-envelope mode in this repo, so this failure has no precedent
    here and nothing else will recognise it.
  - errors flat in both axes → plain noise.

Report per-subcarrier EVM, per-symbol-index EVM, the measured channel `|H|` and
phase, and a **stated verdict** — not a table the reader has to interpret.

It must work on synthetic frames now (no bench data exists yet) and on real
captures later. Accept a `.npy` capture path or generate its own impaired
frame. Note `WHALE_CAPTURE_DIR` in `whale/link.py`: the link already saves the
audio behind near-miss decodes, and real captures live in
`scratch_captures_600ack/` as `.npy`.

# Constraints

  - **No hardware, no transmitting.** The bench is owned by the user.
  - New file only. Do not modify `ofdm.py`, `test_ofdm.py`, `probe_channel.py`
    or `sweep_ofdm.py`. If you find a bug in `ofdm.py`, report it rather than
    editing it.
  - Run `python experiments/ofdm/test_ofdm.py` to confirm you broke nothing.
  - Match the repo's documentation style: reasoning written down, including
    wrong turns. `scripts/bench.py`'s docstring explains why its near-miss
    diagnostic exists and how its first version was wrong — read it, because
    that is exactly the class of tool you are writing and exactly the mistake
    available to you.

# Report back

  - The tool's verdict on each synthetic failure mode you can inject, with the
    numbers it produced.
  - Any case where it **cannot** distinguish two causes. That ambiguity is more
    useful than a confident wrong answer, and it is the thing most likely to
    mislead someone reading a real result later.
