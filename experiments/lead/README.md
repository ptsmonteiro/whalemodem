# A musical lead — one head that burns through, labels the frame, and times it

Experimental. Nothing in `whale/` imports any of this, no mode uses it, and
no framing has been changed. `lead.py` imports `whale.rx_audio` and
`whale.channel` only to be measured through the production receive path.

## What it replaces

Every mode today sends a throwaway head: one block repeated until the
requested guard is filled, so the receiving station's squelch and ALC have
something to destroy other than the frame. `whale/dsp/head.py` then counts
identical blocks backwards to learn how much died, and `ADAPTIVE_TIMING.md`
feeds that back into the next transmission. The audio carries no
information, and because every block is identical the measurement is
ambiguous in exactly the way that document records under
"weak-signal ambiguity".

This replaces it with a repeating **arpeggio** whose notes name the frame
behind it. One mechanism for every mode:

- it burns through, because it is one tone at a time — constant envelope,
  and a hard-clipped sine keeps its fundamental;
- it says what is coming, so the decoder does not infer the mode from
  protocol state;
- it is self-indexing, so the surviving fragment gives the leading loss and
  the frame start together;
- it hands every mode a carrier-offset estimate, which HC1 currently has to
  build for itself out of cyclic-prefix correlation.

```
lead.py         the waveform and the detector
test_lead.py    software invariants — no channel, no hardware
screen_lead.py  the AWGN / fading / clipping screen that produced RESULTS.md
RESULTS.md      the numbers, and the two designs that had to be thrown away
```

Run:

    python experiments/lead/screen_lead.py --trials 200 \
        --snr -28 -26 -24 -22 -20 -16 --out logs/scratch/lead/awgn.json
    python experiments/lead/screen_lead.py --false-alarm --trials 200
    python -m pytest experiments/lead/test_lead.py

## The shape

    | vamp | vamp | vamp | ... | vamp | cadence | frame
      ^ blackout eats from here          ^ this is where the frame starts

**The vamp** is an eight-note arpeggio, one of eight in the alphabet, one
per frame type. It repeats for the whole requested guard. It is what the
blackout is expected to eat, it carries the label, and counting how many
whole cycles arrived is the leading-loss measurement.

**The cadence** is one fixed eight-note closing figure, shared by every
label, always the last thing before the frame's first sample. Its
correlation peak *is* the frame start.

| Property | Value |
| --- | --- |
| Note | 512 samples at 12 kHz — 42.67 ms, 23.4375 Hz per bin |
| Notes | 15, a two-octave just major scale on bin 24: 562.5–2250 Hz |
| Cycle | 8 notes, 341 ms |
| Minimum lead | 1 vamp cycle + 1 cadence = 683 ms |
| Alphabet | 8 labels, minimum distance 5 of 8 over all rotations |
| Modulation | Non-coherent, one note at a time, constant envelope (crest 1.41) |
| Offset search | ±25 Hz, refined on a repeated note pair |
| Detector cost | 6 ms for a 6.3 s capture |

## Why a just scale on integer bins

Bins are integers and just intervals are small-integer ratios, so a justly
tuned scale built on bin 24 lands *exactly* on bins — 24, 27, 30, 32, 36,
40, 45, 48 is a just major scale, and doubling walks octaves. Equal
temperament would have to be rounded onto the bin grid, and at the bottom
of the band that rounding is 40 cents. The scale is in tune because it is
on the grid, not in spite of it.

The grid is the lead's own. It has nothing to do with any mode's payload
grid, and the lead is decoded before the mode is known.

Everything is sized on the **12 kHz decode rate**, not the 48 kHz transmit
rate: production receive audio is decimated by `whale/rx_audio.py` before
any decoder sees it, so 12 kHz sets the achievable frequency resolution.
The transmitter emits the same note as 2048 samples at 48 kHz, the same bin
index, a whole number of cycles either way.

## Status

Screened, not qualified. It has been measured against AWGN, three Watterson
presets, hard clipping and carrier offset, through the production
decimation. It has not been run over the air, and integrating it would
touch `whale/dsp/head.py`, every mode's acquisition, `ADAPTIVE_TIMING.md`'s
head formula and `FRAMING.md` — none of which is attempted here.

See `RESULTS.md`.
