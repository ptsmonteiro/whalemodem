# hf18_ofdm49_vara — 49 carriers at 50 Hz spacing, VARA HF's top-speed geometry

**Bottom line (revised 2026-09-07 after a 100x per-arm trial at matched
5 s air time): the 49-carrier geometry WINS. At the frame size that
amortizes this bench's fixed ~155 ms keying overhead, the two geometries
deliver frames at statistically indistinguishable rates (96/100 vs
94/100, Fisher p = 0.75), so hf18's higher rate is no longer bought at a
reliability cost: 7,586 bps on air vs 7,193, and 7,131 vs 6,905 once
frame loss is priced in (+3.3%). The earlier verdict below — that hf10's
97-carrier configuration should be kept — was drawn from 30 and 40 trial
samples at a 2.4 kB frame and did not survive the larger test.**

**Unchanged: the zero-guard version the geometry nominally implies
(50.0 symbols/s) is dead on this path — 6.7 dB of EVM, and a QPSK probe
frame would not decode. The working point is a 2 ms guard (`cp_len=24`,
45.5 symbols/s).**

## Air time vs frame time

Every keying costs a fixed **~155 ms** of PTT ramp and tail on this bench
(153-158 ms, flat across every geometry, drive level and frame size
tried). `frame_seconds()` does not include it, so every bps figure in
this project's OFDM record — hf10's 7,213 included — is a waveform
number, not an on-air one. At hf10's 2.4 kB frame that is 5.5% of
occupancy:

| Config | payload | frame | air (keyed) | net on frame | net on air |
|---|---|---|---|---|---|
| hf10 record | 2,388 B | 2.655 s | 2.811 s | 7,195.5 | 6,796.2 |
| hf18 cp24 | 2,388 B | 2.508 s | 2.662 s | 7,617.2 | 7,176.6 |

**Neither mode had ever actually moved 7 kbps through the channel at that
frame size.** Since the overhead is fixed per keying, the fix is a longer
frame, and that is what the trial below tests.

## The 100x interleaved A/B trial (`ab_trial.py`)

Both arms resized to codeword-aligned payloads (rate-3/4 LDPC, k=486)
landing within 72 ms of each other on air. Arms alternate **trial by
trial** in a single radio session with the order flipping each round, so
neither channel drift nor a first-in-round ordering effect can land on
one arm: a blocked 100+100 design would take 11 minutes per block, and
this project has repeatedly found drift on that timescale (hf8, hf9,
hf11).

| arm | geometry | payload | codewords | frame | air | delivered | 95% CI | mean raw BER | net on air | effective |
|---|---|---|---|---|---|---|---|---|---|---|
| hf10 | fft 480 / cp 60 / 97 bins | 4,550 B | 75 | 4.905 s | 5.061 s | 96/100 | 90.2-98.4% | 0.0055 | 7,192.8 | 6,905.1 |
| **hf18** | **fft 240 / cp 24 / 49 bins** | 4,732 B | 78 | 4.840 s | 4.990 s | **94/100** | 87.5-97.2% | 0.0130 | **7,585.9** | **7,130.8** |

Fisher's exact on delivery: **p = 0.75**. The confidence intervals
overlap almost completely. The 90%-vs-97.5% gap that drove the original
verdict was a small-sample artifact.

Three things worth recording:

- **Doubling the frame lifted both arms above 7 kbps on air for the first
  time** (6,796 -> 7,193 and 7,177 -> 7,586), purely by amortizing the
  keying overhead from 5.5% to ~3%. This was a bigger lever than any
  waveform parameter tested in this experiment.
- **hf10's four failures cluster in rounds 32-41; hf18 failed at 30 and
  32.** The overlap points to a channel disturbance in that window rather
  than four independent geometry failures, so hf10's 96% is if anything
  slightly flattered by the arms being measured together — which is the
  interleaved design working as intended.
- **Every failure in both arms was a `crc_fail` with post-FEC BER between
  3e-5 and 1.6e-3** — a handful of bits, i.e. one LDPC codeword short of
  convergence out of 75-78. This is exactly hf11's frame-length
  mechanism, and it means the remaining 4-6% is addressable by FEC
  structure (a stronger rate on one codeword, or per-codeword
  retransmission) rather than by more SNR.

hf18 still runs 2.4x hf10's raw BER (0.0130 vs 0.0055) — the ~2.5 dB
per-bin SNR deficit documented below is real and unchanged. The finding
is that rate-3/4 LDPC absorbs it, so the deficit does not reach frame
delivery.

## Recommendation

**The 49-carrier / 2 ms-guard geometry at a ~5 s frame is the better
operating point on this path: 7,586 bps on air, 94/100 delivered.** It is
also the more forgiving of the two to run, having 1.8 dB lower crest
factor.

