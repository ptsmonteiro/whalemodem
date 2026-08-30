# HC2 — differential 8-PSK, K=9: results and dead-end

HC2 (`experiments/hc2/hc2.py`) is a candidate fast HF-SSB rung above HC1,
reusing HC1's exact OFDM geometry (19 carriers, 93.75 Hz spacing,
656.25-2343.75 Hz, 512-sample core / 128-sample cyclic prefix, 47-symbol /
0.695 s frame) but switching the payload modulation from differential QPSK
(2 bits/carrier/symbol) to Gray-coded differential 8-PSK (3
bits/carrier/symbol), and the inner code from rate-1/2 K=7 (171,133) to
rate-1/2 K=9 (561,753) (`whale/dsp/fec.K9`) to buy back some of 8-PSK's SNR
penalty. Same frame duration as HC1 by construction, so the payload
capacity ratio (114 B vs 74 B, +54%) is a clean measurement of what the
modulation and code choice alone are worth.

**Bottom line: HC2 is not a usable replacement or complement for HC1.** It
wins throughput in AWGN and CCIR-Good/quiet conditions, at a real but small
robustness cost, and then loses badly -- worse than HC1 by 30-50 points of
absolute frame delivery -- under CCIR-Moderate and CCIR-Poor/disturbed
conditions, which is precisely the regime the HC0/HC1 campaign
(`logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-30/INDEX.md`) flagged as
HC1's own weak point. This is retained as a design lesson, not shipped
evidence: see "Why it failed" below for the mechanism, which is
predictable in hindsight and should inform the next attempt.

## Benchmark method

Same Monte Carlo frame-sweep methodology `scripts/benchmark_simulated_channels.py`
uses (`whale.qualification.run_frame_trial`, the same Wilson-95% summary),
run via `experiments/hc2/benchmark_hc2.py` because HC2 has no on-air mode ID
and is not in any `ModeRegistry` (see that script's docstring for why it
cannot simply reuse the CLI). 100 trials per point, full-capacity payloads,
waveform SNR -5/0/5/10/15/20 dB, AWGN and all three mid-latitude Watterson
presets. Run against a dirty tree (this change itself) on 2026-08-30; raw
results retained under
`logs/mode_qualification/hf-ssb/hc2-experimental/2026-08-30/` with
provisional/experimental framing -- **this is not a qualification result**,
per `MODE_QUALIFICATION.md`; HC2 has no hardware evidence and is not in the
manifest.

## Results

Frame delivery rate (decoded/100), HC1's own retained numbers from
`logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-30/` alongside:

| Channel | SNR | HC1 | HC2 |
|---|---:|---:|---:|
| AWGN | 0 dB | (not swept for HC1) | 79 |
| AWGN | 5-20 dB | (not swept for HC1) | 100 |
| quiet | -5 dB | 2 | 0 |
| quiet | 0 dB | 99 | 30 |
| quiet | 5-20 dB | 100 | 92-97 |
| moderate | -5 dB | 1 | 0 |
| moderate | 0 dB | 66 | 9 |
| moderate | 5 dB | 89 | 47 |
| moderate | 10 dB | 91 | 59 |
| moderate | 15 dB | 96 | 61 |
| moderate | 20 dB | 93 | 51 |
| disturbed | -5 dB | 0 | 0 |
| disturbed | 0 dB | 19 | 0 |
| disturbed | 5 dB | 34 | 14 |
| disturbed | 10 dB | 50 | 11 |
| disturbed | 15 dB | 43 | 25 |
| disturbed | 20 dB | 14 | 14 |

Acquisition (header found) tracks HC1 closely at every point -- HC2's
failures are payload/CRC losses after a successful lock, exactly like HC1's
own disturbed-preset failures, not an acquisition regression.

