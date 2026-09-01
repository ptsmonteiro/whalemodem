# HF3 results

Confirmed-boundary Monte Carlo evidence for HF3 against Speed Ladder Level 3
("fast data", SPEED_LADDERS.md): minimum net application throughput per frame
2,000 bit/s, required envelope benign/static at +8 dB waveform SNR and above,
quiet Watterson fading at +10 dB and above. All numbers below are from
`whale.qualification`'s Monte Carlo helpers over `whale.channel`, run through
`experiments/hf3/benchmark_hf3.py` -- no radios were available for this work.

A later radio smoke campaign is recorded separately under
`logs/mode_qualification/hf-ssb/hf3/2026-09-01-hardware/`: 3/3 full-capacity
frames decoded from IC-7300 to IC-705. It is provisional retained-direction
evidence below the 40-frame minimum, and is not part of the simulated
campaign below.

## Confirmed-boundary gate (>=300 trials)

Per MODE_QUALIFICATION.md section 3, each required boundary point needs
>=300 independent trials, 95% Wilson-UB FER <= 0.10, 95% Wilson-LB
acquisition >= 0.90, and zero `error` outcomes.

| Point | Trials | Acquire (Wilson-LB) | Decoded (FER Wilson-UB) | Errors | Net bit/s per frame |
| --- | ---: | ---: | ---: | ---: | ---: |
| benign/static +8 dB (required boundary), seed 20260901 | 300 | 300/300 (0.987) | 300/300 (FER Wilson-UB 0.013) | 0 | 2,000.0 |
| `mid_latitude_quiet` +10 dB (required boundary), seed 20260901 | 300 | 300/300 (0.987) | 291/300 (FER Wilson-UB 0.056) | 0 | 2,000.0 |

**Both required boundary points clear the frame Monte Carlo gate**: 95%
Wilson-UB FER (0.013, 0.056) sits well under the 0.10 ceiling at both, 95%
Wilson-LB acquisition (0.987) sits far above the 0.90 floor at both, and no
trial produced an `error` outcome at either point across 600 total trials.

Net application throughput per full-capacity DATA frame is
793 DATA-chunk bytes * 8 / 3.172 s = 2,000.0 bit/s, exactly meeting the
2,000 bit/s floor. The former 803-byte numerator included the 10-byte air
header and was not net application payload.
The channel benchmark also reports the stricter, frame-loss-prorated
figure `benchmark_hf3.py` reports (payload bytes * 8 * decoded_count /
(total_trials * frame_seconds)), which folds each frame lost to the point's
own real FER directly into the rate rather than assuming ARQ recovers it
for free. It is reliability context, not the mode throughput criterion:

- At benign/static +8 dB, decode rate was 100% (300/300), so the
  frame-prorated rate equals the nominal rate, 2,025.2 bit/s -- clears the
  floor with the same (thin but positive, ~1.3%) margin the frame geometry
  gives at 100% success.
- At quiet Watterson +10 dB, the ~3% real frame loss prorates the rate down
  to 1,964.5 bit/s, **1.8% under 2,000** by this stricter proxy.
  The frame-prorated number is diagnostic and is reported separately from
  section 4's per-frame net-throughput gate. FER/acquisition already enforce
  delivery performance at this boundary.

Artifacts: `results/hf3_quiet_watterson_confirmed_final.json`,
`results/hf3_benign_static_confirmed_final.json` (also retained under
`logs/mode_qualification/hf-ssb/hf3/2026-08-31/`).

## Supporting screen (bracketing points, 300 trials each, same campaigns)

| Model | Point | Trials | Decoded (FER Wilson-UB) | Loss-prorated bit/s |
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

- **Occupied bandwidth**: **clears** the 2,300 Hz gate. A promotion-sized
  campaign measured 300 independent random payloads in each of the
  representative (401-byte) and maximum (803-byte) classes. The worst
  distribution-free 95.1% upper confidence bound on the population
  99th-percentile 99%-power bandwidth is 1,774.59 Hz, leaving 525.41 Hz of
  margin. See
  `logs/mode_qualification/hf-ssb/hf3/2026-09-01-bandwidth/INDEX.md`.
- **Benign/static +8 dB**: **clears** the frame Monte Carlo gate with wide
  margin (100% decoded, FER Wilson-UB 0.013) and meets the per-frame net
  throughput floor (2,000.0 bit/s).
- **Quiet Watterson +10 dB**: **clears** the frame Monte Carlo gate (FER
  Wilson-UB 0.056, acquisition Wilson-LB 0.987, zero errors) at the
  confirmed (>=300-trial) tier. Per-frame net throughput is 2,000.0 bit/s;
  the historical loss-prorated statistic is reported separately.
- **Both required Level 3 envelope points clear the frame Monte Carlo gate**
  (FER, acquisition, zero errors) at the confirmed evidence tier. HF3 does
  has passed throughput and simulated-channel evidence for the Level 3
  target. Other promotion gates remain open (see below).

## What is not yet established

- **Per-frame net throughput** (MODE_QUALIFICATION.md section 4): passes at
  2,000.0 bit/s. Session throughput remains separate system evidence.
- **Ladder qualification** (section 6): adjacent-rung overlap, fallback, and
  re-climb are not measured against HC0/HC1/HF2. These do not block HF3's
  waveform qualification.
- **Bounded CI regression**: added (`tests/test_channel_regressions.py`,
  2 trials/point at both required boundary points), but 2 trials at a
  point with a genuine few-percent FER is not a reliability claim, only a
  smoke anchor; see that file's module docstring for the general caveat.
- **Complete modem/system qualification** (section 7): connection lifecycle,
  general ARQ fault recovery, and hardware sessions are not run. These do not
  block HF3's waveform qualification.
- **Hardware evidence** (section 5): provisional in one direction only.
  HF3 decoded 3/3 retained full-capacity frames from IC-7300 to IC-705, with
  valid CRCs and all 36 carriers present. IC-705 to IC-7300 has no successful
  HF3 frame; that leg is non-retained characterization under the current
  criterion. The >=40-frame retained-direction minimum and minimum setup
  metadata are still missing. See
  `logs/mode_qualification/hf-ssb/hf3/2026-09-01-hardware/INDEX.md`.
- **CPU/RSS/resource evidence** (section 7): not measured.
- **Interoperability**: HF3 has not been tested against any implementation
  other than itself.
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

Given these gaps, HF3's honest waveform disposition is **Experimental only**.
Both required frame Monte Carlo points clear their statistical gates. Optional
waveform status still needs a documented
40-frame IC-7300-to-IC-705 retained-direction run; the bandwidth gate now
passes.
Default additionally needs 100 retained-direction frames on each of two
materially different radio/audio pairs and bounded resource evidence. Ladder
and complete-system gaps remain important project work, but no longer block
HF3 waveform promotion.
# Fixed-mode useful-transfer result (2026-09-01)

The lifecycle-free system-diagnostic campaign transferred 10,000 verified
application bytes in each of six independent trials at both Level 3 boundary
points. Its session throughput is below 2,000 bit/s: benign/static +8 dB
was 855.3 bit/s (95% median CI 855.3--855.3), and quiet Watterson +10 dB was
855.3 bit/s (756.7--855.3). All 12 transfers completed exactly; only one DATA
retransmission occurred. The binding cost is the existing stop-and-wait
protocol's fixed 3.42-second HC0 ACK after every fixed-length HF3 DATA frame,
not waveform delivery. This is not the per-mode throughput gate; it diagnoses
a link/system bottleneck. See
`logs/mode_qualification/hf-ssb/hf3/2026-09-01-fixed-transfer/INDEX.md`.
