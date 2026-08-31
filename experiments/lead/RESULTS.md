# A musical lead — results

All figures are from `screen_lead.py`, 150–200 trials per point, waveform
SNR referenced to the lead itself across the full 48 kHz Nyquist band —
the same convention `FRAMING.md` uses when it says HC0 decodes to −16 dB.
Every trial is transmitted at 48 kHz, impaired at 48 kHz, and decimated to
12 kHz by `whale.rx_audio.downsample` before the detector sees it. The lead
is followed by an HC0-shaped payload waveform rather than by silence, so a
false lock inside the frame counts as a failure.

"label+timing" is the fraction of trials that got **both** the right frame
type and a frame start within half a note. Anything less is a failure even
if the label was right.

## The headline

**A 0.68 s lead reads correctly to −22 dB in AWGN — 6 dB below where HC0's
payload gives out — and costs 0.68 s of head that every keying is already
spending.**

The uniform mechanism the design asked for is affordable. It does not need
to be sized per mode.

## AWGN

| Lead | SNR | label+timing | margin | cadence |
| --- | ---: | ---: | ---: | ---: |
| 0.68 s | −16 dB | 100.0% | 0.313 | 0.386 |
| 0.68 s | −20 dB | 100.0% | 0.227 | 0.286 |
| 0.68 s | −22 dB | 100.0% | 0.180 | 0.234 |
| 0.68 s | −24 dB | 99.0% | 0.132 | 0.184 |
| 0.68 s | −26 dB | 76.5% | 0.070 | 0.142 |
| 0.68 s | −28 dB | 35.5% | 0.020 | 0.122 |

A second vamp cycle (1.02 s) is worth nothing measurable: 78.5% against
78.0% at −26 dB. The label decision was never the limit — see below.

**False alarm.** With no lead at all, only noise and an HC0 frame, the
detector still returns a label, because the statistic is an argmax and not
a test. What it returns over 200 trials is a cadence score of 0.147 at p99
and a label margin of 0.069. A real lead at −22 dB scores 0.234 and 0.180.
A threshold near 0.18 on the cadence separates them and puts the working
floor at about −24 dB, which agrees with the sweep.

## Fading

| Preset | SNR | label+timing |
| --- | ---: | ---: |
| mid_latitude_moderate | −16 dB | 97.3% |
| mid_latitude_moderate | −20 dB | 86.0% |
| mid_latitude_moderate | −22 dB | 77.3% |
| mid_latitude_disturbed | −16 dB | 99.3% |
| mid_latitude_disturbed | −20 dB | 96.7% |
| mid_latitude_disturbed | −22 dB | 90.7% |
| high_latitude_disturbed | −16 dB | 100.0% |

`mid_latitude_disturbed` beating `mid_latitude_moderate` is not noise and
is worth stating: moderate has 0.5 Hz of Doppler spread against
disturbed's 1.0 Hz, and a slower fade is *worse* here because a 0.68 s
lead can sit entirely inside one. The lead's enemy is a fade longer than
itself, not a fast one.

`high_latitude_disturbed` (30 Hz of spread) reads the label and the frame
start perfectly and returns a **carrier offset wrong by 17–19 Hz**. That
much Doppler swamps a phase-step estimate. A mode taking the offset from
the lead needs to know it can be this wrong, and to keep its own estimator
for the case.

## Clipping and offset

| Impairment | SNR | label+timing |
| --- | ---: | ---: |
| Hard clip at 0.5× amplitude (near square wave) | −22 dB | 100.0% |
| Hard clip at 0.5× amplitude | −24 dB | 98.7% |
| Carrier offset +14 Hz | −22 dB | 100.0% |
| Carrier offset +14 Hz | −24 dB | 97.3% |
| Blackout eats 1.7 s of a 3.1 s lead | −22 dB | 100.0% |

**The harmonic hazard did not materialise.** A just scale is maximally
aligned with distortion products — bin 24's second and third harmonics are
bins 48 and 72, both notes in the scale — so clipping was expected to
forge legal notes. It does not measurably matter, for two reasons the
design already had: the across-note mean subtraction removes what a
harmonic adds to every candidate alike, and a spurious harmonic lands at a
position where the sequence is not expecting it, so it is rejected by
*when* it arrives rather than by how loud it is. Carrying the label in the
sequence rather than in note identity is what bought this.

