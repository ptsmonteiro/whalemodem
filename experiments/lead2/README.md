# One figure — a mode-independent lead in a quarter of the airtime

Experimental. Nothing in `whale/` imports any of this, no mode uses it, no
framing has changed. `lead2.py` imports `whale.rx_audio` and `whale.channel`
only so it can be measured through the production receive path.

## What it replaces

Every mode today sends a throwaway head: one block repeated until the
requested guard is filled, so the receiving station's squelch and ALC destroy
that instead of the frame. `whale/dsp/head.py` counts identical blocks
backwards to learn how much died and `ADAPTIVE_TIMING.md` feeds that back
into the next transmission. The audio carries no information, and because
every block is identical the measurement is ambiguous in exactly the way that
document records under "weak-signal ambiguity".

`experiments/lead/` replaced that head with a repeating arpeggio plus a fixed
closing cadence, and measured it at **0.68 s**. This experiment starts from
that result and asks one question about it.

## The question

`experiments/lead/`'s floor is two eight-note cycles because it uses two
different figures for two different jobs: a repeating **vamp** carries the
label and a fixed **cadence** marks the end. Both have to arrive, so the
minimum lead is the sum.

They need not be two figures. A sequence long enough to be *located*
unambiguously is already long enough to be *named*, because locating it costs
the whole figure while naming it costs only three of the roughly thirty-four
bits the figure holds. So here there is **one figure per frame type**, ending
exactly at the frame's first sample, and one correlation bank returns the
label, the frame start and the carrier offset together.

```
| burn | burn | burn | ... | burn | figure(label) | frame
  ^ blackout eats from here          ^ correlation peak = frame start
```

The **burn** is one universal pattern, identical for every mode and every
label, repeated to fill whatever guard `ADAPTIVE_TIMING.md` currently asks
for. It is what the blackout is expected to eat, and counting how many whole
repeats arrived is the leading-loss measurement. It is *not* needed to read
the label or to find the frame — which is the point: on a clean path, where
the feedback has driven the guard to its minimum, the lead collapses to one
figure.

## The result

**0.171 s, against the previous 0.68 s, at the same measured robustness.**

| | `experiments/lead/` | this |
| --- | ---: | ---: |
| Minimum lead | 0.683 s | **0.171 s** |
| `mid_latitude_moderate`, −16 dB | 97.3% | 97.3% |
| AWGN, 100% down to | −22 dB | −18 dB |
| Cost on HC0's 3.38 s keying | +20% | **+5%** |
| Cost on HC1's 0.695 s frame | +98% | **+25%** |

The task asked which of the two axes was achieved. This is the shorter lead
at the same robustness, not the lower SNR: AWGN gives out 2 dB sooner, which
is the cheaper thing to spend, since there was already 6 dB of margin below
where HC0's payload dies.

See `RESULTS.md` for the sweep that chose the geometry, the false-alarm
threshold, the leading-loss error and its sign, and the two things that did
not work.

## The geometry

Sized on the **12 kHz decode rate**, because production receive audio is
decimated by `whale/rx_audio.py` before any decoder sees it, so 12 kHz is
what sets the achievable frequency resolution. Sizing at the 48 kHz transmit
rate would be sizing against a resolution the detector never has.

| Property | Value |
| --- | --- |
| Note | 256 samples at 12 kHz — 21.33 ms, 46.875 Hz per bin |
| Tones | 20, 93.75 Hz apart, 562.5–2343.75 Hz |
| Figure | 8 notes, 170.7 ms |
| Minimum lead | one figure — 170.7 ms |
| Alphabet | 8 labels; no wrong alignment agrees in more than 1 note of 8 |
| False-alarm threshold | score 0.14 (lead-free p99 0.137, max 0.141) |
| Modulation | Non-coherent, one tone at a time, constant envelope (crest 1.41) |
| Offset search | ±25 Hz, refined on a repeated adjacent note pair |
| Detector cost | 9.7 ms for a 4.8 s capture |

93.75 Hz is the coarsest useful tone spacing: it is what keeps even a
128-sample note orthogonal under non-coherent detection, since orthogonality
needs a spacing of at least one over the note duration. 20 of them fit
between the skirts of a 2.4 kHz SSB data filter.

Consecutive notes are required to hop at least six tone steps — 562.5 Hz —
on the argument that frequency diversity should buy back the time diversity a
four-times-shorter lead gives up. `RESULTS.md` records that this constraint
measures as worth about three points on `mid_latitude_moderate` and nothing
on `mid_latitude_disturbed`, so it is kept for being free rather than for
being load-bearing. It is also where `experiments/lead/`'s "slow fading is
worse than fast fading" finding stops transferring: at 0.171 s both mid-
latitude presets outlast the lead entirely, and the ordering between them
reverses.

## Files

```
lead2.py         the waveform and the detector
test_lead2.py    software invariants — no channel, no hardware
screen_lead2.py  the AWGN / fading / clipping / offset screen
RESULTS.md       the numbers, and what did not work
```

Run:

    python experiments/lead2/screen_lead2.py --trials 150 \
        --snr -26 -24 -22 -20 -18 -16 --out logs/scratch/lead2/awgn.json
    python experiments/lead2/screen_lead2.py --false-alarm --trials 200
    python experiments/lead2/screen_lead2.py --trials 150 --snr -16 \
        --note-samples 128 256 512 --figure-notes 8 16 32 \
        --watterson mid_latitude_moderate
    python -m pytest experiments/lead2/test_lead2.py

Use the Python that has numpy and scipy; `whale.channel` needs 3.11.

## Status

Screened, not qualified. Measured against AWGN, three Watterson presets, hard
clipping, a 14 Hz carrier offset and a blackout, through the production
decimation. Not run over the air. Integrating it would touch
`whale/dsp/head.py`, every mode's acquisition, `ADAPTIVE_TIMING.md`'s head
formula and `FRAMING.md` — none of which is attempted here.
