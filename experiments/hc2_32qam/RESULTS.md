# HC2 milestone 3: AWGN SNR threshold and EVM health metric

## Conclusion

HC2's existing receiver delivers full-capacity 2,749-byte frames over AWGN
from about **12 dB waveform SNR** upward, and its realized net payload rate
beats the 7,050 bit/s VARA HF 32QAM reference row from **12.5 dB** upward.
Against the three criteria worth stating separately, over 7,800 pooled
frame trials:

| Criterion | Lowest qualifying point | Evidence |
| --- | ---: | --- |
| `MODE_QUALIFICATION.md` section 3 gate (Wilson 95% **upper** bound on FER at most 10%) | **12.0 dB** | 375/400, FER 6.3% [4.3%, 9.1%] |
| FER at most 1e-2 by the same upper bound | **16.0 dB** | 1,099/1,100, FER 0.09% [0.02%, 0.51%] |
| No failure observed at all | **20.0 dB** | 1,100/1,100, FER 0% [0%, 0.35%] |

The waterfall is sharp, as expected for rate-3/4 32QAM: FER falls from 0.88
at 9.5 dB to 0.11 at 11.5 dB, a 2 dB knee. Below 9 dB the
mode does not work at all.

Two findings dominate everything else.

**The residual FER above 11.5 dB is not a noise limit. It is a single
acquisition bug.** Every one of the 84 failures at 12.5 dB and above is the
receiver locking onto the *second* of HC2's two identical training symbols,
recorded as `start_error_samples == 1152` (exactly one OFDM symbol). All 54
such frames at 13 and 14 dB decode exactly when the frame start is forced to
the true offset. `demodulate` selects the earliest correlation lag within
0.5% of the peak; at 14 dB the true peak measures 99.36-99.44% of the second
one, just outside that window. This costs 4.1% FER at 13 dB and 1.5% at
14 dB, and it is why the 1e-2 criterion needs 16 dB rather than roughly 12.5.
Per the milestone constraint the receiver was **not** modified; see
"Recommendations" below.

**Decision-directed EVM is a clean in-frame health metric.** In none of the
7,800 trials did a frame below **9.91%** post-equalization EVM fail, and no
frame above 12.83% ever decoded. A fallback trigger at **EVM > 10%** admits 5,484
frames of which 5,482 decode (P(decode | accept) = 0.9996). The catastrophic
mis-acquisition case is trivially separable: those frames measure 91% to
1,915% EVM.

This is a benign-channel screen of a top-rung candidate, not qualification
evidence. AWGN says nothing about the frequency-selective fading that is the
actual reason HC1 fails the disturbed Watterson preset; milestone 4 covers
that.

## Method

### Waveform and receiver

Unmodified `experiments/hc2_32qam/hc2_32qam.py`: 49 carriers, 1,024-sample
FFT at 48 kHz, 128-sample cyclic prefix, 41.667 symbol/s, coherent
rectangular 32QAM, punctured K=7 rate-3/4 FEC, 2 training + 120 payload
symbols, 2.928 s keyed, 2,749-byte maximum payload, 7,510.93 bit/s sustained
full-frame user rate. Every trial used the full 2,749-byte capacity.

The receiver under test is `demodulate` as it stands after milestone 2:
matched-filter acquisition over +/-20 Hz in 1 Hz steps, training-pair CFO
refinement, per-carrier complex equalization from the two training symbols,
and decision-directed common-phase tracking, with hard 32QAM slicing before
the soft Viterbi decoder.

### Capture and SNR reference

Each trial builds a realistic capture rather than an exactly-`FRAME_SAMPLES`
buffer: 12,000 samples (250 ms) of silence, the frame, then 12,000 samples of
silence, all passed through **one** `AwgnChannel` instance. The padding
therefore reaches the receiver as noise-only audio for acquisition to search.

SNR is `SnrKind.WAVEFORM` with `reference_start`/`reference_stop` set to the
signal-bearing span only, so the quoted figure is frame power over
full-Nyquist-band (0-24 kHz) noise power and does not move when the padding
length changes. Two conversions, for readers comparing against other
references:

- in 32QAM's 2,296.875 Hz occupied bandwidth, add
  `10*log10(24000/2296.875) = 10.19 dB`;
- as Eb/N0 against the 7,656.25 bit/s coded information rate, add
  `10*log10(24000/7656.25) = 4.96 dB`.

So 12 dB waveform SNR is 22.2 dB in 2.3 kHz, or Eb/N0 17.0 dB; 16 dB is
26.2 dB and 21.0 dB respectively.

### Trials and seeding

Payloads are random per trial. Every `(point index, trial)` gets a
deterministic derived seed from `whale.qualification.trial_seed(master_seed,
32999, point_index, trial)`, which seeds both the payload and the channel.
32999 is not a registry mode ID -- HC2 has none -- it only namespaces the
seed sequence away from every registered mode's campaign. Delivery is scored
by comparing recovered bytes to the transmitted payload, never by a non-`None`
return.

