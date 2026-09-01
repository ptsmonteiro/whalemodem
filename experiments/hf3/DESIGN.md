# HF3 design record

This is an architecture decision record and an iteration log, not just a
final-state description: HF3's numbers moved several times during design as
Monte Carlo screens found problems a clean-signal round trip could not show.
It follows the shape of `experiments/hf2/DESIGN.md` (a record with dated
notes), but every number in it was picked independently for HF3 -- Level 3's
target envelope is not Level 2's, and this waveform is not a copy of HF2's
geometry, pilot layout, or frame size.

## Target envelope (SPEED_LADDERS.md, Level 3 "fast data")

- >= 2,000 bit/s useful application throughput (decoded payload bytes /
  `mode.airtime()`, not raw/coded rate).
- <= 2,300 Hz occupied bandwidth.
- Benign/static SSB path (<=0.1 ms differential delay spread, <=0.005 Hz
  Doppler spread, full filter/offset/drift/level/nonlinearity chain) at
  +8 dB waveform SNR and above.
- Quiet Watterson fading (`mid_latitude_quiet`: 0.5 ms delay spread, 0.1 Hz
  Doppler) at +10 dB waveform SNR and above.

## Why OFDM, sparse-pilot coherent 16-QAM

Level 3's envelope is friendlier than HF2's Level 2 envelope in both
directions the ladder allows: quiet Watterson at +10 dB (vs HF2's +5 dB --
5 dB more margin at the same fading class) and a benign/static point that is
near-flat and low-Doppler, easier than any Watterson class. That headroom
was expected to buy either a denser constellation, a sparser pilot comb, or
a larger frame than HF2's Level 2 design needed. All three turned out to be
partly true and partly not -- see the dated notes below; the actual amount
of headroom available was smaller than a first-principles SNR estimate
suggested, because per-carrier tracking accuracy, not raw AWGN margin, was
the binding constraint at both required points.

OFDM was chosen for the same reason every other on-air data mode in this
repository uses it at Level 2 and above: a cyclic-prefixed multicarrier
symbol makes a bounded delay spread transparent (a pure per-carrier phase
rotation) rather than the intersymbol interference a single-carrier scheme
would have to equalize in time. Coherent 16-QAM (not differential, not a
denser constellation) is this design's landing point, reached empirically --
see "Iteration record" below.

## Geometry

- Sample rate 48 kHz TX, matching the project's other HF OFDM modes' audio
  boundary.
- 1,024-sample IFFT core, 64-sample cyclic prefix (1,088 samples/symbol,
  22.67 ms/symbol). The prefix is smaller than HC1/HF2's (they carry
  moderate-fading envelopes with up to 1.0 ms delay spread); 64 samples
  (1.33 ms) is still 2.6x the 0.5 ms quiet-Watterson spread this design's
  envelope actually requires, and far more than benign/static's <=0.1 ms.
- 46.875 Hz carrier spacing (48000/1024), a fresh spacing chosen
  independently of HC1/HF2's 93.75 Hz grid. Carrier bins 9-44 inclusive (36
  carriers spanning 421.875-2,062.5 Hz), which leaves ~240 Hz of nominal
  headroom under the project's 2,300 Hz occupied-bandwidth ceiling.
- A single sparse comb of pilot carriers, not a dense comb plus
  frequency-diversity carrier grouping the way HF2 uses under moderate
  fading -- Level 3's quiet/benign envelope does not need diversity against
  a persistent local notch the way HF2's moderate-fading target does (this
  was checked directly, not assumed: see "Iteration record").

## Coding and framing

- Rate-1/2 K=9 (constraint length 9, polynomials 0o561/0o753) convolutional
  code, `whale.dsp.fec.K9`, with soft-decision Viterbi decoding -- one step
  more coding gain than K=7 for the same rate; the tail-bit cost is trivial
  next to the payload. See "Iteration record" for why K7 alone was not
  swapped in blind: it was measured against K9 at the same point and made
  no measurable difference by itself, but K9 costs nothing to keep and this
  design needed every dB it could get.
- CRC32 integrity check and length field via `whale.dsp.framing.PacketCodec`,
  the same shared payload codec every OFDM mode in the repository uses.
- Self-contained frame: length, CRC32 and FEC live inside HF3's own payload
  grid (`whale.framing`'s PN-sync generic format is bypassed, the same
  independent choice VF3/HC0/HC1/HF2 each made).
- Fixed-length frame, for the same reason HC1/HF2 are: a variable-length
  OFDM frame needs a separately coded header to learn the length before
  decode is possible, and that cost is not worth it next to HF SSB's
  PTT/ALC/turnaround overhead.
- Shares `whale.modes.hf_lead`'s common HF lead-in/signature block
  (`HF3_LABEL = 3`, a fourth six-symbol HC0-grid block alongside HC0/HC1/
  HF2's, distinct from all three so `hf_lead.candidates()` cannot confuse
  the labels). Like HF2's label, this was not run through the
  `experiments/signature128` screening process (see that experiment's
  README) -- HF3 is an experiment-local waveform not yet promoted into
  `whale/modes/`'s screened set, and follows the same documented precedent
  HF2's label note already establishes for that gap.