Caveat worth keeping in view: the two arms are separated by 3.3% in
effective throughput on one session's data, and their delivery rates are
statistically tied. This is a preference, not a decisive margin, and the
~1.5 dB unexplained per-bin SNR deficit below is still the thing that
would make it decisive if closed.

## Bandwidth and the shipped configuration (HF7)

**Updated 2026-09-07:** the owner retired the 2,300 Hz occupied-bandwidth
ceiling and redefined the HF channel as 300-2,700 Hz inclusive, gated on a
2,500 Hz 99%-power width (`SPEED_LADDERS.md`). **The 49-carrier
arrangement measured above is therefore what ships**: it fills the channel
edge to edge and measures 2,444 Hz, inside the new gate. The section below
records the 45-carrier trim that was built while the old ceiling stood; it
is retained because it is the more reliable configuration and is the
fallback if the FER gate has to be recovered.

Under the retired ceiling the 49-carrier arrangement failed at 2,444 Hz
against 2,300 Hz -- the same gate that made HF2 non-compliant.

The fix was free, and then some. The 100-trial run's per-bin SNR census
identified the **four weakest carriers of the 49 as the four lowest** --
300 Hz at 13.7 dB, 350 at 14.6, 400 at 16.8, 450 at 18.1, against a
~19.5 dB median. Trimming exactly those puts the mode at 45 carriers over
500-2700 Hz:

| Carriers | Band | 99%-power occupied | Verdict |
|---|---|---|---|
| 49 | 300-2700 Hz | 2,444.2 Hz | over ceiling |
| 46 | 450-2700 Hz | 2,300.0 Hz | exactly on the ceiling; qualification wants an upper bound strictly below it |
| **45** | **500-2700 Hz** | **2,253.1 Hz** | **47 Hz inside; shipped** |

50 hardware trials of the compliant configuration (`cp_len=24`, 32-QAM,
rate-3/4 LDPC, interleaved, 4,368 B payload = 72 whole codewords):

| | 49-carrier (non-compliant) | **45-carrier (shipped)** |
|---|---|---|
| occupied bandwidth | 2,444 Hz (over) | **2,253 Hz (compliant)** |
| delivered | 94/100 | **50/50** |
| FER 95% Wilson UB | 9.9% | **7.1%** (gate: <10%) |
| mean raw BER | 0.0130 | **0.0072** |
| frame | 4.840 s | 4.862 s |
| air | 4.990 s | 5.014 s |
| net per frame | 7,821 bps | 7,187 bps |
| net on air | 7,586 bps | 6,969 bps |

**Dropping the four weakest carriers cost 8% of rate and halved the raw
BER**, taking frame delivery to 50/50 with zero residual bit errors --
which is the outcome the per-bin SNR census predicted, and the reason the
trim came off the low end rather than being split across both.

That difference matters beyond bandwidth, because the two configurations
land on opposite sides of the 10% FER ceiling:

| | 49-carrier (**ships as HF7**) | 45-carrier (retained fallback) |
|---|---|---|
| band | 300-2700 Hz, channel-filling | 500-2700 Hz |
| occupied (99%-power) | 2,444 Hz | 2,253 Hz |
| delivered | 94/100 | 50/50 |
| FER 95% Wilson UB | **12.5% -- fails the 10% gate** | **7.1% -- passes** |
| mean raw BER | 0.0130 | 0.0072 |
| net per DATA frame | **7,805 bps** | 7,170.7 bps |

Both clear the Level-4 target of 4,000 bit/s -- by 95% and 79%
respectively. The shipped configuration is the faster one and the one that
uses the whole channel; it is not the one that passes the frame-error
gate. Every failure in both arms was a single non-converged LDPC codeword
out of 78 (post-FEC BER 3e-5 to 1.6e-3), so the lever for recovering the
gate at 49 carriers is FEC structure -- a stronger rate on one codeword,
or per-codeword retransmission -- rather than link margin.

**The 49-carrier configuration ships as HF7**
(`whale/modes/hf7_mode.py`, mode_id 14), the maximum-speed rung of the
default HF ladder. What that does and does not claim is recorded in
`MODE_QUALIFICATION.md`.

---

# Original write-up (2.4 kB frames, superseded verdict)

**Bottom line: the geometry works and clears the 7 kbps goal —
7,617.2 bps net at 27/30 (90%) real-hardware frames — but it does not
beat the existing hf10 record once frame loss is counted, and the
zero-guard version the geometry nominally implies (50.0 OFDM symbols/s)
is dead on this path: it costs 6.7 dB of EVM and could not decode even a
QPSK probe frame. The workable operating point is 49 carriers with a
2 ms guard (`cp_len=24`, 45.5 symbols/s), which gives +5.9% raw net rate
over hf10's 7,213 bps record but 2.6x its raw BER and ~7 points worse
frame delivery — an effective-throughput tie at best.**