Because `trial_seed` does not take the SNR, two runs that give the same SNR
different point indices are independent; runs that share a point index at
different SNRs are paired (same payload, same noise draws, rescaled). No
`(SNR, point index)` pair is repeated in this campaign, so the per-SNR pools
below are pools of independent trials.

### EVM measurement

`demodulate` exposes no constellation, and the milestone forbids changing it,
so `benchmark_hc2_snr.frame_metrics` recomputes the payload constellation
outside the receiver from the receiver's own `start_sample` and
`frequency_offset_hz` diagnostics, repeating its equalization and
decision-directed phase track step for step. A test asserts the replica
re-derives the receiver's exact decode, and 54 replayed failures agreed.

Two figures are recorded per frame:

- **decision-directed EVM** -- error against the receiver's own hard
  decisions. This is what a real receiver can compute in-frame with no
  knowledge of the payload, and it is the figure quoted everywhere below.
- **true (data-aided) EVM** -- error against the transmitted constellation.
  An oracle reference only.

They agree within 1% relative above 12 dB and diverge below it (at 0 dB the
decision-directed figure reads 32% against a true 57%), because slicing
errors drag the reference toward the received point. A trigger built on the
decision-directed figure is therefore **optimistic** at low SNR, which is
acceptable here only because the useful threshold sits at 10%, where the two
still agree closely.

The noise-free implementation floor is **1.81% EVM**, and it is a receiver
property, not a channel one: the oracle real-FFT path measures ~1e-6%, while
the `scipy.signal.hilbert` analytic front end used by `demodulate` leaks
across the per-symbol FFT. It is far below the useful threshold and does not
affect any conclusion here.

### Commands

Five runs, all with master seed 20260830, all writing to gitignored scratch:

```sh
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 100 \
  --points 0 2 4 6 8 9 10 11 12 13 14 16 20 25 \
  --out logs/scratch/hc2_awgn_coarse.json
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 300 \
  --points 9.5 10 10.5 11 11.5 12 12.5 13 \
  --out logs/scratch/hc2_awgn_knee.json
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 1000 --points 13 14 \
  --out logs/scratch/hc2_awgn_operating.json
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 1000 --points 16 \
  --out logs/scratch/hc2_awgn_high16.json
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 1000 --points 20 \
  --out logs/scratch/hc2_awgn_high20.json
```

7,800 frame trials, about 2.5 CPU-hours on an 8-core 2026 development host,
Python 3.11.15 / NumPy 2.4.6 / SciPy 1.17.1. The tree was dirty and the HC2
experiment package untracked at commit `8bcf5b6`, so these are scratch
results by `LOGS.md`'s rules and are cited by command, not by path.

## Results

### FER against waveform SNR

Pooled across the runs above. "Mis-acq" counts frames whose acquired start
was one full OFDM symbol late. "Other" is every remaining failure: at 12.5 dB
and above there are none, and below the knee it mixes noise-limited payload
failures with coarse-frequency acquisition errors (a correct start with a
double-digit Hz CFO estimate).

| Waveform SNR (dB) | Trials | Delivered | FER | FER Wilson 95% | Mis-acq | Other | Realized bit/s |
| ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| 0 | 100 | 0 | 1.000 | [0.963, 1.000] | 48 | 52 | 0 |
| 2 | 100 | 0 | 1.000 | [0.963, 1.000] | 44 | 56 | 0 |
| 4 | 100 | 0 | 1.000 | [0.963, 1.000] | 34 | 66 | 0 |
| 6 | 100 | 0 | 1.000 | [0.963, 1.000] | 44 | 56 | 0 |
| 8 | 100 | 0 | 1.000 | [0.963, 1.000] | 32 | 68 | 0 |
| 9 | 100 | 1 | 0.990 | [0.946, 0.998] | 17 | 82 | 75 |
| 9.5 | 300 | 36 | 0.880 | [0.838, 0.912] | 64 | 200 | 901 |
| 10 | 400 | 151 | 0.623 | [0.574, 0.669] | 57 | 192 | 2,835 |
| 10.5 | 300 | 194 | 0.353 | [0.301, 0.409] | 42 | 64 | 4,857 |
| 11 | 400 | 297 | 0.258 | [0.217, 0.303] | 57 | 46 | 5,577 |
| 11.5 | 300 | 267 | 0.110 | [0.079, 0.151] | 26 | 7 | 6,685 |
| **12** | 400 | 375 | 0.063 | [0.043, 0.091] | 23 | 2 | 7,041 |
| 12.5 | 300 | 291 | 0.030 | [0.016, 0.056] | 9 | 0 | 7,286 |
| 13 | 1,400 | 1,342 | 0.041 | [0.032, 0.053] | 58 | 0 | 7,200 |
| 14 | 1,100 | 1,084 | 0.015 | [0.009, 0.024] | 16 | 0 | 7,402 |
| **16** | 1,100 | 1,099 | 0.0009 | [0.0002, 0.0051] | 1 | 0 | 7,504 |
| **20** | 1,100 | 1,100 | 0.000 | [0.000, 0.0035] | 0 | 0 | 7,511 |
| 25 | 100 | 100 | 0.000 | [0.000, 0.037] | 0 | 0 | 7,511 |

