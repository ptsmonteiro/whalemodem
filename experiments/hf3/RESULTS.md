# HF3 results

Confirmed-boundary Monte Carlo evidence for HF3 against Speed Ladder Level 3
("fast data", SPEED_LADDERS.md): minimum useful application throughput
2,000 bit/s, required envelope benign/static at +8 dB waveform SNR and above,
quiet Watterson fading at +10 dB and above. All numbers below are from
`whale.qualification`'s Monte Carlo helpers over `whale.channel`, run through
`experiments/hf3/benchmark_hf3.py` -- no radios were available for this work.

## Confirmed-boundary gate (>=300 trials)

Per MODE_QUALIFICATION.md section 3, each required boundary point needs
>=300 independent trials, 95% Wilson-UB FER <= 0.10, 95% Wilson-LB
acquisition >= 0.90, and zero `error` outcomes.

| Point | Trials | Acquire (Wilson-LB) | Decoded (FER Wilson-UB) | Errors | Useful bit/s (frame-prorated) |
| --- | ---: | ---: | ---: | ---: | ---: |
| benign/static +8 dB (required boundary), seed 20260901 | 300 | 300/300 (0.987) | 300/300 (FER Wilson-UB 0.013) | 0 | 2,025.2 |
| `mid_latitude_quiet` +10 dB (required boundary), seed 20260901 | 300 | 300/300 (0.987) | 291/300 (FER Wilson-UB 0.056) | 0 | 1,964.5 |

**Both required boundary points clear the frame Monte Carlo gate**: 95%
Wilson-UB FER (0.013, 0.056) sits well under the 0.10 ceiling at both, 95%
Wilson-LB acquisition (0.987) sits far above the 0.90 floor at both, and no
trial produced an `error` outcome at either point across 600 total trials.

Nominal (100%-success) useful throughput at this frame geometry is
803 bytes * 8 / 3.172 s = 2,025.2 bit/s, 1.3% above the 2,000 bit/s floor.
The table's "useful bit/s" column is the stricter, frame-loss-prorated
figure `benchmark_hf3.py` reports (payload bytes * 8 * decoded_count /
(total_trials * frame_seconds)), which folds each frame lost to the point's
own real FER directly into the rate rather than assuming ARQ recovers it
for free:

- At benign/static +8 dB, decode rate was 100% (300/300), so the
  frame-prorated rate equals the nominal rate, 2,025.2 bit/s -- clears the
  floor with the same (thin but positive, ~1.3%) margin the frame geometry
  gives at 100% success.
- At quiet Watterson +10 dB, the ~3% real frame loss prorates the rate down
  to 1,964.5 bit/s, **1.8% under 2,000** by this stricter proxy.
  MODE_QUALIFICATION.md section 6's actual useful-throughput gate is a
  full-session ARQ-inclusive measurement (DATA, ACKs, retries,
  connection/disconnect excluded), which was not run for HF3 -- see "What
  is not yet established" below. The frame-prorated number here is
  diagnostic, the same convention HF2's `RESULTS.md`/`benchmark_hf2.py`
  use, and is reported honestly rather than rounded up: at the quiet
  Watterson boundary, on this proxy metric, HF3 is essentially at the
  floor rather than comfortably above it, despite the frame-level
  FER/acquisition gates clearing with real margin.

Artifacts: `results/hf3_quiet_watterson_confirmed_final.json`,
`results/hf3_benign_static_confirmed_final.json` (also retained under
`logs/mode_qualification/hf-ssb/hf3/2026-08-31/`).

## Supporting screen (bracketing points, 300 trials each, same campaigns)

| Model | Point | Trials | Decoded (FER Wilson-UB) | Useful bit/s |
| --- | ---: | ---: | ---: | ---: |
| `mid_latitude_quiet` | 6 dB (below envelope) | 300 | 277/300 (0.112) | 1,870.0 |
| `mid_latitude_quiet` | 8 dB (below envelope) | 300 | 287/300 (0.073) | 1,937.5 |
| `mid_latitude_quiet` | **10 dB (required boundary)** | 300 | 291/300 (0.056) | 1,964.5 |
| `mid_latitude_quiet` | 14 dB (extra margin) | 300 | 287/300 (0.073) | 1,937.5 |
| benign/static | 4 dB (below envelope) | 300 | 300/300 (0.013) | 2,025.2 |
| benign/static | 6 dB (below envelope) | 300 | 300/300 (0.013) | 2,025.2 |
| benign/static | **8 dB (required boundary)** | 300 | 300/300 (0.013) | 2,025.2 |
| benign/static | 12 dB (extra margin) | 300 | 299/300 (0.019) | 2,018.5 |

