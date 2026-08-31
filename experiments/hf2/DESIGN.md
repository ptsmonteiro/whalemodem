# HF2 design record

This is an architecture decision record, not a robustness result -- it fixes
the waveform family before real acquisition/decode is built, the same
function `hr0/DESIGN.md` and `hc1.py`'s module docstring serve for their own
modes. Numbers here are engineering starting points; the values actually
shipped come from the Monte Carlo screen in stage 3/4 of PLAN.md, and this
file is updated if they move.

## Why OFDM, pilot-assisted, coherent 16-QAM

Level 2's required envelope -- quiet Watterson fading (0.5 ms delay spread,
0.1 Hz Doppler) at +5 dB waveform SNR, moderate (1.0 ms, 0.5 Hz) at +10 dB --
is materially more SNR margin than a control/fallback design gets, and both
delay spreads are small next to a cyclic-prefixed multicarrier symbol. That
combination favors trading some of the margin for constellation density
(4 bits/carrier/symbol from 16-QAM, versus 1 from differential BPSK/QPSK)
instead of spending it all on redundancy, provided the channel can be
tracked coherently -- which needs pilots, since 16-QAM is not
differentially decodable at useful noise margin the way DQPSK is.

## Geometry

- Sample rate 48 kHz TX, matching the project's other HF OFDM modes' audio
  boundary.
- 512-sample IFFT core, 128-sample cyclic prefix (640 samples/symbol,
  13.33 ms/symbol). The 128-sample (2.67 ms) prefix covers the 1.0 ms
  moderate-class delay spread with 1.7x margin.
- 93.75 Hz carrier spacing (48000/512). Carrier bins 7-25 inclusive (19
  carriers spanning 656.25-2343.75 Hz), which sits inside a 2.4 kHz SSB
  filter with room at both skirts for uncorrected residual offset -- an
  already-geometrically-valid layout for the project's 2,300 Hz occupied
  bandwidth ceiling, arrived at independently from first principles rather
  than copied from a specific existing mode's implementation.

## Pilots and channel tracking

- 4 of the 19 carriers are **comb pilots**: present at a fixed, known BPSK
  value in every OFDM symbol (not just periodic full-pilot symbols), so the
  channel estimate is refreshed every 13.33 ms rather than every frame. At
  0.5 Hz Doppler (moderate class) the channel's coherence time is on the
  order of seconds; refreshing far faster than that gives frequency-domain
  interpolation (`whale.dsp.equalize`) a stable estimate to interpolate the
  15 data carriers from every symbol.
- Pilot bins are spread roughly evenly across the 19-carrier band so
  interpolation error is bounded at every data carrier, not just near the
  pilots.
- 15 data carriers x 4 bits (16-QAM, Gray-coded I/Q) = 60 raw bits/OFDM
  symbol.

## Coding and framing

- Rate-1/2 K=7 (171,133) convolutional code with soft-decision Viterbi
  decoding, reused unmodified from `whale.dsp.fec.ConvolutionalCode` -- this
  is the shared error-control primitive every OFDM mode in the repository
  already draws on independently, not a design choice specific to any one
  of them.
- CRC32 integrity check over the payload, via `whale.dsp.framing`'s
  primitives.
- Self-contained frame: length field, CRC32, and FEC live inside HF2's own
  payload grid (`whale.framing`, the PN-sync generic format, is bypassed --
  the same choice VF3/HC0/HC1 each made independently, for the same reason:
  an OFDM mode already carries its own acquisition header).
- Fixed-length frame (airtime independent of payload size), for the same
  reason HC1 is fixed-length: a variable-length OFDM frame needs a
  separately coded header to learn the length before decode is possible,
  and that cost is not worth it next to HF SSB's PTT/ALC/turnaround
  overhead.
- Shares `whale.modes.hf_lead`'s common HF lead-in/signature block so a
  receiver's existing HF acquisition poll can dispatch to HF2 the same way
  it dispatches to HC0/HC1, without changing the receiver's polling loop.