Realized bit/s is `7,510.93 * delivered / trials`, matching how HC2b and
HC2c report observed goodput. Like the nominal figure it excludes acquisition
preamble, PTT and turnaround, ARQ, and link air headers, so it is a
frame-layer rate, not application throughput.

The noise-limited failure population is gone by 12.5 dB. From there upward
FER is entirely the acquisition tie-break, which is why the curve flattens
instead of continuing to fall steeply.

### The one-symbol mis-acquisition

`demodulate` correlates against one training symbol, and HC2 transmits two
identical ones, so the correlation metric has two near-equal peaks 1,152
samples apart. The receiver resolves the ambiguity by taking the earliest lag
whose metric is at least 99.5% of the maximum. Replaying three failing 14 dB
trials:

| Trial | Best coarse offset | Peak metric | Metric at true start | Ratio | Chosen offset |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 47 | 0 Hz | 0.963278 | 0.957165 | 0.99365 | +1,152 |
| 188 | 0 Hz | 0.961511 | 0.956166 | 0.99444 | +1,152 |
| 212 | 0 Hz | 0.962135 | 0.956648 | 0.99430 | +1,152 |

The ratio sits a few parts per thousand under the 0.995 tolerance, so noise
decides which peak wins. Forcing the true start and re-deriving CFO from the
correct training pair recovers **38/38** failures at 13 dB and **16/16** at
14 dB, exactly.

### EVM against decode success

Pooled over all 7,800 trials, binned by decision-directed EVM:

| EVM bin (%) | Frames | Decoded | P(decode) |
| --- | ---: | ---: | ---: |
| 0-6 | 1,293 | 1,293 | 1.000 |
| 6-8 | 2,023 | 2,023 | 1.000 |
| 8-9 | 1,470 | 1,470 | 1.000 |
| 9-9.5 | 392 | 392 | 1.000 |
| 9.5-10 | 306 | 304 | 0.993 |
| 10-10.5 | 305 | 281 | 0.921 |
| 10.5-11 | 279 | 248 | 0.889 |
| 11-11.5 | 233 | 158 | 0.678 |
| 11.5-12 | 278 | 126 | 0.453 |
| 12-12.5 | 215 | 38 | 0.177 |
| 12.5-13 | 112 | 4 | 0.036 |
| above 13 | 894 | 0 | 0.000 |

Candidate thresholds, accepting a frame when EVM is at or below the value:

