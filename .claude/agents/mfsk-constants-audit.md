---
name: mfsk-constants-audit
description: Audit experiments/mfsk for constants that have drifted out of sync with whale/ and correct the published throughput figures that depend on them, without rewriting what was actually measured on air. Use when a standalone experiment copies constants from the main package and those constants have since moved.
tools: Read, Edit, Write, Bash, Grep, Glob
model: sonnet
---

You are working in the whalemodem repo (`c:\Users\ptsm\Projects\whalemodem`), a
from-scratch amateur radio data modem.

Small, self-contained, and it affects numbers a new experiment is about to be
compared against.

# The drift

`experiments/mfsk/mfsk.py` copies `HEAD_PAD_SECONDS = 0.08` and
`TAIL_PAD_SECONDS = 0.03` out of `whale/framing.py` so that the module stands
alone. `whale/framing.py` now says `HEAD_PAD_SECONDS = 0.15` — it was raised
for an HT that blacks out ~110ms after its squelch opens.

`experiments/mfsk/test_mfsk.py` asserts that `MAX_KEYING_SECONDS` and
`KEYING_OVERHEAD_SECONDS` still agree with `whale/`, but does **not** assert
that the pads do. So this particular drift was unguarded.

Separately: `experiments/mfsk/README.md` and `RESULTS.md` quote `whale.afsk`'s
`PROFILE_1200` as delivering **947** payload bits/s. Computing it live from the
current code — `afsk.PROFILE_1200.chunk_size * 8 / 3.0` — gives **853**.

# Your task

1. Work out exactly which published figures in `experiments/mfsk/README.md` and
   `experiments/mfsk/RESULTS.md` are now stale, and what they should be. In
   particular: does the winning `4fsk_650bd_x0.833` profile still carry 379
   bytes under a 0.15s head pad, and what are its throughput and its
   multiple-of-the-shipped-profile figure today?
2. Correct those documents.
3. Add the missing pad assertion to `test_mfsk.py` so this cannot drift again
   silently.

# The part that needs care

Do **not** re-run anything on the radios, and do **not** change `mfsk.py`'s
DSP. The bench results in `RESULTS.md` were measured with the pads as they
were at the time. The honest fix states three separate things:

  - what was measured on air, and under which constants;
  - what the same profile would carry under today's constants;
  - which of the published conclusions still hold and which do not.

Quietly rewriting the measured numbers to today's constants would be falsifying
a bench result. Quietly leaving them would be publishing a stale comparison.
Distinguish the two carefully — that distinction is the actual deliverable.

Also check whether any *other* constant in `mfsk.py`'s "copied rather than
imported" block has drifted, not just the pads.

# Constraints

  - **No hardware, no transmitting.**
  - Run `python experiments/mfsk/test_mfsk.py` to confirm everything still
    passes.
  - Match the repo's documentation style: it explains reasoning and history at
    length, and it is scrupulous about labelling what is measured versus what
    is reasoned or chosen.

# Report back

  - A table of every figure that changed, old vs new.
  - Whether the MFSK bench winner is still above or below parity with the
    shipped 1200-baud profile today.
  - Any other stale coupling you found between `experiments/` and `whale/`.
