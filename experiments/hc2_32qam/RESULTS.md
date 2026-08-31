# HC2 AWGN threshold, EVM health metric, and fading boundary

## Campaigns in this file

This file reports **two** AWGN campaigns against two different HC2 designs,
plus a fading campaign against the current one. The two AWGN campaigns are
not interchangeable and are never pooled:

| | Milestone-3 sweep (superseded) | Post-fix sweep (current) |
| --- | --- | --- |
| Waveform | two **identical** training symbols | two **distinct** training symbols |
| Acquisition | earliest lag within 0.5% of the correlation peak | plain matched-filter maximum |
| CFO refinement | phase of `sum(T[1] * conj(T[0]))` | same, after dividing out the known training values |
| Trials | 7,800 | 8,300 |
| Section reporting it | "Superseded results" below | "Results (AWGN)" below |

The milestone-3 sweep found that every failure above 12.5 dB was one
acquisition defect, and per its own milestone constraint it did not fix it.
The fix landed afterwards and the sweep was re-run. Everything in "Results
(AWGN)" describes the current receiver; everything in "Superseded results" describes
the milestone-3 one and is kept because it is the evidence that motivated the
change.

A third campaign, "Milestone 4: the Watterson fading boundary" at the end of
this file, runs the current receiver against fading rather than noise. It is
also never pooled with the AWGN results: AWGN fixes the thermal-noise floor
and fading fixes the operating envelope, and for HC2 those two numbers turn
out to have almost nothing to do with each other.

## Conclusion (AWGN, current receiver)

HC2 delivers full-capacity 2,749-byte frames over AWGN from about **11.5 dB
waveform SNR** upward. Against the three criteria worth stating separately,
over 8,300 pooled frame trials:

| Criterion | Lowest qualifying point | Was (milestone 3) | Evidence |
| --- | ---: | ---: | --- |
| `MODE_QUALIFICATION.md` section 3 gate (Wilson 95% **upper** bound on FER at most 10%) | **11.5 dB** | 12.0 dB | 288/300, FER 4.0% [2.3%, 6.9%] |
| FER at most 1e-2 by the same upper bound | **13.0 dB** | 16.0 dB | 1,400/1,400, FER 0% [0%, 0.27%] |
| No failure observed at all | **13.0 dB** | 20.0 dB | 1,400/1,400 |

Realized net payload beats the 7,050 bit/s VARA HF 32QAM reference row from
**11.5 dB** (7,210 bit/s), previously 12.5 dB.

12.5 dB just misses the 1e-2 criterion rather than clearing it: 1,294/1,300,
FER 0.46%, Wilson 95% upper bound **1.0033%**. The point estimate is under
1e-2 and the upper bound is three parts in ten thousand over it. Calling the
criterion at 13.0 dB is the conservative reading of the same data.

Three findings dominate.

**The one-symbol mis-acquisition is gone, not merely rarer.** Across all
8,300 trials there is not one `start_error_samples == 1152` frame, and not one
frame whose acquired start missed the true one by more than **1 sample** at
any SNR, 0 dB included. Under the identical-training receiver the same defect
consumed 48% of frames at 0 dB and still 1.5% at 14 dB. Every failure that
remains is a genuine noise-limited payload failure with a correct frame start
and a carrier-offset estimate inside 0.65 Hz.

**Removing the defect moved the whole curve, not just its tail.** FER at
12 dB falls from 6.3% to 0.75%, at 11.5 dB from 11.0% to 4.0%, and at 11 dB
from 25.8% to 13.8%. That is roughly 0.5 dB of apparent sensitivity through
the knee and about 3 dB at the 1e-2 criterion.

**Decision-directed EVM remains a usable in-frame health metric, but its
separation is now honest rather than flattering.** The milestone-3 data
separated cleanly above 12.5 dB only because the failures there were
mis-acquisitions reading 91% to 1,915% EVM -- a pathology, not a channel
verdict. With those gone, the surviving failures sit inside the normal EVM
range: the worst decoding frame measures 13.14% and the best failing frame
9.34%. A trigger at **EVM > 10%** still admits 6,025 frames of which 6,017
decode (P(decode | accept) = 0.9987).

This is a benign-channel screen of a top-rung candidate, not qualification
evidence. AWGN says nothing about the frequency-selective fading that is the
actual reason HC1 fails the disturbed Watterson preset; milestone 4 covers
that.

## The fix

### What was wrong

`demodulate` correlates the capture against a template of training symbol 1.
HC2 transmitted the *same* QPSK sequence as training symbol 2, so the metric
had two near-equal peaks 1,152 samples (one OFDM symbol) apart. The receiver
resolved the tie by taking the earliest lag scoring at least 99.5% of the
maximum. At 14 dB the true peak measured 99.36-99.44% of the false one, just
outside that window, so noise decided which peak won. A frame acquired one
symbol late loses its first payload symbol, estimates the channel from a
training symbol and a payload symbol, and never decodes.

