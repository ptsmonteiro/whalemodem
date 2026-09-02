# HF4 dense-carrier redesign campaign (2026-09-01)

Purpose: attempt to fix the 2026-09-01-fec campaign's finding (14/300, 4.67%
frames decoded at +13 dB benign/static against the 90%+ gate) by shrinking
the cyclic prefix's relative overhead through denser carrier spacing, and
using the freed throughput budget for a stronger inner FEC code. See
`experiments/hf4/DESIGN.md`'s "Dense-carrier redesign" section and
`experiments/hf4/RESULTS.md` for the full narrative and rate/length search.
This directory does not modify or remove any prior campaign directory.

## Final chosen configuration

- Carrier plan: 149 carriers, 15.625 Hz spacing (half of the old 31.25 Hz),
  bins 22-170, 343.75-2,656.25 Hz (same Hz band as before).
- CORE_SAMPLES=768 (double the old 384), GUARD_SAMPLES=64 unchanged (5.33 ms
  at 12 kHz) -- CP-to-symbol ratio drops from 14.3% (64/448) to 7.7%
  (64/832).
- DATA_SYMBOLS=108, PILOT_PERIOD=36 (3 pilots).
- Inner FEC: rate 11/12 (0.917) punctured `whale.dsp.fec.K7`, up from the
  old rate 19/20 (0.95) -- more redundancy than the old carrier plan could
  ever afford (its own throughput ceiling capped any code at this geometry
  to no lower than ~0.895).
- Per-carrier reliability weighting floor tightened from `low=0.05` to
  `low=0.02` (see `experiments/hf4/hf4.py`'s `demodulate`) -- the stronger
  code was found, during this redesign, to be *more* sensitive to a
  "confidently wrong" carrier at 0.05 than the old rate-19/20 code was; a
  synthetic one-dead-carrier regression test (`test_hf4.py`) caught this.

## Net throughput

`frame 8.302666666666667 net 7099.405813393287` (from real encoder output,
MODE_QUALIFICATION.md section 4 formula) -- **7,099.41 bit/s, 1.4% above the
7,000 bit/s floor. This gate passes**, though with a thinner margin than the
prior 19/20/75-carrier design's 3.1% -- the price of affording a much
stronger code on a short frame.

## Occupied bandwidth (300-trial campaign, `occupied_bandwidth.json`)

```
"passes": true,
"worst_high_edge_upper_confidence_bound_hz": 2660.295142923542,
"worst_low_edge_lower_confidence_bound_hz": 340.92493609109926
```

Both edges land comfortably inside 300-2,700 Hz (39.7 Hz / 40.9 Hz of real,
measured margin) -- **this gate passes**, essentially unchanged from prior
campaigns since the carrier plan's Hz band and edge taper are the same
design elements, only denser.

## Frame Monte Carlo at the +13 dB boundary (confirmed, 300 trials,
`frame_monte_carlo_13db_ds108.json`)

**211/300 decoded (70.33%), 95% Wilson-UB FER = 0.351** against the <=0.10
gate -- **fails**, but a roughly 15x improvement over the 2026-09-01-fec
campaign's 14/300 (4.67%). Acquisition 300/300 (Wilson-LB 0.987, clears its
own >=0.90 gate). Zero `error` outcomes -- every non-decode is a clean CRC
rejection, not a harness fault.

## Scout sweep (60 trials/point, seed 4)

| SNR | Decoded | Rate |
| --- | ---: | ---: |
| 13 dB | 41/60 | 68.3% |
| 15 dB | 44/60 | 73.3% |
| 20 dB | 50/60 | 83.3% |

Monotonically increasing with SNR but plateauing well under 90% even at
20 dB -- see "What this campaign shows" below.

## What this campaign shows: real progress, not a full fix

