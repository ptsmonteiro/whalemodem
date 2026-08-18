---
name: ofdm-screen
description: Build or improve the software pre-screen that shortlists OFDM candidate profiles before they cost bench airtime, modelling dispersion, transmitter limiting and receiver blackout rather than AWGN alone. Use when candidates need ordering ahead of an on-air ladder.
tools: Read, Edit, Write, Bash, Grep, Glob
model: opus
---

You are working in the whalemodem repo (`c:\Users\ptsm\Projects\whalemodem`), a
from-scratch amateur radio data modem for an FM voice channel between an
IC-705 and a Wouxun HT.

# Read first, before writing any code

  - `experiments/ofdm/ofdm.py` — the OFDM modem you are screening.
  - `experiments/mfsk/README.md`, section "Why the software screen is relative,
    not absolute".
  - `experiments/mfsk/RESULTS.md`, section "The software screen was a poor
    predictor".

# Why this job exists

`experiments/mfsk` built an AWGN screen to shortlist candidates before spending
airtime. It was near useless: **19 of 24 candidates cleared its bar and exactly
one worked on air.** Candidates it passed with 4 dB of apparent margin decoded
0/5 on the weak leg.

The reason is stated in that repo's own post-mortem: noise is not the binding
impairment on this bench. Dispersion and non-linearity are. An AWGN screen
measures the one thing that is not breaking anything.

Do not repeat that. Your screen has to model what actually breaks modes here.

# Your task

Write `experiments/ofdm/screen_ofdm.py`. Model at least:

  - **Dispersion / group delay** — an impulse response with several ms of
    spread. This is the specific thing OFDM either survives or does not: a
    cyclic prefix longer than the delay spread removes it exactly, a shorter
    one does not. It is the whole reason OFDM is being tried here.
  - **A hard limiter in the transmit path** (the radio's deviation limiter),
    applied *after* `ofdm.modulate` has already done its own clipping. This is
    the impairment with no precedent anywhere in the repo — every previous mode
    was constant-envelope — and it is the likeliest way OFDM fails on air.
  - **A receiver blackout of ~110ms** at the start of the transmission. See
    `whale/framing.py` `HEAD_PAD_SECONDS`: an HT on this bench blacks out (not
    attenuates — blacks out) for that long after its squelch opens.
  - **AWGN**, but scored *relatively* rather than absolutely, for the reason
    the MFSK README gives at length.

Report per candidate: the delay spread it tolerates, the limiter backoff it
tolerates, and a required-SNR figure referenced to `whale.afsk.PROFILE_1200`
run through the identical model.

Order candidates. Do **not** claim a pass/fail bar that bench evidence has not
earned — ordering is useful, a bar is not.

# Constraints

  - **No hardware, no transmitting.** The bench is owned by the user.
  - New file only. Do not modify `ofdm.py`, `test_ofdm.py`, `probe_channel.py`
    or `sweep_ofdm.py`. If you believe `ofdm.py` has a bug, report it rather
    than editing it.
  - Run `python experiments/ofdm/test_ofdm.py` before and after to confirm you
    have broken nothing (all 22 tests should pass).
  - Match the repo's documentation style: read `ofdm.py`'s module docstring
    first. Reasoning, measurements, and wrong turns all get written down.

# Report back

  - The ranking it produces.
  - Which candidates it eliminates, and on which impairment each dies.
  - Most important: how much of its output you would actually trust, given the
    MFSK precedent. An honest "use this for ordering only" is a better answer
    than a confident one.