## Frame size (starting point, tuned in stage 3/4)

- Starting point: 40 data-bearing OFDM symbols/frame.
  - Raw bits/frame: 40 x 60 = 2,400 bits.
  - After rate-1/2 coding: 1,200 information bits = 150 bytes/frame, before
    length field and CRC32 overhead.
  - Core+guard time: 40 x 640 / 48000 = 533.3 ms, plus `hf_lead`'s lead-in
    and a short tail (same order as HC1's 2,304-sample lead-in / 960-sample
    tail) -- roughly 600-650 ms total keying.
  - At ~150 bytes/~0.6 s, nominal throughput is roughly 2,000 bit/s before
    accounting for measured FER and Wilson-bound-required margin -- well
    above the 500 bit/s floor, leaving room to trade some of it back for
    reliability (more pilots, more symbols/frame for coding gain, or a
    lower-rate code on a per-carrier basis) if stage 3/4's screen shows the
    coherent-16-QAM design needs it to clear +5 dB quiet / +10 dB moderate.
- This is a starting point for the screen, not a frozen constant: symbol
  count, pilot count, and constellation order are exactly what stage 4 of
  PLAN.md is allowed to retune against measured FER/acquisition before the
  design is considered final.

## Implementation note (stage 2, `hf2.py`)

The 40-symbol starting point above does not divide into a whole number of
packet bytes: at 60 raw bits/symbol, 40 symbols is 1,200 information bits
after rate-1/2 coding, minus the code's 6 tail bits leaves 1,194, and
1,194 / 8 is not an integer -- the same "no stranded bits" concern HC1's
docstring describes for its own grid. A rate-1/2 K=7 grid needs
`payload_symbols * 30 - 6` divisible by 8, i.e. `payload_symbols % 4 == 1`;
**41** is the nearest value satisfying that and is what `hf2.py` uses.

Concrete numbers that follow from 41 payload symbols:

- 4 comb pilots (bins 7, 13, 19, 25) + 15 16-QAM data carriers, 60 raw
  bits/payload symbol.
- Header: 4 identical sync symbols (acquisition) + 6 varying training
  symbols (initial per-carrier gain/offset fit and fine frequency
  estimate) = 10 header symbols. 10 + 41 = 51 total OFDM symbols,
  51 x 640 / 48000 = 0.68 s of core+guard keying (640 samples/symbol at
  48 kHz).
- Payload bits (coded, post-interleave): 41 * 60 = 2,460. Information
  bits: 1,230. Packet bytes: (1,230 - 6) / 8 = 153, with 0 stranded bits.
  Max payload: 153 - 2 (length) - 4 (CRC32) = **147 bytes**.
- Nominal-rate estimate (payload bytes / core+guard OFDM keying time,
  ignoring lead-in/tail and any measured FER): 147 * 8 / 0.68 s ~
  1,730 bit/s; including the measured 6,144-sample lead-in and
  960-sample tail (0.828 s total keying) it is ~1,420 bit/s -- both
  comfortably above the 500 bit/s floor, leaving room to trade throughput
  for reliability in stage 3/4 if the Watterson screen needs it (see the
  "Stretch" row of PLAN.md's falsifiable target).

The comb-pilot-to-data-carrier correction (`whale.dsp.equalize` ships
`fit_header`, a per-carrier fit against a *training block*, and
`pilot_phase`, a *time*-axis phase track from periodic full-pilot symbols
-- neither is a frequency-axis interpolator from a handful of pilot bins
each symbol, which is what the comb-pilot layout above needs) is
implemented as small HF2-local glue in `hf2.py`'s `_pilot_correction`:
per payload symbol, the observed/expected ratio at the 4 pilot bins is
linearly interpolated (real and imaginary parts independently) across the
carrier axis to the 15 data bins. This sits on top of `fit_header`'s
initial per-carrier gain/offset rather than replacing it.

Everything else -- the header/acquisition wiring, frequency and timing
recovery, the payload codec -- is used exactly as designed above, with no
further deviation. `experiments/hf2/test_hf2.py`'s deterministic round
trip (all-zero, all-0xFF, pseudo-random and near-limit payloads, over a
clean/no-channel signal) passes with `synced=True` and the exact payload
recovered in every case.

## Stage 4 note (2026-08-31/09-01): root cause of the Watterson FER floor

Stage 3's coarse screen found AWGN clean (30/30 acquisition, decode trending
to 30/30 by 5 dB) but both Watterson presets flat and broken: FER stuck at
25-100% from 3 dB out to 20 dB in `mid_latitude_moderate`, not improving
with SNR -- the signature of a channel-tracking bug, not a margin shortfall.