All numbers are real over-the-air IC-7300 (TX) -> IC-705 (RX)
audio-coupled trials via `bench.radio_pair`, direction `ab`, one session
on 2026-09-07. `experiments/hf10_ofdm49_v6/{ofdm49_v6,evm_probe}.py` are
imported read-only and unmodified; no PHY code was written for this
experiment. Every step is a parameter combination on the codec hf10
already qualified.

## Motivation and the geometric constraint

The requested configuration — 49 carriers, 50 raw symbols/s, centred on
1500 Hz — is the geometry VARA HF uses at its top speed level. It is
also, exactly, hf10's original pre-record geometry: at the 12 kHz design
rate `fft_size=240` gives 50.0 Hz bin spacing, and `bins_in_band(240)`
returns bins 6..54 = 49 carriers spanning 300–2700 Hz, centred on
1500 Hz (bin 30) to the sample. 240 divides both 48000 and 12000, so the
alignment is exact and free.

The constraint that shapes everything below: for contiguous OFDM filling
bandwidth B with N carriers, spacing = B/N and the useful symbol time is
N/B. 49 carriers over 2450 Hz forces 50 Hz spacing, a 20 ms useful
symbol, and therefore

> **50.0 symbols/s is reachable only with `cp_len = 0`.**

"49 carriers at 50 symbols/s" and "has a guard interval" are mutually
exclusive in an SSB passband. So the real question this experiment had
to answer was not about carrier count at all — it was how much guard
this radio pair's SSB filters actually require.

## Step 1 — EVM vs guard length (`cp_sweep.py`)

hf10's record measured that `cp_len` 60 -> 30 costs 2 dB of EVM, and
attributed it to the two radios' SSB filter impulse responses, but never
measured the rest of the curve. `cp_sweep.py` sweeps it with hf10's own
EVM decomposition on a QPSK probe frame (truth symbols stay recoverable
even when the error is large, so the measurement does not depend on the
frame decoding). Values are visited **round-robin**, not blocked, so
channel drift hits every arm equally. 2 rounds, drive 0.008:

| cp_len | guard | sym/s | mean EVM SNR | after CPE+timing | QPSK probe decoded |
|---|---|---|---|---|---|
| 60 | 5.0 ms | 40.0 | **19.0 dB** | 19.0 dB | 2/2 |
| 36 | 3.0 ms | 43.5 | 18.1 dB | 18.0 dB | 2/2 |
| 24 | 2.0 ms | 45.5 | 18.0 dB | 17.9 dB | 2/2 |
| 12 | 1.0 ms | 47.6 | 16.7 dB | 16.6 dB | 2/2 |
| **0** | **0.0 ms** | **50.0** | **12.3 dB** | 12.1 dB | **0/2** |

The curve is nearly flat from 5 ms down to 2 ms (1.0 dB), then falls off
a cliff at zero: **6.7 dB below the 5 ms guard, and a QPSK probe frame
would not decode.** Removing CPE and per-symbol timing changes nothing
(19.0 -> 19.0, 12.3 -> 12.1), which is the signature of lost subcarrier
orthogonality rather than an additive impairment — it is not fixable by
drive, pilots, or FEC. `cp_len=0` is therefore ruled out on measurement,
not on argument, and with it the literal 50 symbols/s target.

The useful, unexpected half of this result is the other end: **the
guard can be cut from 5 ms to 2 ms for 1.0 dB.** hf10 never tested
between 60 and 30.

## Step 2 — frame trials at the surviving guard lengths

32-QAM, rate-3/4 LDPC, interleaved, `noise_estimator=repeat`,
2,394 B payload — hf10's recommended receiver settings, only the
geometry changed.

An early 5-trial run at `cp_len=24` with `--pilot-interval 0` returned
1/5 with raw BER swinging 2%–30%. That was a harness mistake on my part,
not a property of the geometry: hf10's record config uses
`--pilot-interval 20`, and a 2.4 s frame with no mid-frame pilot is not
the comparison anyone wanted. All rows below carry pilots.

| cp_len | sym/s | net bps | trials | decoded | mean raw BER |
|---|---|---|---|---|---|
| 36 | 43.5 | 7,286.0 | 5 | 2 | 0.0124 |
| **24** | **45.5** | **7,617.2** | 5 | 4 | 0.0127 |
| **24** (confirm, seed 5004) | 45.5 | 7,617.2 | 10 | 9 | 0.0143 |
| 12 | 47.6 | 7,979.9 | 5 | 3 | 0.0222 |