Benign/static shows essentially flat, saturated performance from 4 dB
through 12 dB -- the point grid does not locate a transition inside this
range, which is expected for a near-line-of-sight-style channel this design
comfortably clears well below its required boundary. The quiet-Watterson
FER does not fall monotonically with SNR above the boundary (8 dB, 10 dB,
and 14 dB all land in the 0.056-0.073 range on this campaign's seeds) --
this is sampling noise around a true rate the per-point 300-trial counts do
not fully resolve, not a design ceiling: the 6 dB point (below the
envelope, included only to locate the boundary) is the one that clearly
separates from the rest.

## Verdict against the Level 3 target

- **Occupied bandwidth**: measured (not just nominal-span) 99%-power
  occupied bandwidth is ~1,758 Hz for both a representative (half-capacity)
  and maximum-length frame -- comfortably under the 2,300 Hz ceiling with
  ~540 Hz of margin (`experiments/hf3/test_hf3.py`'s
  `test_occupied_bandwidth_is_under_the_2300hz_ceiling`).
- **Benign/static +8 dB**: **clears** the frame Monte Carlo gate with wide
  margin (100% decoded, FER Wilson-UB 0.013) and comfortably meets the
  useful-throughput floor by both the nominal and frame-prorated measures
  (2,025.2 bit/s at 100% decode).
- **Quiet Watterson +10 dB**: **clears** the frame Monte Carlo gate (FER
  Wilson-UB 0.056, acquisition Wilson-LB 0.987, zero errors) at the
  confirmed (>=300-trial) tier. Frame-level useful throughput is at
  (nominal, 2,025.2 bit/s) to just under (frame-loss-prorated, 1,964.5
  bit/s) the 2,000 bit/s floor at this exact boundary point -- not the
  comfortable margin the design brief hoped for. The nominal design ceiling
  clears the floor, and real ARQ would very likely absorb the ~3% frame
  loss measured here at a real but smaller throughput cost than the
  frame-prorated proxy assumes, but that is not demonstrated (see below).
- **Both required Level 3 envelope points clear the frame Monte Carlo gate**
  (FER, acquisition, zero errors) at the confirmed evidence tier. HF3 does
  **not** yet have `passed`-grade evidence for the complete Level 3 target
  rung as MODE_QUALIFICATION.md's promotion table defines it, because the
  frame Monte Carlo gate is only one of several required gates and the
  full-session useful-throughput gate specifically was not run (see below).

## What is not yet established

- **Full-session useful throughput** (MODE_QUALIFICATION.md section 6):
  not measured. The frame-prorated numbers above are a diagnostic proxy,
  not the required bulk-transfer-with-ARQ measurement; real ARQ likely
  recovers most of the ~3-7% frame loss measured at the quiet-Watterson
  points at a real but smaller throughput cost than this proxy assumes,
  but that is not demonstrated. This is the single largest remaining gap
  for a genuine Level 3 throughput claim at the quiet-Watterson boundary.
- **Adjacent-rung overlap** (section 6): not measured against HC0/HC1/HF2.
- **Bounded CI regression**: added (`tests/test_channel_regressions.py`,
  2 trials/point at both required boundary points), but 2 trials at a
  point with a genuine few-percent FER is not a reliability claim, only a
  smoke anchor; see that file's module docstring for the general caveat.
- **Full-stack sessions, ARQ, adaptation, recovery** (section 4): not run.
- **Hardware evidence** (section 5): none. No radios were available for
  this work. HF3 has zero bidirectional hardware frames and zero hardware
  session evidence.
- **CPU/RSS/resource evidence** (section 7): not measured.
- **Interoperability**: HF3 has not been tested against any implementation
  other than itself.
- **Occupied-bandwidth confidence interval**: the measurement above is a
  single deterministic FFT-based estimate per frame length, not a
  statistical campaign across many random payloads/channel realizations;
  MODE_QUALIFICATION.md section 6 asks for an upper confidence bound, which
  this single-point measurement does not itself provide (though the ~540 Hz
  margin to the ceiling makes it unlikely a wider campaign would cross it).
- **Sample-clock error in the benign/static recipe**: `benchmark_hf3.py`'s
  `benign_static_channel` does not include a `SampleClockChannel` stage.
  It was tried and measured to be computationally impractical at
  realistic few-ppm error (the fraction `scipy.signal.resample_poly` needs
  to represent a few ppm precisely has a denominator on the order of
  10^5-10^6, so its polyphase filter design made each ~3 s frame take
  multiple seconds to process -- roughly an hour for this campaign on that
  stage alone). SPEED_LADDERS.md's benign/static definition names filter,
  frequency-offset, drift, level, and nonlinearity stages, not sample-clock
  error specifically, so this is a scoped omission, not a shortcut around
  a required stage -- but it means clock-rate mismatch is not represented
  in this evidence.

Given these gaps, HF3's honest disposition is **Experimental only**: both
required frame Monte Carlo points clear their statistical gates at the
confirmed tier, but the additional gates listed above -- most importantly
full-session/ARQ throughput, which is the only evidence that would turn the
quiet-Watterson boundary's marginal frame-prorated throughput number into a
real application-throughput claim -- remain unmeasured. Optional and
Default promotion require those gates, none of which this campaign
attempts.
