# HR0 point 4: capacity diagnosis and HR0-B redesign

> **Point-5 update (2026-08-31):** the small real-receiver screen requested
> by this handoff is complete in [`SCREEN.md`](SCREEN.md). Revision B's full
> class has promising boundaries, but its tiny ACK class fails the
> retry-weighted 18 bit/s session gate and its 19.54 s full frame exceeds the
> production 10 s RX buffer and 8 s HF useful-frame policy. The current
> decision is **redesign before the full campaign**. The point-4 decision and
> evidence below are retained as the rationale for running that screen.

## Point-4 decision (executed)

**Go with HR0-B to a small real-receiver AWGN/Watterson boundary screen; do
not run the full campaign yet.**  HR0-A is preserved as revision A in
`hr0.py`.  Revision B is experiment-only in `hr0b.py`; it is not registered
in `whale/`, has no stable mode ID, and is not a production or VARA parity
claim.

The frozen 200,000-sample soft-information screen explains HR0-A's miss:
at -24 dB whole-keying waveform SNR, its last-two-observation channel carries
only 0.786 coded-modulation bit/tone, while the checked wire asks for 1.333.
The independent bit interface is worse: exact BICM gives 0.389 bit/tone and
the implemented max-log proxy gives 0.353.  No replacement binary code can
repair that rate deficit at the same geometry and airtime.

HR0-B changes the interface and energy allocation rather than weakening the
target.  Its full class delivered 19/20 held-out oracle-aligned checked frames
at -24 dB, 20/20 at -23 dB, and 0/20 at -25 dB.  Its bounded real acquisition
and decoder delivered 3/3 high-SNR frames in each of
`mid_latitude_disturbed`, `mid_latitude_disturbed_nvis`, and
`high_latitude_disturbed`.  These are deliberately small screening samples,
not statistical gates or an operating envelope.

## Reproducible evidence

The retained artifact is
[`results/hr0b_redesign_screen_20260830.json`](results/hr0b_redesign_screen_20260830.json).
It was produced with:

```sh
/Users/pedro/miniconda3/envs/gnuradio/bin/python \
  experiments/hr0/redesign_screen.py \
  --information-samples 200000 --awgn-trials 20 \
  --out experiments/hr0/results/hr0b_redesign_screen_20260830.json
```

The information channel is one-of-M orthogonal signaling with unknown,
uniform carrier phase and complex unit-variance Gaussian correlation noise.
The screen uses the exact phase-marginal symbol likelihood
`log I0(2 sqrt(Es/N0) |z|)`.  It also reports exact bitwise GMI and the best
scaled max-energy/max-log GMI.  Fixed Monte Carlo seeds and a fixed
200,000-symbol sample count make every estimate reproducible.  The waveform
smokes use the repository `SnrSpec(WAVEFORM)` contract over the exact full
keying and the ordinary 48 kHz to 12 kHz receive front end.

## Why HR0-A misses by about 6 dB

HR0-A's tiny five-seed smoke put its practical transition around -19 to
-18 dB even with aligned timing.  The following accounting separates the
contributors without pretending that five trials locate an exact boundary.

| Contributor | Reproducible result | Interpretation |
| :--- | ---: | :--- |
| Discarded first observation | 1.761 dB energy; CM information 1.358 -> 0.786 bit/tone at -24 dB | The last-two receiver throws away one third of tone energy to cover 7 ms delay. |
| Lead, preamble, pilots, tail | 18.883% of keying; 0.909 dB relative to DATA tones | This energy helps acquisition/tracking rather than the 426 coded DATA tones. |
| Checked framing | 568 protected input bits / 432 useful DATA bits; 1.189 dB | Length, CRC, zero fill, and termination are honestly charged. |
| Tone count | At the same last-two energy: 8/16/32-MFSK CM = 0.687/0.787/0.846 bit/tone | 32 tones buy only 0.059 bit/tone over 16 while doubling the 125 Hz-grid bank to 4 kHz. |
| Bitwise demapper | 16-MFSK CM 0.786; exact BICM 0.389; max-log GMI 0.353 bit/tone | The dominant algorithmic loss is converting a 16-way observation into four independent bit metrics. Exact bit LLRs recover only 0.036 bit/tone. |
| Required coded rate | 568 / 426 = 1.333 checked bits/tone | It exceeds even all-three CM information (1.358) once finite-frame margin is required, and exceeds all-three exact BICM (0.804) outright. |
| Finite termination | 8 / 568 inputs; 0.062 dB | Small but explicit rate loss. |
| Finite blocklength proxy | all-three CM dispersion 3.411 bit2; normal 10%-error backoff at 426 tones is about 0.115 bit/tone | The approximate finite-block ceiling is 1.244, below the requested 1.333 even with optimal symbol coding. This is a proxy, not a theorem for the implemented code. |
| Code/decoder residual | all-three exact BICM crosses 1.333 between -23 and -22 dB; HR0-A's tiny empirical edge is -19/-18 dB | Roughly 3 dB or more remains in the finite K=7 convolutional code/decoder after the asymptotic demapper threshold. The five-trial smoke cannot split that residual more precisely. |