Throughput, where both modes clear their own gate: HC1's clean AWGN-like
regime (quiet, >=5 dB) delivers 74 B / 0.695 s = 852 bit/s; HC2's does 114 B
/ 0.695 s = 1,314 bit/s, +54%, but with 3-8 points less absolute delivery at
every point in that regime (steady-state around 92-97% vs HC1's 100%) --
the AWGN/quiet win is real but not free even there.

## Decode CPU cost

Measured on this development host (Intel Core-class x86_64, single thread,
no channel impairment, warm caches) -- a proxy, not a Pi-class measurement,
but the ratio between HC1 and HC2 is what a Pi-class budget check needs:

| Mode | Decode time / frame |
|---|---:|
| HC1 (K=7, 64 states) | 14.4 ms |
| HC2 (K=9, 256 states) | 20.4 ms |

Only 1.4x, not the 4x the state-count ratio alone would suggest -- Viterbi
is a minority of total decode time next to the FFT-based acquisition search,
OFDM carrier-bank extraction and channel fit, all of which are unchanged
from HC1 and scale with carrier/symbol count, not code state count. Both
figures are far under the 695 ms a frame occupies on air, leaving ample
margin for a Raspberry Pi-class core, which this benchmark host is not --
a real Pi measurement is listed under "What's still needed" below.

## Why it failed: the aggressive-order trap

The HC0/HC1 investigation's diagnosis was specific: HC1's carrier spacing
is comparable to the disturbed preset's coherence bandwidth, so several
*adjacent* carriers fade together for the life of a frame (the fade's
correlation time, set by the 1.0 Hz Doppler spread, is comparable to or
longer than the 0.695 s frame itself -- interleaving in time cannot average
out a fade that does not change during the frame). HC2 does nothing to
change that coherence bandwidth -- it reuses HC1's exact carrier geometry --
so the same carriers still fade together. What HC2 changes is how much a
lost carrier costs: at 3 bits/carrier/symbol instead of 2, every carrier
lost to a correlated fade now costs the Viterbi decoder 50% more coded bits
per lost symbol, and K=9's ~0.6-1 dB of extra coding gain is nowhere near
enough to pay for that. The result is legible in the numbers: HC2's
disturbed-preset ceiling (never above 25/100) is worse relative to HC1's
own ceiling (never above 61/100) than the AWGN-regime comparison would
predict, and unlike HC1's *nearly monotonic-with-SNR* moderate-preset
climb, HC2's moderate-preset numbers peak at 15 dB (61/100) and *fall* at
20 dB (51/100) -- consistent with a fixed, SNR-independent structural loss
(a correlated fade) dominating over thermal-noise BER once the AWGN
component stops being the limiting factor, the same signature the HC1
disturbed-preset investigation used to rule out a boundary/acquisition bug.

**The lesson for the next attempt**: on a channel this fade-correlated, a
faster HF rung needs to spend its margin on *surviving the fade*, not on
raising the noise floor it needs. That means one of:

- **Frequency diversity below the fade**, not above it: repeat each
  information bit (or each coded bit, pre-interleave) across two carriers
  spaced further apart than the coherence bandwidth, at the cost of raw
  rate -- effectively a rate-1/4 code in the correlated-fade case while
  staying rate-1/2 in AWGN, which needs a soft combiner rather than a
  simple repeat.
- **An outer burst code** (Reed-Solomon over the interleaved coded stream,
  as VF5 already does for a different reason -- see
  `experiments/vf5/vf5.py`) sized to the *number of carriers* a Watterson
  disturbed fade is expected to take out at once, not to a generic
  bit-error rate. This is the standard concatenated-code answer to a
  burst/fading channel and was flagged as worth investigating in this
  task's own brief; it was not attempted here for time, and is the most
  promising next step.
- **Not raising modulation order at all**: a same-modulation, same-code,
  more-payload-symbols HC2 (spend the extra airtime on more QPSK symbols
  rather than more bits per symbol) would not have made a lost carrier any
  more expensive, and is a cheaper next experiment than either of the above
  if the goal is simply "more payload bytes without the failure mode this
  run found."

## What's still needed before any of this enters qualification

- A real Raspberry-Pi-class CPU measurement, not the ratio-only estimate
  above.
- A design that survives moderate/disturbed conditions at least as well as
  HC1 -- HC2 as built here does not clear that bar and should not replace
  HC1 in any ladder.
- Hardware evidence: everything above is simulated-channel only, per
  `MODE_QUALIFICATION.md`'s evidence gates. No frame in this results file
  has been on real air.
- If a burst-code or diversity redesign is pursued, mode-ID allocation
  (`whale/mode_qualification.py`'s MANIFEST; next free HF-SSB ID is 6) and
  promotion from `experiments/hc2/` to `whale/modes/` following the
  `hc1.py`/`hc1_mode.py` pattern, only after the above.