The carrier-density lever worked exactly as hypothesized: halving the CP's
relative overhead let the code rate improve substantially (0.95 -> 0.917 at
this frame length, and even lower rates were reachable at other frame
lengths -- see the search below), and that alone took +13 dB frame decode
from 4.67% to 70.33%. This is a genuine, large improvement, not noise.

It does not fully close the gate. A systematic sweep across data-symbol
count (72/108/144/180/216/288/360/540, each paired with the strongest FEC
rate that frame length could afford above 7,000 bit/s) found:

- **Frame duration, not code rate, is the dominant lever below the code's
  own robustness floor.** At a fixed rate around 0.85-0.92, *shorter*
  frames decoded better at every SNR tested, monotonically: DATA_SYMBOLS=540
  (rate 13/15, 39.1 s frame) scored worse at +13 dB (38%) than DATA_SYMBOLS=
  108 (rate 11/12, 8.3 s frame, 68.3%), even though the longer frame's code
  was meaningfully stronger. This suggests the required benign/static
  channel's per-frame carrier-fade variance (see DESIGN.md's "Inner FEC and
  interleaving") scales with frame duration in a way that a fixed-rate code
  budget cannot outrun by simply getting longer.
- **But this trend runs into a floor.** DATA_SYMBOLS=72 (rate 17/18, 5.7 s
  frame) scored the best in the scout sweep (68.3%/73.3%/90.0% at
  13/15/20 dB) but **failed `test_one_dead_carrier_still_decodes`
  outright, even with the dead carrier's soft bits weighted to zero
  (a hard erasure)** -- the interleaver depth and 17/18 code at this frame
  length is not strong enough to survive a single fully dead carrier at
  all, regardless of weighting. That configuration was disqualified (its
  campaign artifact is kept as `frame_monte_carlo_13db_ds72_disqualified.json`
  for the record, not as evidence for promotion) and DATA_SYMBOLS=108/rate
  11/12 -- the shortest frame that still passes the synthetic dead-carrier
  regression test -- was chosen instead.
- Pushing carrier density further (tried: doubling again to ~297 carriers/
  7.8125 Hz spacing) did not change this picture in the exploratory sizing
  math: at any given frame length the achievable code rate improves only
  modestly relative to what a shorter frame already buys directly, and the
  frame-duration effect above dominates.

**Diagnosis.** The remaining gap looks structural to the required
benign/static channel's per-carrier fade behavior interacting with frame
duration, not simply "not enough redundancy" or "too much CP overhead" --
both of those were real and are now measurably fixed (CP ratio halved, code
rate strengthened, decode rate improved 15x), but a residual, apparently
duration-scaling carrier-fade sensitivity remains that this pass could not
fully characterize or eliminate within the FEC-rate/frame-length/carrier-
density search performed here. A further attempt would need to look at the
channel model's per-carrier fade *process* directly (how its variance scales
with frame duration) rather than continuing to search this waveform's own
rate/length/density space, which this campaign explored thoroughly without
finding a combination that closes the gap.

## Test suite

`experiments/hf4/test_hf4.py`: **25/25 pass** against the final chosen
configuration (149 carriers, DATA_SYMBOLS=108, rate 11/12, weighting floor
0.02), including a strengthened `test_one_dead_carrier_still_decodes`
(weight lowered from 0.05 to 0.02 to match the tightened production
weighting floor).

## Honest summary against the three simultaneous gates

| Gate | Result |
| --- | --- |
| Net throughput > 7,000 bit/s | **Pass** (7,099.4 bit/s, 1.4% margin) |
| Occupied bandwidth inside 300-2,700 Hz | **Pass** (real margin both edges) |
| Frame Monte Carlo >=90% decoded at +13 dB | **Fail** (70.33%, 95% Wilson-UB FER 0.351) |

HF4 does **not** qualify at Level 4 as of this campaign. The carrier-density
redesign is a real, substantial improvement (4.67% -> 70.33% decoded at the
+13 dB boundary) and should be kept as the new baseline for any further
attempt, but it is not a complete fix.
