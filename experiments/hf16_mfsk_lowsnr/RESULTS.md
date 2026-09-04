# hf16 — MFSK on the `ic705 -> ic7300` minimum-power HF path

**Bottom line.** On this path, non-coherent MFSK works where OFDM does not, and
the fastest geometry that decoded every frame in a ranking campaign is
**M=256 tones, 9.375 Hz spacing, 106.7 ms symbols, rate-1/2 K=7 convolutional,
12 s frames, transmit amplitude 0.9 — 43 payload bytes per frame, 28.7 bit/s
net, 6/6 decoded.** Everything faster decoded 2-3 of 6.

The single most useful finding is *why* that is. The path's overall level swings
frame to frame between about 4 and 20 dB of sent-tone SNR, and that swing —
not within-frame fading — decides whether a frame lives. Doubling the symbol
length buys more than halving the code rate does, because what is short on this
path is energy per symbol, not coding gain.

Every number below is measured on the two radios. Nothing here is simulated.

## The path

Measured with `experiments/hf15_lowsnr_ofdm/sounder.py` and the tone probes in
this directory. `ic705` transmits, `ic7300` receives, 10.147000 MHz USB, data
mode on, **IC-705 RF power deliberately at 0% (minimum)** — that minimum-power
transmitter is the low-SNR channel being researched, not a fault.

| quantity | measured |
|---|---|
| C/N0 at transmit amplitude 0.9 | **28.5 dB-Hz** |
| equivalent SNR in 2400 Hz | **-5 dB** |
| carrier offset between the radios | **+8 Hz**, stable to a few tenths within a run |
| level drift | **~10 dB over minutes** |
| coherent integration stops paying after | **0.3-1.0 s** |

C/N0 sets a ceiling on any waveform: `Rb = C/N0 - Eb/N0`. At 28.5 dB-Hz and a
realistic 6 dB Eb/N0 for non-coherent MFSK with a rate-1/2 soft-decision code,
that ceiling is about **180 bit/s before any fading margin**. The 28.7 bit/s
actually achieved spends roughly 8 dB on implementation loss and margin against
the level swing.

## Why not OFDM

The work started as an OFDM design for the same 300-2700 Hz passband
(`experiments/hf15_lowsnr_ofdm/`). A 49-carrier BPSK frame at 50 Hz spacing
decoded **0/5**. Two independent measurements explain it:

1. Coherent integration stops paying after a few hundred milliseconds, so every
   OFDM mechanism that spends a phase reference is spending credit the channel
   does not extend.
2. The receiver's idle noise floor measured 0.035 RMS while a transmission whose
   wanted signal was supposedly 14 dB *below* the noise measured 0.075 RMS.
   Those cannot both describe an additive-noise path. They are consistent with a
   hard-limiting transmitter splattering a signal with a 10 dB crest factor.

MFSK is constant-envelope — peak and RMS are the same number, so a peak-limited
transmitter delivers all of it, and no phase reference exists anywhere in the
receive path to lose. The first MFSK screen decoded 5/5 at M=64 where the OFDM
frames had decoded nothing.

## Geometry ranking (`hardware/screen3`)

Six geometries, **round-robined** (trial 1 of every config, then trial 2, ...),
6 rounds, 12 s frames, amplitude 0.9. Round-robin ordering is essential: see
"Two mistakes" below.

| config | tones | spacing | symbol | code | net bit/s | decoded |
|---|---:|---:|---:|---|---:|---:|
| M32 r1 | 32 | 75.0 Hz | 13.3 ms | 1/2 | 164.7 | 2/6 |
| M64 r1 | 64 | 37.5 Hz | 26.7 ms | 1/2 | 96.7 | 3/6 |
| M128 r1 | 128 | 18.75 Hz | 53.3 ms | 1/2 | 54.0 | 3/6 |
| M64 r2 | 64 | 37.5 Hz | 26.7 ms | 1/4 | 46.0 | 2/6 |
| **M256 r1** | **256** | **9.375 Hz** | **106.7 ms** | **1/2** | **28.7** | **6/6** |
| M128 r2 | 128 | 18.75 Hz | 53.3 ms | 1/4 | 24.9 | 3/6 |

**More tones beats more redundancy.** M256 at rate-1/2 is both *faster* and
strictly more reliable than M128 at rate-1/4, and M128 at rate-1/2 beats M64 at
rate-1/4 at a similar rate. Spending airtime on longer symbols is a better buy
than spending it on repetition, because a longer symbol collects proportionally
more energy while repetition only averages noise the code could already handle.

With 6 trials a 6/6 result has a 95% Wilson lower bound of 0.61. This campaign
**ranks** geometries; it does not establish a 95% decode claim. See the confirm
run below.

## Why each configuration failed (`replay.py`)

`replay.py` reconstructs the transmitted tones from the recorded seed, locates
the frame by matched-filtering the whole frame, and reports per-decile symbol
error and SNR. Three findings:

**Acquisition has never been the failure.** Across every frame recorded in this
project — including frames that decoded nothing at all — the receiver's own
acquisition landed within 0.15 symbols of the truth. Every failure is
payload-side. The 1 s sync pattern and the offset-hypothesis search are not the
limitation and do not need work.

**The faster geometries fail from an energy shortfall, not fading.** M128's
three failures show SNR flat at 6-8 dB across all ten deciles with errors flat
near 0.5 — the entire frame sat about 6 dB below what that geometry needs. There
is no fade structure to interleave around.