The three cp=36 failures had post-FEC BER of 0.0003–0.0006 — a handful
of bits short of convergence, not a collapse. With 5 trials per arm
these are not separable from each other; cp=24 is chosen because it is
the arithmetic sweet spot (the 1.0 dB EVM cost buys 14% more symbol
rate) and it led on the measured evidence.

## Step 3 — drive re-calibration

The 49-carrier waveform has a **1.8 dB lower crest factor** than the
97-carrier one (8.9 vs 10.7 dB), so hf10's `--drive-scale 0.008`, which
was calibrated against the 97-carrier crest, is not the right level here.

| drive | trials | decoded | mean raw BER |
|---|---|---|---|
| 0.008 | 15 | 13 | 0.0143 |
| **0.012** | 5 | 5 | 0.0159 |
| **0.012** (confirm) | 10 | 9 | 0.0150 |
| 0.016 | 5 | 3 | 0.0197 |

0.012 and 0.008 are indistinguishable; 0.016 is clearly past the peak.
The extra level is real but the EVM curve is flat over that range, so it
buys nothing measurable. **Pooled `cp_len=24` result: 27/30 (90%) at
7,617.2 bps net.**

## Step 4 — same-session control (this is the result that decides it)

Every hf10-vs-hf18 comparison is worthless without a control, because
this project has repeatedly found session-to-session channel drift
(hf8, hf9, hf11). hf10's exact recommended configuration was re-run in
the same session, twice:

| Config | trials | decoded | mean raw BER | net bps |
|---|---|---|---|---|
| hf10 record (fft 480, cp 60, 97 bins) | 10 | **10** | 0.0044 | 7,195.5 |
| hf10 record (fft 480, cp 60, 97 bins) | 10 | **10** | 0.0058 | 7,195.5 |
| **hf18 (fft 240, cp 24, 49 bins)** | 30 | **27** | 0.0150 | **7,617.2** |

**The control reproduced the record exactly — 20/20, 7,195 bps.** Today's
channel is the record's channel, so the comparison is fair and this
session was not unusually favourable to either arm.

### The verdict

| | hf10 record | hf18 (VARA geometry) |
|---|---|---|
| carriers / spacing | 97 / 25 Hz | 49 / 50 Hz |
| symbol rate | 22.2 /s | 45.5 /s |
| guard | 5.0 ms (11%) | 2.0 ms (5%) |
| crest factor | 10.7 dB | 8.9 dB |
| net bps when delivered | 7,195.5 | **7,617.2 (+5.9%)** |
| mean raw BER | 0.0051 | 0.0150 (2.6x) |
| frame delivery | 39/40 incl. record (97.5%) | 27/30 (90%) |
| **effective ARQ throughput** | **~7,015 bps** | ~6,855 bps |

The requested geometry **does** clear 7 kbps, and its headline rate beats
the record. But it pays 2.6x the raw BER for it, and once frame loss is
priced in the two are a tie that slightly favours the incumbent. Fisher's
exact on 20/20 vs 27/30 gives p = 0.27, so the delivery-rate gap alone
is not statistically established at these sample sizes — but
the raw-BER gap is unambiguous and consistent across every run.

**Recommendation: hf10's fft 480 / cp 60 / 97-carrier configuration
remains the operating point.** hf18's geometry is recorded as a real,
working 7.6 kbps alternative, not as a replacement.

## What would make this geometry win

The 49-carrier arm is ~2.5 dB worse in per-bin SNR (median 19.3–19.9 dB
vs 21–23 dB), and only 1.0 dB of that is the shorter guard measured in
Step 1. The remaining ~1.5 dB is unexplained by this experiment and is
the thing to chase, not the carrier count. Two untested levers:

- `--edge-guard-bins`: at 50 Hz spacing each bin is twice as wide, so the
  outermost bins sit further into the SSB filter skirts than the
  97-carrier layout's do. Min per-bin SNR was 14.4 dB against a 19.3 dB
  median. Dropping bins costs 2% of rate each, and there is 5.9% of
  headroom to spend.
- Bit loading: run the edge bins at 4 bits and the centre at 5. The
  per-bin SNR spread here is 11 dB, which is wide enough for it to matter.

## Caveats

- One direction (IC-7300 -> IC-705), one session, one day's channel.
- Occupied bandwidth is ~2,450 Hz, above the 2,300 Hz ceiling
  `SPEED_LADDERS.md` sets for HF rungs — the same inherited caveat hf10
  carries. No rung claim is made here.
- `--drive-scale` values are calibrated to this bench path's audio gain
  structure and do not generalize.
- The `CLIP()` counter fires on every trial. Per hf10's 2026-09-07
  analysis this is a known false alarm on this capture device.
- Provisional experiment results, not a qualification run under
  `MODE_QUALIFICATION.md`.