The arithmetic also explains why merely adding a stronger binary
convolutional, LDPC, or polar code was rejected.  At -24 dB, the bitwise
channel supplies 0.353--0.389 bit/tone, far below the 1.333 requested by A.
Full-frame repetition or rate 1/6 can lower the request, but cannot retain the
18 bit/s clean DATA+ACK budget.  A multilevel or iterative coded-modulation
LDPC/polar design remains possible, but it is a new symbol-aware architecture,
not a drop-in decoder improvement.

## Bounded architecture screen

### Observation, dwell, guard, and tone count

The retained screen evaluates 8, 16, and 32 tones and records the rejected
alternatives machine-readably.

- 8-MFSK on the 8 ms/125 Hz grid has slightly better bitwise GMI than 16,
  but lower symbol-channel information at the energy that matters.  It does
  not solve the coded-modulation deficit.
- 16-MFSK retains the 2 kHz nominal bank, 8 ms observation, 24 ms dwell, and
  125 Hz tone separation.  The worst 30 Hz spread is 0.24 of tone spacing,
  and the 8 ms guard still exceeds the 7 ms maximum differential delay.
- 32-MFSK on that grid needs a 4 kHz bank and is rejected.  Compressing it to
  62.5 Hz spacing needs 16 ms observations; the worst spread becomes 0.48 of
  spacing, and a full 16 ms guard consumes too much dwell.  It is deferred,
  not represented as a hidden bandwidth win.
- A 32 ms or longer dwell improves AWGN guard efficiency but requires either
  coherent combining over a larger fraction of the 30 Hz fade or fewer code
  symbols in the session budget.  HR0-B instead coherently combines only the
  last two 8 ms observations.  The +20 dB Watterson smoke verifies wiring,
  but a near-boundary 7 ms/30 Hz screen is still mandatory.

### Coding and energy allocation

HR0-B uses a two-memory, rate-1/4 GF(16) convolutional inner code for full
frames.  A trellis branch consumes the complete 16-tone likelihood, avoiding
the BICM loss.  A shortened RS(96,70) outer code corrects up to 13 erroneous
bytes left by the inner decoder.  The full inner code protects 96 bytes plus
two terminating GF(16) symbols in 776 tone symbols: 0.990 entropy bit/tone at
the inner boundary.  The exact HR0-B clean observation proxy at -24 dB is
1.311 bit/tone; a 10%-error normal approximation over 776 tones is 1.227,
leaving a useful screening margin over 0.990.

The discarded 8 ms guard is silent, and the two trusted observations carry
the peak-limited tone through 0.5 ms raised-cosine-squared edges.  This moves
energy out of a receive interval known to be contaminated by a 7 ms echo.
At the frozen whole-keying SNR it yields 2.235 linear symbol `Es/N0` for the
trusted coherent pair, while peak amplitude remains `0.13*sqrt(2)`.  The
measured complete-keying 99% cumulative-power width is 1,969.86 Hz
(328.86--2,298.72 Hz), inside the 2,300 Hz width gate.  This waveform is no
longer constant-envelope during the guard; the peak-limited radio trade is
explicit and must be tested with SSB filtering and ALC when radios exist.

The tiny class omits RS and uses the first three GF(16) inner outputs.  It is
less protected than full DATA and exists to preserve ACK latency.  Incremental
parity or repeated ACKs remain an ARQ option if the tiny-class boundary is too
high; they are not counted in the clean rate.

## HR0-B wire revision and rate

Revision B is intentionally wire-incompatible with A:

