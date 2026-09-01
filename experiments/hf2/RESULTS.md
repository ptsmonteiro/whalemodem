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

## Post-simulation qualification update (2026-09-01)

Later work against `MODE_QUALIFICATION.md`'s remaining gates found a real
**gate failure**: a 300-trial-per-payload occupied-bandwidth campaign
(`experiments/hf2/measure_bandwidth.py`) measured a distribution-free 95.1%
upper confidence bound of 4,212.11-4,218.98 Hz on the 99%-power occupied
bandwidth at representative and maximum payload -- nearly double the
2,300 Hz ceiling, and by a wide margin (even the 300-trial sample minimum,
2,866-3,044 Hz, exceeds the ceiling). Two compounding causes, not one: (1)
HF2's own top carrier sits at 2,343.75 Hz, already 43.75 Hz above the
2,300 Hz ceiling by design, before any leakage is counted -- unlike HF3,
which deliberately kept its top carrier ~240 Hz below the ceiling to absorb
exactly this kind of skirt; and (2) HF2's coarser 93.75 Hz carrier spacing
(512-sample OFDM core, vs. HF3's 1,024) spreads the shared, unwindowed
`whale/dsp/ofdm.py` kernel's inherent symbol-to-symbol spectral leakage
roughly 2x wider in Hz than HF3's finer spacing produces, with zero
headroom to absorb it. The root cause has not been fixed -- both the
carrier plan and the lack of inter-symbol windowing need addressing. See
`logs/mode_qualification/hf-ssb/hf2/2026-09-01-bandwidth/INDEX.md`.

A subsequent IC-7300-to-IC-705 hardware campaign -- 3-frame smoke/confirm
followed by a 40-frame retained-direction run -- decoded 43/43 full-capacity
frames byte-for-byte, and the 40-frame run numerically clears the
retained-direction FER/acquisition Wilson gate (see
`logs/mode_qualification/hf-ssb/hf2/2026-09-01-hardware/INDEX.md`). A
bounded CI regression anchor was added at both required envelope points
(`tests/test_channel_regressions.py`). **None of this changes the bandwidth
verdict above.** The hardware run does not establish channel-plan
compliance -- neither radio's actual TX/RX filter passband was captured or
verified against the bandwidth failure, so a clean hardware pass over this
one pair says nothing about whether the transmitted signal was band-limited
by the radios or emitted largely as measured. The bandwidth failure remains
the qualification's primary open item and, on the evidence alone, would
block Optional/Default promotion regardless of the hardware and CI results.

## Product decision: promoted to Default (2026-09-01)

The evidence picture above is unchanged and the bandwidth gate is still
open. Separately, the repo owner made an explicit product decision on
2026-09-01 to promote HF2 to Default status in
`whale/mode_qualification.py`'s `MANIFEST`
(`QualificationEntry("hf-ssb", 7, QualificationLevel.DEFAULT)`), overriding
this open gate. As `MODE_QUALIFICATION.md` states, a `default` entry is a
product-availability disposition, not proof that the evidence gates passed
-- this promotion does not mean the bandwidth failure was resolved,
re-measured, or found to be a false alarm. It was not: the 99%-power
occupied bandwidth is still measured at ~4,212-4,219 Hz against the
2,300 Hz ceiling, and the underlying causes (an over-ceiling top carrier
and unwindowed OFDM sidelobe leakage) remain unfixed.

Because HF2 is now Default, it is negotiated and transmitted automatically
by any default-configuration station, with no operator opt-in. The open
bandwidth failure is therefore not just an internal test number: it is a
live SSB channel-plan compliance concern, since a station running HF2 will
routinely occupy spectrum outside its expected passband and risks
adjacent-channel interference on shared HF spectrum. This gap list is
otherwise unchanged and still applies in full.

## What is not yet established

- **Occupied bandwidth fails the gate.** See the update above -- this
  remains the primary blocker on the evidence, not merely unmeasured
  evidence, and the 40-frame hardware pass below does not address it. As of
  2026-09-01, HF2 ships at Default status anyway by explicit product
  decision (see above); the gate itself is still open.
- **Hardware setup record is missing.** 43/43 frames decoded
  IC-7300-to-IC-705 across all runs, including a 40-frame retained-direction
  campaign that numerically clears the frame gate, but without the minimum
  setup record (RF frequency, filter, power, antenna path) for either
  radio -- and radio filter settings are exactly what would determine
  whether the bandwidth failure above actually reached the air unfiltered.
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
