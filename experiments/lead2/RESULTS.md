# One figure — results

All figures are from `screen_lead2.py`, 150 trials per point (200 for the
false-alarm measurement), waveform SNR referenced to the lead itself across
the full 48 kHz Nyquist band — the convention `FRAMING.md` uses when it says
HC0 decodes to −16 dB. Every trial is transmitted at 48 kHz, impaired at
48 kHz, and decimated to 12 kHz by `whale.rx_audio.downsample` before the
detector sees it. The lead is followed by an HC0-shaped payload waveform
rather than by silence, so a false lock inside the frame counts as a failure.

"label+timing" is the fraction of trials that got **both** the right frame
type and a frame start within half a note. Anything less is a failure even
if the label was right; the label-only rate is carried alongside in the JSON
and is one to five points higher.

Raw output is in `logs/scratch/lead2/`.

## The headline

**0.171 s of lead reads the label and locates the frame 97.3% of the time on
`mid_latitude_moderate` at −16 dB — the previous experiment's number, at a
quarter of its airtime.**

This is the shorter lead at the same robustness, which the brief named as the
single most valuable outcome. It is *not* also the lower SNR: in AWGN this
gives out at −20 dB where the 0.68 s musical lead ran to −22 dB. That is the
cheaper thing to spend, because there was already 6 dB of margin below where
HC0's payload dies and now there is 4 dB.

| | `experiments/lead/` | this | |
| --- | ---: | ---: | --- |
| Minimum lead | 0.683 s | **0.171 s** | −75% |
| `mid_latitude_moderate`, −16 dB | 97.3% | 97.3% | same |
| `high_latitude_disturbed`, −16 dB | 100% | 97.3% | −2.7 |
| AWGN, 100% label+timing to | −22 dB | −18 dB | −4 |
| AWGN, ≥98% to | −24 dB | −20 dB | −4 |
| Cost on HC0's 3.38 s keying | +20% | **+5.0%** | |
| Cost on HC1's 0.695 s frame | +98% | **+24.6%** | |
| Detector CPU | 6 ms / 6.3 s | 9.7 ms / 4.8 s | 2× per second |

Given the same 0.68 s of airtime the same mechanism is about 2 dB *better*
than the musical lead rather than merely as good — 16 notes of 512 samples
runs 100% to −24 dB and 96.0% at −26 dB, against 99.0% and 76.5%. So the
airtime saving is not bought by a weaker design; it is bought by spending one
figure where the previous design needed two.

## Where the saving comes from

`experiments/lead/` needs two figures because it uses two: a repeating
**vamp** carries the label and a fixed **cadence** marks the end. Both must
arrive, so its floor is their sum.

They need not be two. A sequence long enough to be *located* is already long
enough to be *named*: locating it costs the whole figure, while naming it
costs three bits out of the roughly `8 × log2(20) = 34` a figure holds. So
there is one figure per frame type here, ending exactly at the frame's first
sample, and one joint argmax over (carrier offset, label, position) returns
the label, the frame start and the offset together.

The repeated **burn** in front of it is universal — identical for every mode
and every label — and exists only to be eaten and counted. It is not needed
to read the label or to find the frame, which is what lets the lead collapse
to one figure whenever `ADAPTIVE_TIMING.md`'s feedback says the path is
clean.

## The geometry sweep

Two parameters, swept together on `mid_latitude_moderate` at −16 dB, 150
trials each. Note duration is the columns, notes per figure the rows.

| notes × samples | 128 (10.67 ms) | 256 (21.33 ms) | 512 (42.67 ms) |
| ---: | ---: | ---: | ---: |
| **8** | 78.7% (0.085 s) | **97.3% (0.171 s)** | 96.0% (0.341 s) |
| **16** | 94.0% (0.171 s) | 97.3% (0.341 s) | 100% (0.683 s) |
| **32** | 92.0% (0.341 s) | 100% (0.683 s) | 100% (1.365 s) |

Two things fall out, and neither was obvious in advance.