### What changed

1. **Training symbol 2 is a different sequence.** `_TRAINING_SEEDS =
   (0x0C531, 0x00C3A)`. Symbol 1 keeps the original seed so the acquisition
   template is bit-identical to before. Seed `0x00C3A` was chosen by scanning
   order-17 LFSR seeds 1..4095 for a symbol that is simultaneously
   low-PAPR and unlike the template: it gives **7.39 dB** PAPR (2.50 dB
   *below* symbol 1's 9.89 dB) and a normalized matched-filter score of
   **3.4e-4 (-34.7 dB)** against the symbol-1 template, versus 1.0 for the old
   repeat. Both sequences are QPSK on all 49 carriers, so both stay
   constant-modulus and full-band and the per-carrier channel estimate stays
   equally well conditioned. The choice is a fixed seed, not a runtime draw.
2. **The 0.995 tie-break is deleted.** It existed only to arbitrate between
   two identical training symbols; with one unambiguous peak, taking the
   earliest lag within half a percent of the maximum can only bias the
   estimator toward earlier, noisier lags. The receiver now takes
   `np.argmax`. Measured consequence: zero frames off by more than 1 sample
   in 8,300 trials, at every SNR from 0 to 25 dB.
3. **CFO refinement divides out the known training values first.** The old
   `angle(sum(T[1] * conj(T[0])))` is only the inter-symbol phase advance when
   both symbols carry the same constellation. It now forms
   `(grid[k] * conj(_TRAINING[k]))` per carrier before taking the product, so
   the data cancels and the channel -- common to both symbols -- still cancels
   with it. The unambiguous range is unchanged at one symbol period,
   +/-48000/(2*1152) = +/-20.83 Hz, which comfortably covers the residual left
   by the 1 Hz coarse search.

### What did not change

Airtime, frame structure, and capacity are untouched: still 2 training + 120
payload symbols, 140,544 samples, 2.928 s keyed, 2,749-byte maximum payload,
**7,510.93 bit/s** sustained. `rate_accounting()` is byte-identical before and
after, the payload half of the frame is sample-identical, and frame PAPR is
12.50 dB either way. Only 1,152 samples of the transmitted waveform differ.
`demodulate_oracle` and the milestone-1 rate proof are unaffected.

The one measurable cost: the receiver's noise-free implementation floor rose
from **~1.74% to ~2.12% EVM** (full-capacity frames, six payload seeds). The
`scipy.signal.hilbert` analytic front end leaks across the per-symbol FFT, and
with identical training symbols the two leakage patterns were similar enough
that averaging them cancelled part of the error; two different symbols leak
differently. The floor is still nearly five times below the 10% trigger and
below every conclusion drawn here, but it is a real, if small, regression, and
recommendation 3 below would remove it.

## Method

### Waveform and receiver

`experiments/hc2_32qam/hc2_32qam.py`: 49 carriers, 1,024-sample FFT at 48 kHz,
128-sample cyclic prefix, 41.667 symbol/s, coherent rectangular 32QAM,
punctured K=7 rate-3/4 FEC, 2 training + 120 payload symbols, 2.928 s keyed,
2,749-byte maximum payload, 7,510.93 bit/s sustained full-frame user rate.
Every trial used the full 2,749-byte capacity.

The receiver is `demodulate`: matched-filter acquisition over +/-20 Hz in
1 Hz steps, training-pair CFO refinement, per-carrier complex equalization
from the two training symbols, and decision-directed common-phase tracking,
with hard 32QAM slicing before the soft Viterbi decoder.

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

So 11.5 dB waveform SNR is 21.7 dB in 2.3 kHz, or Eb/N0 16.5 dB; 13 dB is
23.2 dB and 18.0 dB respectively.

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
`(SNR, point index)` pair is repeated within either campaign, so each per-SNR
pool below is a pool of independent trials.

The post-fix runs reproduce the milestone-3 `--points` lists verbatim, so
matching `(SNR, point index)` pairs across the two campaigns are the same
payload against the same noise realization. That makes the before/after
comparison a paired one wherever both campaigns cover a point, which is why
mis-acquisition counts can be quoted as eliminated rather than merely absent.

### EVM measurement

`demodulate` exposes no constellation, so `benchmark_hc2_snr.frame_metrics`
recomputes the payload constellation outside the receiver from the receiver's
own `start_sample` and `frequency_offset_hz` diagnostics, repeating its
equalization and decision-directed phase track step for step. Measuring
therefore cannot perturb what the receiver decided, and a test asserts the
replica re-derives the receiver's exact decode.

Two figures are recorded per frame:

- **decision-directed EVM** -- error against the receiver's own hard
  decisions. This is what a real receiver can compute in-frame with no
  knowledge of the payload, and it is the figure quoted everywhere below.
- **true (data-aided) EVM** -- error against the transmitted constellation.
  An oracle reference only.

They agree within 1% relative above 12 dB and diverge below it, because
slicing errors drag the reference toward the received point. A trigger built
on the decision-directed figure is therefore **optimistic** at low SNR, which
is acceptable here only because the useful threshold sits at 10%, where the
two still agree closely.

The noise-free implementation floor is **~2.12% EVM**, and it is a receiver
property, not a channel one: the oracle real-FFT path measures ~1e-6%, while
the `scipy.signal.hilbert` analytic front end used by `demodulate` leaks
across the per-symbol FFT. It is far below the useful threshold and does not
affect any conclusion here.

### Commands

Six runs, all with master seed 20260830, all writing to gitignored scratch:

```sh
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 100 \
  --points 0 2 4 6 8 9 10 11 12 13 14 16 20 25 \
  --out logs/scratch/hc2_awgn_coarse_fixed.json
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 300 \
  --points 9.5 10 10.5 11 11.5 12 12.5 13 \
  --out logs/scratch/hc2_awgn_knee_fixed.json
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 1000 --points 13 14 \
  --out logs/scratch/hc2_awgn_operating_fixed.json
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 1000 --points 12.5 \
  --out logs/scratch/hc2_awgn_low125_fixed.json
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 1000 --points 16 \
  --out logs/scratch/hc2_awgn_high16_fixed.json
python -m experiments.hc2_32qam.benchmark_hc2_snr --trials 500 --points 20 \
  --out logs/scratch/hc2_awgn_high20_fixed.json
```

8,300 frame trials, six processes in parallel on an 8-core 2026 development
host, Python 3.11.15 / NumPy 2.4.6 / SciPy 1.17.1. The first five commands are
the milestone-3 commands with identical `--points` lists (the 12.5 dB
1,000-trial run is new, and the 20 dB run is the first 500 trials of the
milestone-3 1,000). HC2 is not a declared mode and has no
`logs/mode_qualification/` campaign directory, so by `LOGS.md`'s rules these
are scratch artifacts and are cited by command, not by path.

## Results (AWGN)

### FER against waveform SNR

Pooled across the six runs above. "Mis-acq" counts frames whose acquired
start was one full OFDM symbol late -- the defect the fix removes. "Acq" is
any other acquisition failure (start outside the cyclic prefix or carrier
offset estimate beyond 2 Hz). "Payload" is a correctly acquired frame the
decoder could not recover.

| Waveform SNR (dB) | Trials | Delivered | FER | FER Wilson 95% | Mis-acq | Acq | Payload | Realized bit/s |
| ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| 0 | 100 | 0 | 1.000 | [0.963, 1.000] | 0 | 0 | 100 | 0 |
| 2 | 100 | 0 | 1.000 | [0.963, 1.000] | 0 | 0 | 100 | 0 |
| 4 | 100 | 0 | 1.000 | [0.963, 1.000] | 0 | 0 | 100 | 0 |
| 6 | 100 | 0 | 1.000 | [0.963, 1.000] | 0 | 0 | 100 | 0 |
| 8 | 100 | 0 | 1.000 | [0.963, 1.000] | 0 | 0 | 100 | 0 |
| 9 | 100 | 3 | 0.970 | [0.916, 0.990] | 0 | 0 | 97 | 225 |
| 9.5 | 300 | 25 | 0.917 | [0.880, 0.943] | 0 | 0 | 275 | 626 |
| 10 | 400 | 157 | 0.608 | [0.559, 0.654] | 0 | 0 | 243 | 2,948 |
| 10.5 | 300 | 209 | 0.303 | [0.254, 0.358] | 0 | 0 | 91 | 5,233 |
| 11 | 400 | 345 | 0.138 | [0.107, 0.175] | 0 | 0 | 55 | 6,478 |
| **11.5** | 300 | 288 | 0.040 | [0.023, 0.069] | 0 | 0 | 12 | 7,210 |
| 12 | 400 | 397 | 0.0075 | [0.0026, 0.0218] | 0 | 0 | 3 | 7,455 |
| 12.5 | 1,300 | 1,294 | 0.0046 | [0.0021, 0.0100] | 0 | 0 | 6 | 7,476 |
| **13** | 1,400 | 1,400 | 0.000 | [0.000, 0.0027] | 0 | 0 | 0 | 7,511 |
| 14 | 1,100 | 1,100 | 0.000 | [0.000, 0.0035] | 0 | 0 | 0 | 7,511 |
| 16 | 1,100 | 1,100 | 0.000 | [0.000, 0.0035] | 0 | 0 | 0 | 7,511 |
| 20 | 600 | 600 | 0.000 | [0.000, 0.0064] | 0 | 0 | 0 | 7,511 |
| 25 | 100 | 100 | 0.000 | [0.000, 0.037] | 0 | 0 | 0 | 7,511 |

Realized bit/s is `7,510.93 * delivered / trials`, matching how HC2b and
HC2c report observed goodput. Like the nominal figure it excludes acquisition
preamble, PTT and turnaround, ARQ, and link air headers, so it is a
frame-layer rate, not application throughput.

The waterfall is sharp, as expected for rate-3/4 32QAM: FER falls from 0.92
at 9.5 dB to 0.04 at 11.5 dB, and the curve now keeps falling instead of
flattening -- the flat shelf in the milestone-3 data was the tie-break, not
the channel. Below 9 dB the mode does not work at all.

### Acquisition

Every one of the 8,300 trials acquired correctly:

| Statistic | Value |
| --- | ---: |
| Frames with `start_error_samples == 1152` | **0** |
| Frames with any start error beyond the cyclic prefix | 0 |
| Frames with a start error beyond +/-1 sample | 0 |
| Distribution of non-zero start errors | -1: 16, +1: 14 |
| Largest carrier-offset estimate error (true offset 0 Hz) | 0.65 Hz, at 0 dB |
| Largest at 12 dB and above | 0.22 Hz |

The milestone-3 campaign, for contrast, mis-acquired 48/100 frames at 0 dB,
23/400 at 12 dB and 16/1,100 at 14 dB. Acquisition is no longer a failure
mode of this receiver at any SNR in the swept range; the waveform simply stops
decoding when the noise wins.

### EVM against decode success

Pooled over all 8,300 trials, binned by decision-directed EVM:

| EVM bin (%) | Frames | Decoded | P(decode) |
| --- | ---: | ---: | ---: |
| 0-6 | 705 | 705 | 1.000 |
| 6-8 | 1,868 | 1,868 | 1.000 |
| 8-9 | 1,782 | 1,782 | 1.000 |
| 9-9.5 | 1,196 | 1,192 | 0.997 |
| 9.5-10 | 474 | 470 | 0.992 |
| 10-10.5 | 310 | 298 | 0.961 |
| 10.5-11 | 342 | 299 | 0.874 |
| 11-11.5 | 303 | 219 | 0.723 |
| 11.5-12 | 334 | 147 | 0.440 |
| 12-12.5 | 270 | 33 | 0.122 |
| 12.5-13 | 157 | 4 | 0.025 |
| above 13 | 559 | 1 | 0.002 |

Candidate thresholds, accepting a frame when EVM is at or below the value:

| Threshold | Accepted | P(decode &#124; accept) | Failing frames admitted | Good frames rejected |
| ---: | ---: | ---: | ---: | ---: |
| 9.33% | 5,201 | 1.00000 | 0 | 1,817 |
| 9.5% | 5,551 | 0.99928 | 4 | 1,471 |
| **10.0%** | 6,025 | 0.99867 | 8 | 1,001 |
| 10.5% | 6,335 | 0.99684 | 20 | 703 |
| 11.0% | 6,677 | 0.99056 | 63 | 404 |
| 11.63% | 7,056 | 0.97520 | 175 | 137 |
| 12.0% | 7,314 | 0.95433 | 334 | 38 |

11.63% again maximizes raw accuracy (96.25% of all 8,300 trials classified
correctly) and again admits far too many failing frames; 10% remains the right
choice for a fallback trigger, which should be biased against admitting them.

The overlap region is **9.34% to 13.14%**: the worst decoding frame measured
13.14% and the best failing frame 9.34%, and 2,527 trials fall between them.
It is wider than the milestone-3 figure of 9.91%-12.83%, and that is a more
honest number rather than a worse receiver -- the old sweep's failures above
12.5 dB were mis-acquisitions reading 91% and up, which no threshold has to
work to separate. Restricted to 12.5 dB and above the two populations still
nearly separate, but they now touch: decoded frames reach 10.28% and the six
failing frames start at 9.34%.

**Recommended trigger: fall back when decision-directed EVM exceeds 10%.**
Justification: it admits 8 failing frames in 6,025 accepted, and at 12 dB and
above -- where HC2 clears the FER gate with room -- every decoded frame
measured at or below 10.48% and the median was 9.66% or lower. A frame reading
above 10% is by construction in or below the waterfall. Note that the
distinctive "EVM near 100%" mis-acquisition signature the milestone-3 write-up
described no longer occurs, because the mis-acquisition no longer occurs; a
link should no longer expect to see it.

Median EVM by point, for calibration:

| SNR (dB) | 9 | 10 | 11 | 11.5 | 12 | 12.5 | 13 | 14 | 16 | 20 | 25 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Median EVM (%) | 12.94 | 11.83 | 10.75 | 10.21 | 9.66 | 9.21 | 8.74 | 7.87 | 6.42 | 4.43 | 3.15 |

## Superseded results (milestone-3 receiver, identical training symbols)

Everything in this section describes the **previous** waveform and receiver
and is retained as the evidence for the fix. Do not quote it as HC2's current
performance.

Over 7,800 pooled trials that receiver cleared the section 3 FER gate at
**12.0 dB** (375/400, FER 6.3% [4.3%, 9.1%]), reached FER at most 1e-2 only at
**16.0 dB** (1,099/1,100, FER 0.09% [0.02%, 0.51%]), and first showed no
failure at all at **20.0 dB** (1,100/1,100). Realized payload passed
7,050 bit/s from 12.5 dB.

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

Replaying three failing 14 dB trials showed how narrowly the tie-break lost:

| Trial | Best coarse offset | Peak metric | Metric at true start | Ratio | Chosen offset |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 47 | 0 Hz | 0.963278 | 0.957165 | 0.99365 | +1,152 |
| 188 | 0 Hz | 0.961511 | 0.956166 | 0.99444 | +1,152 |
| 212 | 0 Hz | 0.962135 | 0.956648 | 0.99430 | +1,152 |

Forcing the true start and re-deriving CFO from the correct training pair
recovered **38/38** failures at 13 dB and **16/16** at 14 dB, exactly, which is
what predicted that fixing acquisition alone would move the 1e-2 criterion from
16 dB down toward 12.5 dB. The measured move was to 13.0 dB, with 12.5 dB
missing by three parts in ten thousand of Wilson upper bound.

That campaign's EVM analysis reported a 9.91%-12.83% overlap region, a 10%
trigger admitting 2 failing frames in 5,484 accepted, and a noise-free
implementation floor of 1.81%. Its commands were the five listed in the
milestone-3 write-up, differing from the current ones only in the output paths
and in not including the 1,000-trial 12.5 dB run.

## Honest limits (AWGN)

**Trial counts.** 1,400 trials with zero failures bounds FER only at 0.27%
with 95% confidence. This campaign can establish "FER at or below 1e-2" from
13 dB. It **cannot** establish a 1e-3 or 1e-4 frame error rate anywhere; that
needs 10^4-10^5 trials per point, roughly 3-30 CPU-hours each at the current
~1 s/trial. The 100-trial coarse points are screening resolution only.

**12.5 dB is a coin-toss call.** Its Wilson upper bound is 1.0033% against a
1% criterion. A different 1,300-trial draw could put it on either side. The
conservative reading -- 13.0 dB -- is the one quoted in the conclusion, but a
reader who cares about the real threshold should read it as "between 12.5 and
13 dB".

**Hard-decision demapping penalty (estimate, not implemented).**
`demodulate` hard-slices 32QAM through `bits_from_qam32` before the soft
Viterbi decoder, discarding the reliability information Gray-mapped 32QAM
makes available. A milestone-3 scratch comparison -- same captures, same
acquisition, equalization and phase track, replacing only the slicer with a
max-log LLR demapper into the same `fec.K7.decode_soft` -- moved the
50%-delivery point from about 10.2 dB to about 7.8 dB, so the
**hard-decision penalty is roughly 2.4 dB**, consistent with the 2 dB rule of
thumb. That comparison was made against the identical-training receiver, where
the acquisition tie-break capped both curves at the top and disguised most of
the gain; it has **not** been repeated against the current receiver, and the
gain above the knee should now be larger, not smaller. Soft 32QAM metrics
remain explicitly out of scope.

**What AWGN does and does not predict.** It fixes the thermal-noise floor and
nothing else. HC2's 46.875 Hz carrier spacing is half HC1's, so it is *more*
exposed to frequency-selective fading, which is precisely the mechanism that
holds HC1 to 61/100 under the disturbed Watterson preset regardless of SNR.
Its 2.928 s frame is also nearly four times HC1's, so a given Doppler spread
has four times as long to decorrelate the channel from the two front-loaded
training symbols, which HC2 never refreshes. Neither effect appears anywhere
in this data. An AWGN threshold of 11.5 dB is a lower bound on the SNR any
real HF path will need, not a prediction of one.

**Scope.** One logical direction, no sample-clock offset, no CFO beyond the
+/-20 Hz search (all trials here were at zero offset), no radio, no ARQ, no
link. HC2 remains outside the qualification process entirely.

## Recommendations (milestone 3)

Ordered by measured value. Item 1 of the milestone-3 list -- fix the
acquisition ambiguity -- is done and is what this document's "Results" section
measures. The fading campaign has its own recommendations at the end of the
file, and where the two disagree the fading ones are the later evidence.

1. **Soft 32QAM LLR demapping.** About 2.4 dB at the 50% point per the
   milestone-3 scratch estimate, and probably more above the knee now that
   the acquisition tie-break is no longer capping the top of the curve.
   The largest remaining win on AWGN.
2. **Refresh the channel estimate mid-frame.** Not measurable on AWGN, but
   two training symbols at the head of a 2.928 s frame is the design most
   exposed to the fading milestone 4 will introduce. HC2c's pilot work is the
   obvious precedent.
3. **Replace the analytic front end's per-symbol leakage.** The ~2.12% EVM
   floor is harmless at present but is a real error budget the receiver spends
   before the channel does anything, and it grew slightly with the distinct
   training symbols.
4. **Re-measure the EVM trigger once (1) lands.** The 10% threshold was
   calibrated against a hard-slicing receiver. Soft demapping moves the
   decodable region without moving the EVM of the received constellation, so
   the threshold will need to rise.

## Milestone 4: the Watterson fading boundary

### Conclusion

**HC2's operating envelope is narrower than the mildest standard HF fading
preset.** Against `mid_latitude_quiet` -- ITU-R F.1487's most benign
mid-latitude case, 0.5 ms differential delay and 0.1 Hz frequency spread --
HC2 delivered **0 of 300 frames at every SNR from 11.5 dB to 40 dB**. That is
1,800 consecutive failures with no SNR trend. The mode does not have a quiet
-preset threshold; it has none at all.

This is the expected shape of the answer for a speed-first top rung, and it
is not a defect. The useful result is the parametric boundary underneath it,
which says *how* favourable a path has to be:

| Channel parameter | HC2 still works | HC2 is broken |
| --- | --- | --- |
| Differential delay (near-static channel) | below ~0.1 ms | 0.25 ms and beyond |
| Frequency spread (flat channel) | up to ~0.005 Hz | 0.0075 Hz and beyond |
| `mid_latitude_quiet` | -- | 0/1,800 at any SNR |

**Differential delay binds first, and by a wide margin.** At an essentially
static 0.001 Hz spread and a generous 25 dB SNR, a flat channel delivers
599/600, but only 0.25 ms of differential delay -- one tenth of the 2.67 ms
cyclic prefix -- already costs 35.5% FER, and 0.5 ms costs 57.3%. The cyclic
prefix is not the binding constraint and this is not inter-symbol
interference: it is two-path frequency-selective fading putting nulls in a
2,250 Hz band that one front-loaded channel estimate and a rate-3/4 code
cannot ride out. Only past 4 ms, where the delay exceeds the prefix, does
performance collapse the rest of the way to ~96%.

Both of `mid_latitude_quiet`'s parameters break HC2 *independently*: its
0.5 ms delay alone gives 57.3% FER on an otherwise static channel, and its
0.1 Hz spread alone gives 89.0% on an otherwise flat one. Together they give
100%. Nothing about the preset is marginal for this waveform.

### The result that matters most: failure is not always loud

Milestone 3 recommended EVM > 10% as the fallback trigger. Under fading that
trigger is **only reliable where the channel is violently bad, and it is
unreliable exactly where a link controller most needs it.**

| Regime | FER | Failed frames the trigger caught | Median EVM of failed frames |
| --- | ---: | ---: | ---: |
| `mid_latitude_moderate`, 15-30 dB | 1.000 | **100%** | 78-93% |
| `mid_latitude_disturbed`, 15-30 dB | 1.000 | **100%** | 95-104% |
| `mid_latitude_quiet`, 11.5-40 dB | 1.000 | **99.7-100%** | 34-38% |
| Delay 0.1 ms, 15-30 dB | 0.055-0.195 | **45-57%** | 9.4-10.6% |
| Delay 0.5 ms, spread 0.001-0.005 Hz | 0.573-0.670 | **66-71%** | 12.1-12.6% |

In the delay-dominated regime the failing frames look almost healthy: their
median EVM sits within a point or two of the 10% trigger, and single points
carried up to 45 silent failures out of 200 trials -- frames that were
corrupt and passed the health check. A controller trusting EVM alone would
sit at 57% FER believing the link was fine.

The trigger also mis-fires in the other direction as the channel degrades:
at 0.03-0.1 Hz spread, 10-27% of frames that **did** decode correctly would
have been flagged. A single global EVM threshold cannot separate these
populations.

What *is* reliable is the CRC. Across all **10,400 fading trials there was
not one false accept** -- never once did a corrupt frame pass CRC32 and get
delivered as good (95% upper bound 0.037%). Frame-level integrity holds
completely, which means a fallback design can be built on decode outcome
even though it cannot be built on EVM.

Acquisition also held: zero mis-acquisitions of the retired
one-symbol-late class at any point, and start error stayed within one cyclic
prefix everywhere, including the 1,800 quiet-preset frames that never
decoded. The frames are found; they cannot be read.

### Results

10,400 trials. Parametric points use the same F.1487 two-path equal-power
geometry as the presets, with delay and spread moved one at a time.

**Frequency spread, flat channel (0 ms delay), 25 dB, 200 trials/point:**

| Spread (Hz) | Delivered | FER | Realized bit/s |
| ---: | ---: | ---: | ---: |
| 0.001 | 200/200 | 0.000 | 7,510.9 |
| 0.0025 | 197/200 | 0.015 | 7,398.3 |
| 0.005 | 194/200 | 0.030 | 7,285.6 |
| 0.0075 | 179/200 | 0.105 | 6,722.3 |
| 0.01 | 174/200 | 0.130 | 6,534.5 |
| 0.02 | 129/200 | 0.355 | 4,844.5 |
| 0.05 | 62/200 | 0.690 | 2,328.4 |
| 0.1 | 22/200 | 0.890 | 826.2 |

**Differential delay, near-static channel (0.001 Hz spread), 25 dB:**

| Delay (ms) | Delivered | FER | Realized bit/s |
| ---: | ---: | ---: | ---: |
| 0 | 599/600 | 0.002 | 7,498.4 |
| 0.1 | 368/400 | 0.080 | 6,910.1 |
| 0.25 | 129/200 | 0.355 | 4,844.5 |
| 0.5 | 171/400 | 0.573 | 3,211.0 |
| 1 | 89/200 | 0.555 | 3,342.4 |
| 2.667 (= cyclic prefix) | 84/200 | 0.580 | 3,154.6 |
| 4 | 7/200 | 0.965 | 262.9 |
| 8 | 6/200 | 0.970 | 225.3 |

Between 0.5 ms and the 2.67 ms prefix the curve is flat at 53-59%: once the
band is selectively faded, more delay does not make it materially worse until
the prefix is actually exceeded.

**No SNR escape.** At 0.1 ms delay the FER is 19.5% / 5.5% / 11.5% / 9.0% at
15 / 20 / 25 / 30 dB -- an irreducible floor with no trend, scattered by
realization draw rather than by noise. `mid_latitude_quiet` is 0/300 at
11.5, 15, 20, 25, 30 **and 40 dB**. Beyond about 15 dB, adding transmit
power buys HC2 nothing on a fading path.

### Method

`benchmark_hc2_watterson.py` is a sibling of the AWGN sweep rather than an
extension of it: the AWGN point space is a one-dimensional SNR list and
`trial_seed` keys on the index within that list, so folding a three
-dimensional (delay, spread, SNR) space into the same `--points` list would
have renumbered the AWGN indices and silently broken the paired milestone-3
comparison. It imports `frame_metrics`, `wilson`, `_quantiles` and
`evm_separation` from `benchmark_hc2_snr`, so the EVM figures here have
exactly the definition the AWGN campaign calibrated the trigger against, and
the AWGN curve remains a valid control.

Each trial chains `WattersonChannel` into `AwgnChannel` in that order,
matching `whale.qualification.channel_factory("watterson", ...)` including its
`seed ^ 0x5A5A` noise seed. The SNR reference stays the signal-bearing span
of the transmitted capture. Under fading that is a per-realization average:
a frame spending most of its 2.928 s in a deep fade still gets noise scaled
to its own mean power, so instantaneous in-fade SNR is much worse than the
label. Spreads follow the F.1487 2-sigma convention.

Commands (artifacts to gitignored `logs/scratch/`, cited by command because
HC2 is not a declared mode and has no qualification campaign directory):

```sh
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --presets mid_latitude_quiet --points 11.5 15 20 --trials 300 \
  --out logs/scratch/hc2_wat_quiet_a.json
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --presets mid_latitude_quiet --points 25 30 40 --trials 300 \
  --out logs/scratch/hc2_wat_quiet_b.json
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --presets mid_latitude_moderate --points 15 20 25 30 --trials 200 \
  --out logs/scratch/hc2_wat_moderate.json
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --presets mid_latitude_disturbed --points 15 20 25 30 --trials 200 \
  --out logs/scratch/hc2_wat_disturbed.json
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --delay-ms 0 --spread-hz 0.001 0.0025 0.005 0.0075 0.01 --points 25 \
  --trials 200 --out logs/scratch/hc2_wat_spread_flat_a.json
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --delay-ms 0 --spread-hz 0.015 0.02 0.03 0.05 0.1 --points 25 \
  --trials 200 --out logs/scratch/hc2_wat_spread_flat_b.json
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --delay-ms 0.5 --spread-hz 0.001 0.005 0.01 0.02 0.05 0.1 --points 25 \
  --trials 200 --out logs/scratch/hc2_wat_spread_half.json
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --delay-ms 0 0.1 0.25 0.5 0.75 --spread-hz 0.001 --points 25 \
  --trials 200 --out logs/scratch/hc2_wat_delay_a.json
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --delay-ms 1 2 2.667 4 6 8 --spread-hz 0.001 --points 25 \
  --trials 200 --out logs/scratch/hc2_wat_delay_b.json
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --delay-ms 0 --spread-hz 0.001 --points 15 20 25 30 --trials 200 \
  --out logs/scratch/hc2_wat_operating_flat.json
python -m experiments.hc2_32qam.benchmark_hc2_watterson \
  --delay-ms 0.1 --spread-hz 0.001 --points 15 20 25 30 --trials 200 \
  --out logs/scratch/hc2_wat_operating_tenth.json
```

### Honest limits (milestone 4)

**Realization variance dominates at 200 trials.** The 0.1 ms / 0.001 Hz /
25 dB point was measured twice under different seeds and gave 191/200 and
177/200 -- 4.5% and 11.5% FER, with barely overlapping Wilson intervals.
Pooled, it is 368/400, FER 8.0% [5.7%, 11.1%]. Every single-point FER in the
tables above carries that much draw-to-draw scatter; read the shape of the
curves, not individual cells. The boundary values quoted in the conclusion
(~0.1 ms, ~0.005 Hz) are therefore order-of-magnitude statements, not
thresholds established to two significant figures.

**One geometry only.** Two equal-power paths, no Doppler *shift* (only
spread), no sample-clock offset, no CFO (all trials at zero offset), one
logical direction, no radio, no ARQ. Real paths carry unequal path powers and
a bulk frequency shift, neither of which is tested here.

**The quiet-preset result is bounded below, not measured.** 0/1,800 says the
FER is above 99.8% with 95% confidence. It does not distinguish "always
fails" from "succeeds once in 10,000", and nothing in this campaign should be
read as the latter being ruled out.

**Not qualification evidence.** AWGN and Watterson sweeps in an experiment
directory, from a working tree, with artifacts in gitignored scratch. HC2 has
no manifest entry, no mode ID, and no `logs/mode_qualification/` campaign.

### Recommendations (milestone 4)

1. **Do not build the fallback trigger on EVM alone.** The CRC is the trustworthy
   signal -- zero false accepts in 10,400 fading trials -- and EVM is not, in
   the regime that matters. Demote on decode outcome; use EVM only as
   corroboration, and expect a single global threshold to both miss real
   failures and demote healthy frames near the boundary.
2. **Refresh the channel estimate mid-frame.** This was recommendation 2 of
   milestone 3 and the fading data promotes it to the single highest-value
   change. Two training symbols at the head of a 2.928 s frame is the design
   most exposed to exactly what binds here. HC2c's payload-pilot work is the
   precedent, and its measured result -- pilots helped at every moderate and
   disturbed point -- is the reason to expect it to move this boundary.
3. **Attack frequency selectivity, not just time selectivity.** Delay binds
   first, so mid-frame pilots alone will not be enough; the per-carrier
   equalizer has to survive nulls across a 2,250 Hz band. Frequency-domain
   interpolation across carriers and deeper interleaving are the levers.
4. **Consider a shorter frame for the top rung.** 2.928 s is nearly four times
   HC1's. A shorter frame both shortens the tracking span and cuts the cost of
   each retransmission, at some rate overhead.
5. **Declare the envelope explicitly.** Whatever HC2 becomes, its negotiation
   entry needs to state the delay and spread bounds above, because the mode
   fails silently inside part of that region rather than announcing itself.

## Next experiment (milestone 5)

Two candidate paths, in order of expected value:

1. Mid-frame channel tracking (recommendations 2 and 3 above), then re-run
   this exact boundary sweep to measure how far the envelope moves. The
   commands and seeds here make that a paired comparison.
2. Soft 32QAM LLR demapping, worth ~2.4 dB on AWGN, which widens the fading
   envelope only insofar as the boundary is SNR-limited -- and this campaign
   shows it mostly is not.

Real-radio measurement (the original milestone 5) should wait until the
envelope is wide enough that a real path can be expected to sit inside it.