**Leading loss.** The cycle count is exact in AWGN at −20 dB and below, and
biased *early* under fading — median −1 cycle at −20 dB on
`mid_latitude_moderate`. Early is the safe direction: it lengthens the next
head rather than shortening it. This is the same weak-signal conservatism
`ADAPTIVE_TIMING.md` records today, except that it no longer costs the
frame start, which the cadence now fixes independently.

## Two designs that had to be thrown away

Both were wrong in the same way — they tried to infer the end of the lead
instead of marking it — and both are recorded because the second one looked
like it worked.

**Argmax on the correlation peak.** A repeated arpeggio scores identically
at every cycle boundary inside itself, so the peak is not the end. 7.5%
correct at −16 dB. This is the same ambiguity today's repeated head has;
being aperiodic *within* a cycle does not help across cycles.

**Run-length: walk forward while the score holds.** Correct in AWGN, and
wrong in a way that got worse as the SNR improved — 100% at −16 dB with a
one-cycle decision window, 0% with a four-cycle one, because a window
three-quarters full of real lead still scores well and the run walks past
the end. Splitting the statistic fixed that: accumulate for the label,
use a single-cycle edge for the boundary.

Then fading killed it anyway. On `mid_latitude_moderate` at −16 dB the
label was right in **120 of 120** trials while the frame start landed one
to six whole cycles early in 29 of them. A Watterson fade at 0.5 Hz of
spread lasts one to three seconds — several cycles — and a fade over the
last cycles of the lead is *indistinguishable from the lead having
stopped there*. Tolerating a gap of two quiet cycles recovered a third of
the failures and no more.

**The cadence is the fix.** The end is marked rather than inferred: the
vamp resolves onto one fixed closing figure whose correlation peak is the
frame start. `mid_latitude_moderate` at −16 dB went from 78% to 97%. It now
fails only when a fade covers the cadence itself — which is a fade over the
first moments of the frame, where the frame was lost regardless. That is
the right coupling. Run-length detection had the frame start failing while
the frame was still perfectly good.

## One bug worth keeping written down

A 14 Hz carrier offset — the bench pair's own figure — dropped detection to
46% at −20 dB and returned an offset estimate wrong by 25 Hz.

Two faults, one root. A note here is 42.67 ms, four times the reciprocal of
its own 23.4375 Hz spacing, so **the phase-step estimator wraps at 11.7 Hz
while a note is only confused with its neighbour at 23.4 Hz**. Those are
different quantities and the code had conflated them; 14 Hz wrapped to
−9.4 Hz. Meanwhile the offset scalloped the note's energy out of its
analysis bin.

Both are cured by searching for the offset rather than tolerating it. The
search costs nothing structural: a note is a pure tone, so its DFT
evaluated *at its own frequency* is full-amplitude whether or not that
frequency is an integer bin, and zero-padding by four puts a grid point
every 5.86 Hz. Nine hypotheses, one FFT per window, ±25 Hz of range, and
the coarse answer unwraps the phase-step refinement. Residual error is
1.3 Hz p95 at −16 dB in AWGN.

This also means **the lead hands every mode a carrier-offset estimate for
free**, which HC1 currently derives for itself from cyclic-prefix
correlation before it can undo the offset in the time domain.

## What this does not answer

- No over-the-air run. Everything here is simulated.
- The 0.68 s minimum is a real new cost on a *clean* path, where
  `ADAPTIVE_TIMING.md`'s feedback can currently drive the head down to a
  10 ms guard. On HC0's 3.38 s keying that is +20%; on HC1's 0.695 s frame
  it is +98%. Against that, the unambiguous leading-loss measurement should
  stop the pathology that document records on the STA2→STA1 leg, where a
  present-but-weak head read as 0–128 ms and drove the transmitted head to
  the 1 s ceiling. Whether that trade is net positive is a link-level
  measurement, not a waveform one, and it has not been made.
- Interference and impulse noise were not screened.
- The alphabet is greedily chosen, not optimal. Minimum distance is 5 of 8
  over all rotations, with 8 labels out of 15 notes; there is a lot of room
  left if a future mode ladder wants more codepoints.