**Lengthening the note buys almost nothing; adding notes buys a lot.** Along
the `8`-note row, doubling the note from 256 to 512 samples doubles the
airtime and the energy and moves 97.3% to 96.0% — nothing, within the ±2% a
150-trial point carries. Down the 0.171 s diagonal (128×16 against 256×8) the
same airtime and the same energy split into twice as many, half-as-strong
notes and loses three points. So there is a per-note energy floor, and above
it the figure is not energy-limited at all.

**128-sample notes are below that floor and were rejected.** They are the
shortest note this tone set can carry — 93.75 Hz spacing is exactly the
reciprocal of 10.67 ms, so nothing shorter stays orthogonal under
non-coherent detection — and an 85 ms lead is a tempting number. It reads
78.7%. Worse, adding notes does not rescue it: 128×32 is 0.341 s of airtime
for 92.0%, which 256×8 beats in half the time. A note that cannot be detected
reliably on its own does not become one by being repeated.

256 samples by 8 notes is where the two pressures meet. It also has the best
*absolute* timing in the table: its ±64-sample p95 is 5.3 ms, against the
512-sample geometry's nominally perfect score against a tolerance four times
looser.

## AWGN

| SNR | label+timing | score median | score p05 |
| ---: | ---: | ---: | ---: |
| −16 dB | 100.0% | 0.249 | 0.216 |
| −18 dB | 100.0% | 0.201 | 0.165 |
| −20 dB | 98.7% | 0.158 | 0.120 |
| −22 dB | 78.0% | 0.120 | 0.094 |
| −24 dB | 29.3% | 0.103 | 0.090 |
| −26 dB | 8.7% | 0.100 | 0.089 |

## False alarm

With no lead at all — only noise and an HC0 frame — the detector still
returns a label, because the statistic is an argmax and not a test. Over 200
trials it produces a **score of 0.137 at p99 and 0.141 at maximum**, and a
label margin of 0.043.

A threshold at 0.14 is therefore where one would sit, and the honest
consequence is worth stating rather than burying:

- In AWGN it costs nothing to −18 dB (p05 0.165) and passes the working floor
  at about −18 to −20 dB, which agrees with the sweep.
- **Under fading at −16 dB it rejects about 5% of leads that were read
  correctly** (`mid_latitude_moderate` p05 0.121, `mid_latitude_disturbed`
  p05 0.109). The 97.3% argmax accuracy and the 95% threshold retention are
  different numbers and both are real.

The margin is not usable as the test here — 0.043 of lead-free margin against
a real lead's 0.11 at −16 dB is a much worse separation than the score gives.
That differs from the musical lead, where the vamp/cadence split made margin
the natural statistic; with one figure the score is.

## Fading

| Preset | spread / delay | −16 dB | −20 dB | −22 dB |
| --- | --- | ---: | ---: | ---: |
| `mid_latitude_moderate` | 0.5 Hz / 1 ms | 97.3% | 80.0% | 66.0% |
| `mid_latitude_disturbed` | 1.0 Hz / 2 ms | 90.7% | 66.7% | 46.0% |
| `high_latitude_disturbed` | 30 Hz / 7 ms | 97.3% | 71.3% | 41.3% |

**The previous experiment's "slow fading is worse than fast fading" does not
transfer, and reverses.** There, `mid_latitude_disturbed` beat
`mid_latitude_moderate` because a 0.68 s lead could sit entirely inside a
0.5 Hz fade and not inside a 1.0 Hz one. At 0.171 s that distinction is gone:
both fades far outlast the lead, so neither preset offers any time diversity
and what separates them is the delay spread — 2 ms against 1 ms, frequency
nulls every 500 Hz instead of every 1000 Hz. `high_latitude_disturbed`, with
7 ms of spread but 30 Hz of Doppler, gets several independent fades inside
171 ms and lands back at 97.3%. The design's exposure has moved from the time
axis to the frequency axis, which is the expected consequence of making it
four times shorter, and it is the reading of these three rows rather than
something separately proved.

`high_latitude_disturbed` also returns a carrier offset wrong by 28 Hz at
p95, and a frame start biased half a search step late. That much Doppler
swamps a phase-step estimate, exactly as the previous experiment found.