## Constellation: a generic Gray-coded M-PAM/M-QAM builder

Rather than hand-deriving a fixed 16-QAM soft-bit formula the way HF2's
`qam16_soft_bits` does, HF3's mapper (`_gray_pam_table`, `qam_from_bits`,
`qam_soft_bits`) is parameterized on `BITS_PER_AXIS`: it builds the
Gray-coded PAM level table and a max-log per-bit LLR (nearest distance to a
1-bit level minus nearest distance to a 0-bit level, scaled by a per-symbol
reliability weight) generically, for any per-axis bit count. This was a
deliberate choice to support trying denser constellations without rewriting
the demodulator -- which the design actually did, once (see below).

## Pilot tracking

Per payload OFDM symbol, `_pilot_channel_estimate` reads each pilot
carrier's own complex gain fresh (`(raw - header offset) / known pilot
value`), smooths it across a short window of neighboring symbols
(`PILOT_TIME_SMOOTHING_SYMBOLS`), then interpolates **in polar form**
(magnitude and unwrapped phase separately, not linear real/imaginary
interpolation) across the carrier axis to the data bins. Each data
carrier's soft bits are then weighted by that same interpolated gain's
power, so a carrier caught in a momentary fade discounts itself rather than
corrupting the whole frame. This is the same general per-symbol
pilot-tracking idea HF2 uses (a fresh per-symbol read, not a ratio against
a stale header fit -- see HF2's module docstring for why a static
header-time gain does not survive a moving channel), independently
implemented and independently tuned for HF3's much sparser pilot comb.

## Iteration record

Dates below are development-session markers within this task, not wall-clock
calendar dates; they record the order screens were run, which is what
matters for reproducing the reasoning.

**Note 1 -- 64-QAM was tried first and dropped.** The initial candidate used
64-QAM (6 bits/carrier, `BITS_PER_AXIS = 3`) on a 9-pilot comb (36 carriers,
27 data), reasoning that Level 3's 5 dB extra margin over HF2's quiet-fading
floor (+10 dB vs +5 dB) could fund a denser constellation the way the task
brief suggested. A first 4-pilot version failed completely at the
benign/static +8 dB point (0/20 decoded); instrumentation showed the true
cause was **not** insufficient pilot density for tracking a moving channel --
even a genie reference using the *exact* transmitted values at every pilot
position showed the same ~0.2-0.4 EVM regardless of pilot spacing (2 through
9 carriers). That ruled out interpolation error and pointed at raw
per-carrier SNR: median per-carrier SNR at this point measured ~14-20 dB,
enough for a comfortable 16-QAM margin (half-min-distance/noise-sigma ratio
~2-3) but only ~0.8x 64-QAM's half-min-distance -- the constellation was
simply too dense for the delivered SNR budget once real per-carrier noise
(not a theoretical waveform-SNR-to-Nyquist-processing-gain estimate) was
accounted for. 64-QAM was dropped in favor of 16-QAM, matching HF2's
modulation order -- reached empirically here, not copied from there.

**Note 2 -- linear vs. polar pilot interpolation.** An early 16-QAM
candidate interpolated pilot gain by linear real/imaginary interpolation
across the carrier axis, the same mechanism HF2 uses. Measured against a
genie reference under even a plain in-band Butterworth filter (no fading),
this showed a consistent bias: interpolating I/Q directly chord-cuts across
any real phase excursion between two pilots, shrinking the reconstructed
magnitude and biasing the phase toward the chord's midpoint rather than the
arc the channel actually traces. Switching to polar interpolation (magnitude
and unwrapped phase interpolated separately) removed this bias. A cubic
spline was also tried in place of linear interpolation on both axes; it
measured *worse* (overshoot/oscillation between sparse pilots), so plain
piecewise-linear polar interpolation was kept.

**Note 3 -- benign/static's Watterson stand-in needed asymmetric path
power.** SPEED_LADDERS.md's benign/static class has no ready-made channel
recipe in `whale.scenario`, and `whale.channel.WattersonChannel` requires a
second path with strictly positive power. An initial two-path model with
*equal* power on both paths (matching the standard Watterson quiet/
moderate/disturbed preset convention) produced a real, sharp notch
somewhere across HF3's ~1.6 kHz carrier band even at a 0.05 ms delay --
confirmed by instrumenting `channel.gain` directly (one carrier measured
~1.3x while its neighbors measured ~2.5-3.3x, with a smooth phase sweep
either side, the signature of two-ray interference). That is a
moderate/disturbed-fading phenomenon, not the near-line-of-sight path
SPEED_LADDERS.md's benign/static class describes. `benchmark_hf3.py`'s
`benign_static_channel` now gives the second path -17 dB relative power
(`BENIGN_STATIC_SECOND_PATH_POWER = 0.02`) instead of equal power, which
removed the artificial notch while keeping the required stage chain
(filter, frequency offset + drift, gain, light clipping, then
waveform-referenced AWGN). A `SampleClockChannel` stage was also tried
and dropped: representing a realistic few-ppm clock error needs a
`Fraction` denominator on the order of 10^5-10^6, which makes
`scipy.signal.resample_poly`'s polyphase filter design take multiple
seconds per ~3 s frame -- impractically slow for a 300-trial x 4-point
confirmed campaign (measured at roughly an hour for that stage alone).
SPEED_LADDERS.md's benign/static definition names filter,
frequency-offset, drift, level, and nonlinearity stages, not sample-clock
error specifically, so this is a scoped omission -- see RESULTS.md's
"What is not yet established" section.

**Note 4 -- pilot density, frame size, and FEC, traded against each other
at the quiet-Watterson +10 dB point.** This was the point that needed the
most iteration. A first 16-QAM/9-pilot/150-symbol candidate cleared
benign/static +8 dB comfortably (25/25 decoded, ~2,400 bit/s) but only
decoded 20/25 at quiet Watterson +10 dB. Several levers were measured
independently rather than assumed:

- *Frame length*: shrinking from 150 to 50-100 payload symbols changed the
  quiet-Watterson decode rate inconsistently across trial batches (no clean
  monotonic relationship was found within the trial counts this design
  budget allowed), so frame length was set primarily by the throughput
  floor, not tuned purely against FER.
- *Pilot density*: comparing 4, 6, 9 and 12 pilots (out of 36 carriers) at a
  fixed frame length showed real, if noisy, improvement with more pilots,
  but going from 6 to 12 pilots costs enough data carriers that the
  bandwidth-limited throughput ceiling (`N_DATA_CARRIERS * bits/carrier / 2
  code rate / symbol duration`) drops below the 2,000 bit/s floor even at
  the largest frame size tried. 9 pilots (36 carriers, 27 data) was the
  balance point that kept the ceiling comfortably above the floor.
- *Temporal pilot smoothing*: quiet Watterson's 0.1 Hz Doppler spread is a
  ~10 s coherence time, three orders of magnitude longer than a handful of
  OFDM symbols, so a short centered moving average of each pilot's raw
  per-symbol complex gain (`PILOT_TIME_SMOOTHING_SYMBOLS`) smooths
  AWGN-driven pilot-estimation noise for free. This measurably helped
  (e.g. one screen went from 20/25 to 29/30 decoded at the same point after
  adding it) but did not fully close the gap on its own.
- *K7 vs. K9*: swapping the FEC code alone, holding geometry fixed, did not
  change the decoded set at all in one controlled comparison (identical
  failing trials, identical throughput) -- the observed failures are
  concentrated (multi-hundred-bit chunks of the coded frame wrong at once,
  consistent with a carrier or small group of carriers seeing genuinely low
  SNR for most of a frame's duration under a specific fade realization,
  not scattered single-bit AWGN errors a stronger convolutional code
  absorbs). K9 was kept anyway (no cost, and it may help at other points
  not screened here) but is not the fix for this specific residual.
- *Static (header-only) equalization*: as a control, replacing the
  per-symbol pilot-tracked equalization with a single static per-carrier
  gain from the header fit failed **every** trial at quiet Watterson
  +10 dB (0/40) where the same frames decoded at ~85-95% with per-symbol
  tracking. This confirms per-symbol tracking is doing real, necessary
  work -- the channel moves enough within one frame that a static estimate
  is not viable at this point, even though quiet Watterson's Doppler
  spread nominally implies a long coherence time.

The final configuration (9-pilot comb, `PILOT_TIME_SMOOTHING_SYMBOLS = 11`,
75 payload symbols, K9) is the best measured combination found within this
design's time budget: comfortable margin and reliability at benign/static
+8 dB, and useful throughput at or above the 2,000 bit/s floor at quiet
Watterson +10 dB with FER close to, but not conclusively inside,
MODE_QUALIFICATION.md's 95%-Wilson-upper-bound <=10% gate at the trial
counts screened. See `RESULTS.md` for the confirmed-tier (>=300 trial)
numbers and an honest verdict -- this design record does not claim the gate
is cleared; it records what was tried and measured while arriving at the
final candidate.

## What was not tried

- A physical frequency-diversity scheme like HF2's (each data value on 2-3
  widely spaced physical carriers) was analyzed but not implemented: at
  this design's carrier count and occupied-bandwidth budget, halving the
  distinct-carrier count to buy diversity drops the bandwidth-limited
  throughput ceiling below the 2,000 bit/s floor even at unlimited frame
  length, so it could not have helped without also widening the carrier
  band, which was not pursued given the time budget.
- 8-PSK/32-QAM/other non-square constellations between 16-QAM and 64-QAM
  were not implemented or measured, though the generic Gray-PAM mapper
  would support a square 4-bit or non-square intermediate constellation
  with moderate additional work.
- Decision-directed or Kalman-style temporal channel tracking (combining
  pilot and data-carrier soft decisions across symbols, rather than a fixed
  moving-average window) was not attempted.
