# Repeated HC0-grid frame signature

This directory retains the qualification experiment that selected the common
HF lead now implemented by `whale.modes.hf_lead`. Production does not import
the experiment; the fixed sequences and receiver live in `whale/`.

This experiment asks for the shortest common MFSK signature that is no less
robust than HC0, the most robust HF frame.  It therefore measures the paired
quantity

```text
P(signature misses | the following HC0 frame decodes)
```

rather than requiring the signature to work below HC0's own useful range.
Every trial sends a signature and a genuine coded HC0 frame through the same
channel realization.  A signature failure only counts against a geometry
when that HC0 payload passes its CRC.

The candidate uses HC0's exact non-coherent tone bank:

| Property | Value |
| --- | --- |
| Receive rate | 12 kHz |
| Hop | 128 samples, 10.67 ms |
| Tones | 16, spaced 93.75 Hz, 750–2156.25 Hz |
| Current repeated candidate | 12 hops, 128 ms (6 hops twice) |
| Modulation | one tone at a time, constant envelope |
| Labels | 2 balanced sequences: HC0 and HC1 |

The frozen six-symbol blocks are `(9, 6, 12, 15, 0, 3)` and
`(12, 3, 15, 6, 9, 0)`. The deterministic construction seed is `0x1285f`;
it is not a channel-test seed. Detector choices are screened starting at
`0x51ec7100`, while held-out validation starts at `0xa11da710`. Do not tune a
scorer or codeword on the latter range and then describe another run from the
same range as held out.

The 93.75 Hz spacing is exactly reciprocal to the hop duration, so all tones
are non-coherently orthogonal. On air, the selected label is the repeated
head block; there is no separate burn sequence. The minimum-duration screen
uses one repetition. Production uses the selected six-symbol block twice,
orders eligible HF decoders from the label hint, retains all of them as
fallbacks, and accepts only a checked frame.

This is deliberately an HF alphabet. A future FM lead can use a different
geometry and label budget behind the same `LeadFormat` boundary described in
`ARCHITECTURE.md`.

Run with the Python environment containing NumPy and SciPy:

```sh
python experiments/signature128/screen_signature128.py \
  --trials 1000 --hops 12 --snr -12 \
  --watterson mid_latitude_disturbed --seed 2703075088 \
  --scorer sum --candidate-limit 32
```

`pair-max` is retained as a reproducible rejected scorer. It independently
selects the stronger observation of each repeated symbol; the held-out pilot
below found no benefit.
