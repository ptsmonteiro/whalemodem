# HF2 results

Stage 5 of `PLAN.md`. This records the >=300-trial confirmed-boundary gate at
both of Level 2's required envelope points, the supporting >=100-trial screen
around them, and the resulting pass/fail call against the falsifiable target
in `PLAN.md`. All numbers below are from `whale.qualification`'s Monte Carlo
helpers over `whale.channel`'s Watterson model, run through
`experiments/hf2/benchmark_hf2.py` — no radios were available for this work
(see PLAN.md's status section).

## Confirmed-boundary gate (>=300 trials, seed 20260910)

Per `MODE_QUALIFICATION.md` section 3, the two required boundary points need
>=300 independent trials, 95% Wilson-UB FER <= 0.10, 95% Wilson-LB
acquisition >= 0.90, and zero `error` outcomes.

| Point | Trials | Acquire (Wilson-LB) | Decoded (FER Wilson-UB) | Errors | Useful bit/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| `mid_latitude_quiet` +5 dB (required boundary) | 300 | 300/300 (0.987) | 300/300 (0.0126) | 0 | 584.5 |
| `mid_latitude_moderate` +10 dB (required boundary) | 300 | 300/300 (0.987) | 296/300 (0.0338) | 0 | 576.7 |

Both required boundary points clear the gate with wide margin: Wilson-UB FER
(0.0126, 0.0338) sits far under the 0.10 ceiling, Wilson-LB acquisition
(0.987 at both) sits far above the 0.90 floor, and no trial produced an
`error` outcome at either point. The thin-margin concern the 100-trial
confirmation (stage 4b of DESIGN.md) flagged did not materialize at 300
trials — if anything the sample noise in the 100-trial run's FER estimate
(0.037–0.070) was pessimistic relative to the 300-trial value at the
moderate boundary (0.033) and optimistic relative to it at the quiet
boundary (0.013 vs 0.037), both well inside tolerance and both comfortably
under the ceiling either way.

Artifacts: `results/mid_latitude_quiet_confirm300_20260910.json`,
`results/mid_latitude_moderate_confirm300_20260910.json`.

## Supporting screen (>=100 trials each, reused from the stage-4b confirmation)

The stage-4b 100-trial confirmation (`DESIGN.md`, seed 20260903) already
covered one point on each side of both boundaries at >=100 trials, so it is
reused here rather than re-run, per this task's instructions. It brackets
where the boundary actually sits: every point tested, inside and just
outside each preset's required envelope edge, passes.

| Point | Trials | Acquire (Wilson-LB) | Decoded (FER Wilson-UB) | Useful bit/s |
| --- | ---: | ---: | ---: | ---: |
| `mid_latitude_quiet` +3 dB (just outside quiet's +5 dB requirement) | 100 | 100/100 (0.963) | 100/100 (0.037) | 584.5 |
| `mid_latitude_quiet` +5 dB (required boundary) | 100 | 100/100 (0.963) | 100/100 (0.037) | 584.5 |
| `mid_latitude_quiet` +8 dB | 100 | 100/100 (0.963) | 100/100 (0.037) | 584.5 |
| `mid_latitude_moderate` +8 dB (just outside moderate's +10 dB requirement) | 100 | 99/100 (0.946) | 98/100 (0.070) | 572.8 |
| `mid_latitude_moderate` +10 dB (required boundary) | 100 | 100/100 (0.963) | 99/100 (0.054) | 578.7 |
| `mid_latitude_moderate` +13 dB | 100 | 100/100 (0.963) | 99/100 (0.054) | 578.7 |

Artifacts: `results/mid_latitude_quiet_confirm100_20260903.json`,
`results/mid_latitude_moderate_confirm100_20260903.json`. (These points are
not required to pass — the "outside" points, +3 dB quiet and +8 dB moderate,
sit below each preset's required envelope edge — but both do pass at this
sampling depth, which is useful context for where the true boundary sits,
not evidence the gate needed.)

The stage-4b 30-trial screen (`DESIGN.md`, seed 20260902) additionally covers
a wider point grid — `mid_latitude_quiet` from −2 dB to +15 dB and
`mid_latitude_moderate` from +3 dB to +20 dB — at 30 trials/point, satisfying
`MODE_QUALIFICATION.md`'s "≥2 points well inside, ≥2 near the boundary, ≥2
outside" grid-bracketing requirement, though at n=30 a perfect 30/30 run's
own Wilson-UB floor (0.114) exceeds the 0.10 gate, so those points are
reported there as directional/screening evidence only, not gate evidence.
See `DESIGN.md`'s stage 4b note for that full table.

## Verdict against PLAN.md's falsifiable target

**Envelope: passes.** At >=300 trials, both required boundary points —
`mid_latitude_quiet` +5 dB and `mid_latitude_moderate` +10 dB — clear
`MODE_QUALIFICATION.md`'s confirmed-boundary statistical gate (95% Wilson-UB
FER <= 0.10, 95% Wilson-LB acquisition >= 0.90, zero `error` outcomes).

**Floor: passes, but with thin margin.** Useful application throughput
(decoded payload bytes / total keyed time, via `mode.airtime()`, the
MODE_QUALIFICATION.md section-6 convention) measured 576.7–584.5 bit/s
across every qualifying point in both tables above — above the 500 bit/s
Level 2 floor at every point, but by only 15–17%. This is a materially
thinner margin than the design's first working iteration (stage 4a,
~900–1060 bit/s) had before the frequency-diversity fix for the
persistent-carrier-fade erasure floor (DESIGN.md's stage 4b note) traded
throughput for the redundancy needed to clear the gate. A future channel
model change, a stricter gate, or a design regression that costs even a
modest fraction of this margin could put the floor at risk; this is not a
comfortable pass.

**Level 2 target: cleared in simulation.** Combining the two results above,
HF2 as currently implemented in `hf2.py` meets Level 2's contract — >=500
bit/s useful throughput simultaneously with the FER/acquisition gate — at
both required envelope points, in Monte Carlo simulation over the
repository's Watterson channel model.

## What is not yet established

- **No hardware or radio evidence.** No radios were available for this
  experiment (see PLAN.md's status section); every result above is
  simulated over `whale.channel`'s Watterson model. `MODE_QUALIFICATION.md`
  treats simulated Monte Carlo evidence as valid primary qualification
  evidence, but hardware/session evidence is a separate, later gate this
  experiment does not attempt to satisfy.
- **No full-session or ARQ evidence.** These results are single-frame
  acquire/decode trials, not a full link session (retries, ARQ, turnaround,
  PTT/ALC behavior over an extended exchange).
- **No disturbed-fading or low/high-latitude-preset coverage.** Per
  DESIGN.md and PLAN.md, this experiment targets only Level 2's contract:
  `mid_latitude_quiet` and `mid_latitude_moderate`. Disturbed Watterson
  fading and the low/high-latitude presets are out of scope for Level 2 and
  were not tested.
- **Thin throughput margin.** As noted above, measured useful throughput
  (576.7–584.5 bit/s) clears the 500 bit/s floor by only 15–17%, not by a
  wide margin. A reader relying on this result for anything beyond "clears
  Level 2 as specified" should treat that margin as the main caveat.
- **No claim of parity or comparison** with HC0, HC1, VF6, HR0, or VARA
  (per DESIGN.md's explicit scope note).