- two 16-tone balanced permutations (32 symbols) identify tiny and full
  classes instead of A's 80-symbol, three-class word;
- every guarded symbol is 8 ms silence plus two active 8 ms observations;
- active edges use a 0.5 ms raised-cosine-squared ramp;
- full packets use the checked 70-byte packet, whitening, shortened
  RS(96,70), two GF(16) termination symbols, and the rate-1/4 GF(16) trellis;
- tiny packets use an 18-byte checked packet, whitening, two termination
  symbols, and the rate-1/3 GF(16) trellis; and
- PN tone masks randomize both classes without changing energy.

| Quantity | HR0-B value |
| :--- | ---: |
| Full body | 776 tones / 18.624 s |
| Full complete keying | 19.540 s |
| Full 54-byte frame useful rate | 22.109 bit/s |
| Tiny complete keying | 3.652 s |
| Two 0.3 s turnarounds | 0.600 s |
| Clean DATA + tiny ACK exchange | 23.792 s |
| Clean projected long-transfer rate | 18.157 bit/s |

The 0.157 bit/s surplus is very small.  It is a clean projection, not a
measured session rate, and any ordinary retransmission rate will pull the
session below 18 bit/s.  HR0-B therefore advances only to a boundary screen;
it is not ready for session or production promotion.

## Implemented receiver and bounded work

`hr0b.py` preserves A and exposes `experiments.hr0.hr0b:HR0B`.  The receiver:

- searches at most 1,001 timing cells, 27 CFO cells, and two classes: 54,054
  coarse cells;
- deduplicates to at most 16 candidates and refines each over 195 cells;
- computes exact phase-marginal likelihoods after coherently combining the
  two trusted observations;
- bounds a full GF(16) decode at 194 trellis steps, 256 states, and 16
  branches per state (`794,624` branch metrics per candidate); and
- accepts payload only after inner termination, RS, length, CRC32, and zero
  fill checks.

The likelihood scale is estimated from the known class word.  This is
AWGN-aligned; it is not yet a fading-amplitude tracker.  The high-SNR
Watterson result verifies acquisition and channel plumbing, not a low-SNR
fading boundary.

## Exact screen results and limitations

| Screen | Result |
| :--- | :--- |
| Held-out oracle AWGN -25 dB | 0/20 |
| Held-out oracle AWGN -24 dB | 19/20 |
| Held-out oracle AWGN -23 dB | 20/20 |
| Real RX, disturbed mid, +20 dB | 3/3 |
| Real RX, disturbed NVIS (7 ms), +20 dB | 3/3 |
| Real RX, disturbed high (7 ms/30 Hz), +20 dB | 3/3 |

Twenty trials are not a qualification gate: 19/20 has a wide Wilson interval
and does not establish the required FER upper bound.  No canonical or
fixed-N0 Watterson boundary, continuous fade, false-acquisition campaign,
interference/CW/notch result, CPU target result, full session, radio result,
or VARA comparison has been run.  The 18.157 bit/s figure has no retry margin
and is the main redesign risk.

## Handoff

Proceed sequentially with a **small HR0-B real-receiver screen**, not the full
300-trial campaign:

1. Run 30 exploratory real-receiver frames at -25, -24, -23, and -22 dB
   canonical AWGN, separately reporting acquisition and checked-body failure.
2. Run 10--30 exploratory frames at high SNR and coarse boundary points in
   disturbed mid, disturbed NVIS, and disturbed high.  Stop or redesign if
   coherent pair combining recreates an impairment floor in either 7 ms
   preset.
3. Measure tiny-class AWGN and Watterson boundaries.  Stop or redesign the
   ACK/IR policy unless the predicted retry-weighted session rate remains at
   least 18 bit/s.
4. Only if those screens pass, freeze the likelihood estimator and
   acquisition threshold on training seeds, run held-out absent-window tests,
   and then return to `PLAN.md`'s statistical campaign.

Current decision: **go HR0-B to the next small screen**.  A failure of the
tiny-class/session gate is a framing/ARQ redesign; a 7 ms/30 Hz floor is a
geometry redesign; failure of real AWGN near -24 dB after acquisition is
a stop for this GF(16)+RS revision.  The target must not be weakened.

## Subsequent result

The tiny-class/session failure occurred; see `SCREEN.md`. The superseding
campaign decision is **redesign**.