**Within-frame fading is already handled.** M128's trial 1 ran at 16-17 dB for
the first 60% of the frame and 8-9 dB for the rest — a deep partial fade over
40% of the payload — and decoded anyway. Rate-1/2 with a full-frame
multiplicative interleaver absorbs that comfortably.

Together these kill the obvious idea that longer frames would help. The
variation that matters is slower than a frame, so a longer frame samples the
same bad level rather than averaging over it. The only lever against it is link
margin, which means rate.

**Errors are not adjacent-tone confusion.** The neighbour-error fraction
measured 0.00-0.10 throughout, so the orthogonal spacing and the +8 Hz offset
correction are sound; nothing is to be gained by widening the tone spacing.

## Soft-metric comparison — a null result (`rescore.py`)

The receiver's soft metric normalises each symbol by its own mean tone power,
which is AGC-immune but gives a symbol sitting in a fade the same weight as a
clean one. Two alternatives were implemented — `raw` (square-law combining, no
per-symbol normalisation, so faded symbols quietly weigh less) and `snr` — and
all three were run over the retained captures, which costs no airtime.

| metric | screen3 | screen1 |
|---|---|---|
| `normalized` (current) | **19/36** | 12/25 |
| `raw` | 17/36 | 12/25 |
| `snr` | 18/36 | 12/25 |

The fade-weighting hypothesis is **wrong on this data**: the current metric is
best or tied everywhere and strictly better on M128. It is kept unchanged. This
is consistent with the finding above — the failures are uniform energy
shortfalls, and no per-symbol weighting rescues a frame that is uniformly 6 dB
short.

## Two mistakes this campaign exists to prevent

**Never run a configuration's trials as a contiguous block.** The first screen
did, and produced an impossible result: a configuration that is strictly more
robust than another (same geometry, half the code rate, identical preamble)
scored 0/5 where the other scored 3/5. Replay showed acquisition had been
perfect in all of them and the symbol error rate was 50-67% throughout — the
path had simply faded across that whole block. The harness now round-robins.

**Never assume the tone's bin.** A C/N0 probe analysed the bin at exactly the
transmitted 1500 Hz and took its noise reference from neighbouring bins. With a
+8 Hz carrier offset the real tone sat at 1508 Hz, so the signal was excluded
from the measurement *and* folded into its own noise reference. The probe
reported the tone bin at **negative** sigma and concluded no signal existed on a
path that was working. A tone bin reading below its own local noise mean is the
signature of exactly this error. `tone_probe.py` now searches +/-30 Hz for the
peak and guards +/-6 Hz around it.

## Confirm run: 28.7 bit/s does NOT hold 95% (`hardware/confirm_m256`)

55 consecutive trials of the winning geometry, amplitude 0.9:
**38/55 decoded = 69.1%** (95% Wilson interval 0.560-0.801), 16 CRC failures and
1 acquisition failure. **The 6/6 in the ranking campaign was a favourable draw
from a ~70% distribution, not evidence of a reliable mode.**

The failures cluster: 5 in trials 1-40 (87% decode), 12 in trials 41-55 (20%).
That looked at first like the transmitter sagging under a 60% duty cycle, which
it is not — the sent-tone SNR moved only from 10.0 dB to 9.6 dB across the
break, and carrier offset (+8.0 to +8.6 Hz throughout) and sync scores were
unchanged. The real explanation is that **the mode is sitting exactly on its
decode cliff**, where a fraction of a dB decides everything:

| symbol error rate at the true start | outcome |
|---|---|
| <= 0.15 | decodes |
| 0.18-0.23 | **coin flip** (0.150 pass, 0.180 fail, 0.180 pass, 0.220 fail, 0.230 pass, 0.230 fail) |
| >= 0.28 | fails |

Most frames land in that 0.10-0.25 band, so a ~0.5 dB level drop moved the
median symbol error from 0.15 to 0.27 and took the decode rate with it. A mode
with margin would not care about half a dB.

**Conclusion: this path supports less than 28.7 bit/s at a 95% decode rate.**
About 2-3 dB more link margin is needed, which on this path means a lower rate.
The efficient way to buy it, given that more tones beat more redundancy, is
M=512 (a further doubling of symbol length, so +3 dB of symbol energy) rather
than repetition, plus K=9 for a free ~0.4 dB at no rate cost. Longer frames do
not improve reliability — the variation is slower than a frame — but they do
amortise the ~1.2 s of head and sync, so they raise the rate at unchanged
reliability. Candidate to test next: **M=512, K=9, 24-30 s frames, about
20-23 bit/s**.

## What is not established

- **The 95% decode target at any rate.** 28.7 bit/s was measured at 69%. No rate
  has yet been shown to hold 95%, and demonstrating one needs about 59
  consecutive successes with no failures on a single configuration.
- **Longer frames at M=256.** A 24 s frame would amortise the ~1.2 s of head and
  sync over twice the payload, worth roughly 15% more net rate. Untested.
- **Anything about a different path.** Every number here is one radio pair, one
  frequency, one antenna path, one afternoon, with the transmitter at minimum
  power. None of it is a claim about HF generally.
- **Whether the level swing is propagation or the transmitter.** The IC-705 was
  observed to weaken and then stop delivering usable power entirely earlier the
  same day, which is why several hours of measurements had to be discarded. The
  10 dB frame-to-frame swing may be partly a sagging transmitter rather than the
  channel.
- **Sub-orthogonal tone spacing.** The neighbour-error result suggests the
  detector is not spacing-limited, so packing tones closer than one symbol rate
  might buy rate. Not tested.