| Threshold | Accepted | P(decode &#124; accept) | Failing frames admitted | Good frames rejected |
| ---: | ---: | ---: | ---: | ---: |
| 9.5% | 5,178 | 1.00000 | 0 | 1,159 |
| 9.74% | 5,327 | 1.00000 | 0 | 1,010 |
| **10.0%** | 5,484 | 0.99964 | 2 | 855 |
| 11.0% | 6,068 | 0.99061 | 57 | 326 |
| 11.63% | 6,368 | 0.97535 | 157 | 126 |
| 12.0% | 6,579 | 0.95683 | 284 | 42 |

11.63% is the threshold with the best raw accuracy (96.4% of all 7,800 trials
classified correctly), but it admits 157 failing frames; 10% is the right
choice for a fallback trigger, which should be biased against admitting them.

The overlap region is **9.91% to 12.83%**: the worst decoding frame measured
12.83% and the best failing frame 9.91%, and 1,441 trials fall between them.
No single threshold can be right inside that band. It is not spread across
the whole sweep -- it occurs only at 9 to 12.5 dB, inside the waterfall.
Restricted to 12.5 dB and above, the two populations are completely disjoint:
decoded frames peak at 9.74% and the mis-acquired ones start at 90.98%.

**Recommended trigger: fall back when decision-directed EVM exceeds 10%.**
Justification: it admits no failing frame in 5,327 accepted trials at 9.74%
and only 2 in 5,484 at 10.0%; at 12 dB and above, where HC2 clears the
existing FER gate, every decoded frame measured at or below 10.40% and the
median was 9.54% or lower. A frame reading above 10% is by construction in
or below the waterfall, where FER exceeds 5%. EVM near 100% or higher is a
distinct signal -- mis-acquisition, not a channel verdict -- and a link should
retry rather than immediately demote the mode on it.

Median EVM by point, for calibration:

| SNR (dB) | 9 | 10 | 11 | 12 | 13 | 14 | 16 | 20 | 25 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Median EVM (%) | 12.93 | 11.80 | 10.64 | 9.54 | 8.59 | 7.70 | 6.21 | 4.13 | 2.73 |

## Honest limits

**Trial counts.** 1,000 trials with zero failures bounds FER only at
0.35% with 95% confidence. This campaign can establish "FER at or below 1e-2"
at 16 dB and "no failure observed" at 20 dB. It **cannot** establish a
1e-3 or 1e-4 frame error rate anywhere; that needs 10^4-10^5 trials per
point, roughly 3-30 CPU-hours each at the current 1 s/trial. The 100-trial
coarse points are screening resolution only -- the coarse run recorded
100/100 at 14 dB where 1,100 pooled trials show 1.5% FER, which is exactly
the kind of small-sample illusion the larger runs exist to correct.

**Hard-decision demapping penalty (estimate, not implemented).**
`demodulate` hard-slices 32QAM through `bits_from_qam32` before the soft
Viterbi decoder, discarding the reliability information Gray-mapped 32QAM
makes available. A scratch comparison -- same captures, same acquisition,
equalization and phase track, replacing only the slicer with a max-log LLR
demapper into the same `fec.K7.decode_soft` -- gave, at 100 trials per point:

| SNR (dB) | 8 | 9 | 10 | 11 | 12 | 13 | 14 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Hard (as shipped) | 0 | 3 | 40 | 87 | 93 | 97 | 98 |
| Soft LLR (scratch) | 58 | 72 | 84 | 93 | 95 | 97 | 97 |

The 50%-delivery point moves from about 10.2 dB to about 7.8 dB, so the
**hard-decision penalty is roughly 2.4 dB**, consistent with the 2 dB rule of
thumb. The gain shrinks to under 1 dB at the 90% point only because the
acquisition tie-break above is demapping-independent and caps both curves.
This measurement is a 100-trial scratch estimate and is **not** part of the
delivered harness; soft 32QAM metrics were explicitly out of scope for this
milestone.

**What AWGN does and does not predict.** It fixes the thermal-noise floor and
nothing else. HC2's 46.875 Hz carrier spacing is half HC1's, so it is *more*
exposed to frequency-selective fading, which is precisely the mechanism that
holds HC1 to 61/100 under the disturbed Watterson preset regardless of SNR.
Its 2.928 s frame is also nearly four times HC1's, so a given Doppler spread
has four times as long to decorrelate the channel from the two front-loaded
training symbols, which HC2 never refreshes. Neither effect appears anywhere
in this data. An AWGN threshold of 12 dB is a lower bound on the SNR any
real HF path will need, not a prediction of one.

**Scope.** One logical direction, no sample-clock offset, no CFO beyond the
+/-20 Hz search (all trials here were at zero offset), no radio, no ARQ, no
link. HC2 remains outside the qualification process entirely.

## Recommendations for future work

Ordered by measured value, none implemented here:

1. **Widen the acquisition peak tie-break, or stop making the two training
   symbols identical.** This is the single largest win available and is
   independent of the waveform: it is worth 4.1% FER at 13 dB and 1.5% at
   14 dB, all of it recoverable (54/54 replays). Either relax the 0.995
   tolerance to roughly 0.98 -- verifying that it does not start selecting
   spurious earlier lags at low SNR -- or make training symbol 2 a distinct
   known sequence so the correlation has one unambiguous peak. The second is
   preferable and costs no airtime. Re-run this sweep afterward; the 1e-2
   criterion should move from 16 dB down toward 12.5 dB.
2. **Soft 32QAM LLR demapping.** About 2.4 dB at the 50% point per the
   scratch estimate above. Worth doing after (1), since (1) currently masks
   most of its benefit at the top of the curve.
3. **Refresh the channel estimate mid-frame.** Not measurable on AWGN, but
   two training symbols at the head of a 2.928 s frame is the design most
   exposed to the fading milestone 4 will introduce. HC2c's pilot work is the
   obvious precedent.
4. **Replace the analytic front end's per-symbol leakage.** The 1.81% EVM
   floor is harmless at present but is a real 1.8% error budget the receiver
   spends before the channel does anything.

## Next experiment (milestone 4)

Repeat this sweep against `watterson` with the `mid_latitude_quiet` preset
first, at 12 to 25 dB, and establish HC2's benign-fading boundary before
touching moderate. Reuse the same seeding and EVM instrumentation so the
AWGN curve here is the control. Expect the boundary to be set by the 2.928 s
frame's channel-tracking span rather than by SNR, and be prepared for the
correct outcome to be a narrow declared envelope rather than a slower mode.