## Clipping, offset, blackout

| Impairment | −16 dB | −20 dB | −22 dB |
| --- | ---: | ---: | ---: |
| none | 100.0% | 98.7% | 78.0% |
| hard clip at 0.5× amplitude (near square wave) | 100.0% | 98.0% | 72.0% |
| carrier offset +14 Hz | 100.0% | 98.0% | 83.3% |
| blackout eats 1.37 s of a 1.54 s lead | 100.0% | — | 78.7% |

None of the three is measurably an impairment. Clipping is harmless for the
two reasons the previous experiment established and this one inherits: the
across-tone mean subtraction removes what a harmonic adds to every candidate
alike, and a spurious harmonic arrives where the sequence is not expecting
it, so it is rejected by *when* it comes rather than by how loud it is.
Carrying the label in the sequence rather than in tone identity is what buys
that.

The 14 Hz offset is searched rather than tolerated, and the search is one
column shift per hypothesis in a spectrum zero-padded to 2048 points, so the
whole ±25 Hz costs one FFT per window rather than one per hypothesis. It is
not free of consequences at −22 dB, where it is 5 points *better* than no
offset — that is trial noise, not a real gain.

The blackout case is the one that most resembles the failure
`ADAPTIVE_TIMING.md` records: eight burn repeats transmitted, six destroyed,
so 1.37 s of a 1.54 s lead never reaches the receiver. Label, frame start and
**burn count are all exact at −16 dB**.

## Leading loss: the count and its sign

The brief asked for the error and its sign, because early is safe — it
lengthens the next head — and late is not.

**The count was never late. Not once, in any of the 3,450 successful trials
across every impairment and every preset**, is `burns_observed` greater than
the number of repeats that survived. The error distribution is one-sided by
construction: the count walks backward from the located figure while each
repeat still scores above `RUN_FRACTION` of the figure's own level, so noise
can only stop it early.

| Condition | median error | fraction early | fraction late |
| --- | ---: | ---: | ---: |
| AWGN −16 dB | 0 | 0% | 0% |
| AWGN −20 dB | 0 | 22% | 0% |
| AWGN −22 dB | −5 | 67% | 0% |
| `mid_latitude_moderate` −16 dB | −0.5 | 50% | 0% |
| `mid_latitude_disturbed` −16 dB | −2 | 66% | 0% |
| blackout, −16 dB | 0 | 0% | 0% |

The bias grows as the SNR falls, which is the same weak-signal conservatism
`ADAPTIVE_TIMING.md` records today. What is different is that it no longer
costs the frame start: the count is derived *from* the located figure and
cannot move it. In the pathology that document reports on the STA2→STA1 leg,
a present-but-weak head read as 0–128 ms and drove the transmitted head to
the one-second ceiling; here the same weak path still under-counts, but the
frame is still found, still labelled, and the under-count is bounded by the
guard actually sent rather than being confused with a missing frame.

## The carrier offset, as a bonus

It is close to free — the joint argmax was searching offset anyway, and the
refinement is one FFT of the figure — so the brief's condition is met on
cost. The accuracy is the part that needs quantifying, and it is worse than
the 0.68 s lead's:

| Condition | offset error p95 |
| --- | ---: |
| AWGN −16 dB | 4.6 Hz |
| AWGN −20 dB | 7.9 Hz |
| `mid_latitude_moderate` −16 dB | 8.0 Hz |
| `mid_latitude_disturbed` −16 dB | 7.4 Hz |
| `high_latitude_disturbed` −16 dB | 28.3 Hz |
| 512×16 (0.683 s), AWGN −22 dB | 2.9 Hz |

That is a direct consequence of the shorter figure: the phase step is
measured over one repeated note pair, and a 256-sample note gives half the
lever arm a 512-sample one does. **HC1 can use this as a coarse estimate — 8 Hz
against its ±46.875 Hz tolerance is comfortable — but it should keep its own
cyclic-prefix estimator**, both because 28 Hz under high-latitude Doppler is
not usable and because HC1 needs to undo the offset in the time domain before
its carriers stop leaking into each other, which wants better than 8 Hz. The
lead saves HC1 its *coarse* stage, not its fine one.