**Root cause, confirmed by instrumentation (comparing the per-symbol
equalized constellation against the known transmitted values on individual
trials).** The original `_pilot_correction` computed, per payload symbol, the
comb pilots' *observed/expected ratio relative to `fit_header`'s single,
static per-carrier gain estimate*, then linearly interpolated that ratio
(real/imag independently) to the data carriers with `np.interp`, forced
through all 4 pilot points exactly. Two compounding problems:

1. `fit_header`'s gain is fit once from the 10-symbol (~130 ms) header
   block. Watterson fading moves a given carrier's true gain several-fold
   over a 41/43-symbol (~550-700 ms) frame (confirmed: one carrier's
   header-equalized magnitude drifted from ~2x to ~9x within a single
   frame, its neighboring pilot's ratio from ~3x to ~18x in lockstep).
   Computing a "correction" as a ratio *relative to* that stale header
   estimate chains two different times' channel reads multiplicatively and
   amplifies their mismatch instead of removing it.
2. `np.interp` was pinned exactly through every pilot's ratio, including a
   pilot that happened to sit in a header-time fade (observed header SNR as
   low as -8 dB on some trials) -- an inherently noisy point that then
   dragged every data carrier interpolated near it.

**Fix, in three parts, all in `hf2.py`:**

1. `_pilot_channel_estimate` (replacing `_pilot_correction`) computes each
   pilot's complex gain *fresh*, directly from that payload symbol's raw
   carrier value (`(raw - header_offset) / known_pilot_value`) -- a single
   division at the time it matters, not a ratio of two different times --
   then interpolates that across the carrier axis with plain unweighted
   `np.interp`. (A weighted-least-squares variant, and a variant that
   dropped/downweighted pilots by header-time SNR, were both tried first
   and both measured *worse* against a noiseless 40 dB genie reference than
   plain per-symbol-fresh interpolation -- weighting by instantaneous
   pilot magnitude biases the fit toward strong pilots' positions, which is
   the wrong asymmetry when carriers differ in gain by design.)
2. The pilot comb was widened twice against measured residual error: 4
   pilots (6-bin spacing) to 6 (3.5-bin) to the shipped 8 pilots / 11 data
   carriers (~2.4-bin spacing), trading raw bits/symbol (60 to 52 to 44)
   for a smaller interpolation gap. Even so, a noiseless-genie residual did
   not reach zero (measured ~0.18-0.27 RMS against a unit-energy
   constellation even with pilots on every other carrier): moderate
   Watterson's 1 ms delay spread produces frequency-response nulls whose
   phase can turn within a single 93.75 Hz carrier bin, which no
   piecewise-linear interpolation at any pilot density fully inverts.
3. `demodulate`'s soft-bit weighting was changed from a single per-carrier
   weight off the header block (`_eq.carrier_weights(channel.snr_db[...])`)
   to a fresh per-(symbol, carrier) weight from the same per-symbol pilot
   gain used to equalize (`|gain|^2`, clipped to
   `[0.05, 2.0]` of the per-symbol median). This is the answer to point 2
   above: rather than expecting the equalizer to perfectly invert a null
   the pilot density cannot resolve, the FEC is told, symbol by symbol,
   carrier by carrier, exactly when to discount a reading. This produced by
   far the largest single improvement of the three changes.

`PAYLOAD_SYMBOLS` moved from 41 to 45 (with 11 data carriers, the smallest
value at/above the 40-symbol starting point leaving whole packet bytes:
`payload_symbols * 22 - 6` divisible by 8). `MAX_PAYLOAD_BYTES` is now
**117** (was 147); nominal throughput is correspondingly lower but still
comfortably clears the 500 bit/s floor (measured ~900-1060 bit/s across the
screen below). A frame-length doubling (89 payload symbols, for FEC/time
diversity) was also tried and measured no material FER improvement over 45
symbols at roughly double the airtime, so it was not kept.

**Result: large improvement, gate not yet cleared.** Before/after at 30
trials/point (seed 20260901 vs the original 20260831; `errors=0` in both):

| Point | Before: decoded/30 (FER Wilson-UB) | After: decoded/30 (FER Wilson-UB) |
| --- | ---: | ---: |
| quiet +5 dB (required boundary) | ~19-24/30 (>0.4, flat) | 26/30 (0.297) |
| quiet +8 dB | ~19-24/30 (>0.4, flat) | 30/30 (0.114) |
| quiet +10 dB | ~19-24/30 (>0.4, flat) | 30/30 (0.114) |
| quiet +15 dB | ~19-24/30 (>0.4, flat) | 29/30 (0.167) |
| moderate +10 dB (required boundary) | ~8-23/30 (>0.4, flat) | 25/30 (0.336) |
| moderate +15 dB | ~8-23/30 (>0.4, flat) | 27/30 (0.256) |
| moderate +20 dB | ~8-23/30 (>0.4, flat) | 28/30 (0.213) |

(Before-column figures are read off `results/mid_latitude_quiet_screen_20260831.json`
and `results/mid_latitude_moderate_screen_20260831.json`; after-column off
`results/mid_latitude_quiet_screen_20260901.json` and
`results/mid_latitude_moderate_screen_20260901.json`.)

The flat, SNR-independent failure signature from stage 3 is gone -- FER now
clearly trends down with SNR and stays there, confirming the fix addressed
a real tracking bug rather than papering over a margin shortfall. But the
Wilson-UB does not reach the required <=0.10 at either boundary point, and
plateaus around 0.21-0.34 even at 15-20 dB well above the required
envelope -- both presets, not just at the boundary. Per-trial instrumentation
of the remaining failures shows a consistent pattern: a specific carrier (or
small handful) sitting in a deep, persistent fade for that trial's entire
frame (observed carrier SNR as low as -1 to +3 dB against neighbors at
15-25 dB, unmoving across the ~700 ms frame because Watterson Doppler here
is slow, 0.1-0.5 Hz). This is not a tracking or interpolation defect --
genie (exact-channel) equalization on such a trial still leaves that
carrier's bits effectively erased -- it is a structural property of a
19-carrier, 1.7 kHz-wide OFDM design over this delay spread: some fraction
of channel realizations will always put a notch on a carrier and hold it
there for the whole frame, and the current rate-1/2 K=7 code with soft-bit
erasure-style weighting is not quite redundant enough to always recover
from losing ~1/11 to ~2/11 data carriers outright for an entire frame.

**What real further work would look like** (not attempted here, budget
exhausted for this stage): a lower code rate or added outer redundancy
sized specifically for "1-2 of 11 carriers erased for the whole frame"
rather than average per-bit noise; true frequency diversity (e.g.
duplicating the most failure-prone bits onto multiple carriers, or a wider
occupied band trading against the 2.3 kHz ceiling) so a single notch cannot
take out the same information twice; or accepting a narrower envelope claim
than the current +5 dB quiet / +10 dB moderate targets and re-measuring
where this design's boundary actually falls. Doubling the frame length for
added FEC block gain was tried and did not help (see above), which argues
for redundancy/diversity aimed at the erasure pattern specifically, not
generic longer coding.

## Stage 4b note (2026-08-31/09-03): frequency diversity clears the gate

**Interleaving hypothesis, checked directly and refuted.** Before changing
anything, the multiplicative interleaver (`whale.dsp.interleave.multiplicative`,
stride 937, size 1980) was instrumented directly: for a fixed data-carrier
index, the 45 trellis (pre-interleave, encoder-order) bit positions its four
per-symbol bits land on across the whole 1980-bit codeword were computed
exactly (`permutation[i]` for `i = s*44 + carrier_offset`, `s = 0..44`).
Result: the gaps between consecutive trellis positions for one fixed
carrier are **all exactly 44**, out of 1980 -- perfectly even coverage of
the whole codeword, not a cluster. So a carrier dead for the entire frame
does not produce a burst error in trellis time; it produces a low-level,
perfectly periodic erasure spread over the *entire* codeword. The
interleaver was already doing its job. This means the stage-4a plateau
(Wilson-UB FER 0.21-0.34 even at 15-20 dB, both presets) was a redundancy
problem, not a clustering/interleaving problem: with 11 data carriers, one
carrier out for the whole ~700 ms frame is 9% of ALL codeword bits
uninformative for the whole codeword, regardless of how evenly they are
spread, and the rate-1/2 K=7 code's erasure-correcting margin was not
enough to always absorb that on top of ordinary AWGN/residual-fade noise on
the other carriers.

**Fix: physical frequency diversity.** Rather than lower the code rate
(would need modifying the shared `whale.dsp.fec.ConvolutionalCode`, used
unmodified by every OFDM mode in the repo) or widen the occupied band
(risks the 2.3 kHz ceiling and the delay-spread margin DESIGN.md already
reasoned through), the 11 physical data carriers were regrouped into 5
*logical* carriers, each backed by 2-3 physical carriers chosen from
opposite halves of the band (`DATA_GROUPS` in `hf2.py`): group 0 is bins
8/24/16 (triple), the other four are low/high pairs (9/23, 11/21, 13/19,
14/18) -- every pair spans roughly 700-1500 Hz, comparable to or larger
than the Watterson notch widths observed in the stage-4a instrumentation.
The same 16-QAM value is transmitted on every physical carrier in a group.
At the receiver, each physical carrier's already-computed, per-symbol
pilot-reliability-weighted soft-bit LLRs (`demodulate`'s `data_weight`
mechanism, unchanged from stage 4a) are simply **summed** across a group's
physical carriers before Viterbi decoding: a carrier in a notch contributes
a small, near-zero-weighted LLR at that instant, so the sum is dominated by
whichever group member is not simultaneously faded -- which a spatially
local Watterson notch makes unlikely for carriers 700+ Hz apart. This is
plain diversity combining, not a new mechanism; no other part of the
pilot/equalization/interleaving/FEC pipeline changed.

**Cost.** The coded frame is unchanged (990 information bits, 1980 coded
bits, `MAX_PAYLOAD_BYTES` still **117**) -- diversity only changes how those
1980 bits are placed on the air, not how many there are. But 5 logical
carriers x 4 bits = 20 raw bits/symbol (was 44), so `PAYLOAD_SYMBOLS` grew
from 45 to **99** (`TOTAL_SYMBOLS` 55 to **109**) to carry the same coded
frame. `frame_seconds()` grew from ~0.75 s to **1.601 s**; nominal
throughput (`MAX_PAYLOAD_BYTES * 8 / frame_seconds`) dropped from ~1250 to
**~585 bit/s** -- still above the 500 bit/s floor, but with much less
margin than stage 4a had. This is the "spend margin on redundancy instead
of throughput" trade PLAN.md's stretch-goal section anticipated might be
necessary.

**Screen results.** 30 trials/point (seed 20260902) at the same point grid
as the stage-4a screen, both presets:

| Point | Acquire | Decoded (FER Wilson-UB) |
| --- | ---: | ---: |
| quiet -2 dB | 27/30 | 26/30 (0.297) |
| quiet 0 dB | 30/30 | 30/30 (0.114) |
| quiet 3 dB | 30/30 | 30/30 (0.114) |
| quiet +5 dB (boundary) | 30/30 | 30/30 (0.114) |
| quiet +8 dB | 30/30 | 30/30 (0.114) |
| quiet +10 dB | 30/30 | 30/30 (0.114) |
| quiet +15 dB | 30/30 | 30/30 (0.114) |
| moderate +3 dB | 30/30 | 30/30 (0.114) |
| moderate +5 dB | 30/30 | 29/30 (0.167) |
| moderate +8 dB | 30/30 | 30/30 (0.114) |
| moderate +10 dB (boundary) | 30/30 | 29/30 (0.167) |
| moderate +13 dB | 30/30 | 29/30 (0.167) |
| moderate +15 dB | 30/30 | 29/30 (0.167) |
| moderate +20 dB | 30/30 | 29/30 (0.167) |

(`results/mid_latitude_quiet_screen_20260902.json`,
`results/mid_latitude_moderate_screen_20260902.json`, `errors=0`
throughout.) At n=30, a 30/30 clean run's own Wilson-UB floor is 0.114,
above the required <=0.10 -- so this screen cannot itself prove the gate
even at a perfect result; it only rules out a design that is still visibly
struggling (stage 4a's 0.21-0.34 plateau is gone). Per PLAN.md's step 3,
this triggered a 100-trial confirmation at both boundary points plus one
neighbor on each side:

| Point | Acquire (Wilson-LB) | Decoded (FER Wilson-UB) | Useful bit/s |
| --- | ---: | ---: | ---: |
| quiet +3 dB | 100/100 (0.963) | 100/100 (0.037) | 585 |
| quiet +5 dB (boundary) | 100/100 (0.963) | 100/100 (0.037) | 585 |
| quiet +8 dB | 100/100 (0.963) | 100/100 (0.037) | 585 |
| moderate +8 dB | 99/100 (0.946) | 98/100 (0.070) | 573 |
| moderate +10 dB (boundary) | 100/100 (0.963) | 99/100 (0.054) | 579 |
| moderate +13 dB | 100/100 (0.963) | 99/100 (0.054) | 579 |

(`results/mid_latitude_quiet_confirm100_20260903.json`,
`results/mid_latitude_moderate_confirm100_20260903.json`, `errors=0`
throughout, seed 20260903.) Both required boundary points and both
neighbors on each side clear the gate: Wilson-UB FER <= 0.10 (0.037-0.070,
comfortably under) and Wilson-LB acquisition >= 0.90 (0.946-0.963) at every
point tested, at both presets.

**Status.** This is a 100-trial result, explicitly *not* the >=300-trial
confirmed-boundary gate PLAN.md's stage 4 reserves as a separate later
step -- that run was not attempted here per the task's scope. Given how far
under the 0.10 ceiling the 100-trial Wilson-UB sits (0.037-0.070, versus a
0.054-0.070 floor that is already most of the way there), and that
useful throughput (565-585 bit/s measured, accounting for the observed FER)
stays above the 500 bit/s floor at every point checked, the design looks
ready for that >=300-trial confirmation. The one caution: throughput
margin above the 500 bit/s floor is much thinner than stage 4a's (was
~900-1060 bit/s useful; is now ~565-585 bit/s useful) -- there is no more
room to trade throughput for reliability without another design pass if
the 300-trial run finds a problem stage 4a's larger margin would have
absorbed.

## What this experiment does not attempt

- Disturbed Watterson fading, low/high-latitude presets, or any envelope
  beyond Level 2's quiet+5dB/moderate+10dB contract.
- Hardware, radio, or full-session/ARQ evidence -- no radios are available;
  see PLAN.md's status section.
- Any claim of parity or comparison with HC0, HC1, VF6, HR0, or VARA.