The unwrapping matters and is the previous experiment's bug, kept fixed. A
note's phase advances by the offset times its own duration, so the phase-step
estimate wraps at half the *note rate* — 23.4 Hz at 256 samples — while a
tone is confused with its neighbour only at half the *tone spacing*,
46.875 Hz. Different quantities; the coarse search unwraps the fine estimate
rather than the fine estimate being trusted alone.

## What did not work

**128-sample notes.** Above; the shortest orthogonal note on this grid, an
85 ms lead, and 78.7% on `mid_latitude_moderate` at −16 dB. Rejected because
more of them does not fix it.

**The frequency-diversity constraint does less than the design claimed for
it.** The argument for hopping consecutive notes at least six tone steps
(562.5 Hz, wider than the coherence bandwidth of a 1 ms delay spread) is that
frequency diversity should buy back the time diversity a four-times-shorter
lead gives up. Rebuilding the alphabet with the constraint relaxed and
measuring it says otherwise:

| minimum hop | `mid_latitude_moderate` −16 dB | `mid_latitude_disturbed` −16 dB |
| ---: | ---: | ---: |
| 1 step (94 Hz) | 94.0% | 91.3% |
| 3 steps (281 Hz) | 94.7% | 90.0% |
| 6 steps (563 Hz) | 97.3% | 90.7% |

Three points on `mid_latitude_moderate`, which is at the edge of what 150
trials resolves, and **nothing at all on `mid_latitude_disturbed`**, whose
2 ms spread is where a frequency-diversity argument should pay best. The
constraint is kept — it is free, it is not negative anywhere, and the
`mid_latitude_moderate` column is at least suggestive — but the honest
reading is that it is not what makes the short lead work. What makes the
short lead work is simply that 171 ms of a 1 ms-spread path is not very often
inside a deep fade, and that eight 21 ms notes carry far more redundancy than
three bits of label require.

**A figure allowed to resemble the burn.** This was a bug rather than a
design, and it is recorded because of how it presented. The alphabet is
chosen by requiring low agreement between every pattern and every window of
the on-air stream `burn burn figure`, excluding the alignments where a match
is the signal rather than an alias. Those exclusions are different for
different patterns — the burn legitimately matches at two of the windows, a
figure at one, a foreign label at none — and the first implementation used
the burn's exclusion set for all of them. The effect was not a small loss of
margin. It admitted figures that agreed with the burn everywhere, and the
detector then locked cleanly and confidently onto the **first burn repeat**
instead of the frame: label correct, score 0.5, frame start eight figures
early. The screen read 12–27% across every geometry, which looked like a
failed design rather than a failed sieve. What gives it away is that the
label was right; a genuinely confused detector gets the label wrong too. The
fixed sieve gives an alphabet in which no wrong alignment agrees in more than
**1 note of 8**, and the geometry sweep came back at 78–100%.

## What this does not answer

- No over-the-air run. Everything here is simulated.
- The 0.171 s minimum is still a real new cost on a clean path, where
  `ADAPTIVE_TIMING.md`'s feedback can currently drive the head down to a
  10 ms guard. It is 5.0% on HC0's keying and 24.6% on HC1's frame, against
  20% and 98% before. Whether that trade is net positive is a link-level
  measurement, not a waveform one, and it has not been made — but at 5% on
  the control mode it is much closer to obviously affordable than it was.
- Interference, impulse noise and sample-clock error were not screened.
- The threshold's 5% rejection of correctly-read leads under fading is stated
  but not designed around. A confidence-aware feedback rule of the kind
  `ADAPTIVE_TIMING.md` asks for would want to treat "read but below
  threshold" as its own outcome, and nothing here does that.
- Eight labels is the budget the brief set and the alphabet meets it with
  distance to spare. How far it stretches — a sixth mode, a distinguished
  control keying — was not measured.
