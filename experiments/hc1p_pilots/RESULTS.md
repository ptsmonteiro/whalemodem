# HC1P: pilot re-anchoring inside HC1W's frame

## Question

`docs/MODES.md` records HC1W as the weakest fading performer in the HF ladder
-- moderate Watterson not passed at 20 dB -- despite carrying the most robust
constellation and code in the table (QPSK, rate 1/2, K=9). HF8 is the same
5 s length at 8PSK rate 2/3, has a pilot every 10 symbols, and passes quiet
Watterson at 18 dB.

Is that HC1W's 5 s frame length, or what it does inside the frame?

Answer: inside the frame, and specifically the soft-bit weights. Frame length
was held fixed at 5.015 s in every arm measured here and never needed to move.

## Method

`waveform.py` parameterises HC1W's payload path. Geometry, acquisition,
frequency and timing are imported from `whale/modes/hc1w.py` rather than
copied, so every arm shares them bit for bit. Pilots go into payload slots as
VF6 does, so `TOTAL_SYMBOLS` stays 365 and capacity is the only thing that
moves.

The `baseline` arm transmits audio **bit-identical** to `HC1W.encode()`
(`test_waveform.py`), so it is a live control, not a recorded number. It
reproduced the shipped figures on three independent channels: 92/100 at 6 dB
AWGN against the documented 90/100, and 76/100 at 20 dB moderate Watterson
against the documented 75/100.

Delivery comes from `whale.qualification.run_frame_trial`, the same trial path
`scripts/benchmark_simulated_channels.py` uses. 100 trials per point. All arms
at one point see identical channel realisations.

## Result

`mid_latitude_moderate`, delivered frames per 100 (`results_moderate.json`):

| Arm | Payload | 8 dB | 12 dB | 16 dB | 20 dB | 24 dB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline (= HC1W) | 1005 B | 0 | 18 | 51 | 76 | 81 |
| diff-s16 | 941 B | 1 | 70 | 97 | 98 | 99 |
| diff-s24 | 961 B | 5 | 77 | 98 | 98 | 99 |
| diff-s32 | 973 B | 1 | 67 | 94 | 96 | 97 |
| diff-s48 | 984 B | 3 | 49 | 89 | 92 | 100 |
| cohe-s16 | 941 B | 18 | 75 | 85 | 90 | 93 |
| cohe-s24 | 961 B | 17 | 72 | 84 | 88 | 92 |
| cohe-s32 | 973 B | 7 | 49 | 75 | 75 | 79 |
| cohe-s48 | 984 B | 3 | 22 | 29 | 30 | 34 |

`diff-s16` delivers 97/100 at 16 dB where HC1W delivers 51/100 -- CI95
[0.91, 0.99] against [0.41, 0.61]. HC1W's error floor near 80% is gone.

`mid_latitude_disturbed` (`results_disturbed.json`, `results_disturbed_high.json`):

| Arm | Payload | 12 dB | 16 dB | 20 dB | 24 dB | 28 dB | 32 dB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 1005 B | 0 | 4 | 9 | 15 | 19 | 14 |
| diff-s16 | 941 B | 7 | 35 | 53 | 79 | 70 | 73 |
| diff-s24 | 961 B | 4 | 35 | 43 | 58 | 54 | 58 |
| diff-s32 | 973 B | 4 | 16 | 26 | 43 | | |
| diff-s48 | 984 B | 0 | 13 | 16 | 36 | | |

Nothing passes disturbed: every arm hits an irreducible floor that added SNR
does not lift. `diff-s16` reaches ~70-79% where HC1W reaches ~15%.

`mid_latitude_quiet` (`results_quiet.json`) and AWGN (`results_awgn.json`)
are in the JSON. Quiet: baseline 91 at 14 dB, `diff-s16` 98. AWGN 6 dB:
baseline 92, `diff-s16` 82 -- the differential arms pay their capacity on a
channel with nothing to track, and the coherent arms gain ~0.5 dB there.

## Which mechanism

Two things ride on the pilots. `-blind` transmits the pilot slots as
differentially-encoded PN, spending the capacity while withholding the known
absolute reference; `-flatweight` keeps the anchors but holds HC1W's
header-derived soft-bit weights for the whole frame
(`results_mechanism.json`, 16 dB):

| Arm | Anchors | Tracked weights | 12 dB | 16 dB | 20 dB |
| --- | --- | --- | ---: | ---: | ---: |
| baseline | no | no | 12 | 48 | 68 |
| diff-s24-blind | no | no | 20 | 61 | 85 |
| diff-s24-flatweight | yes | no | 22 | 59 | 76 |
| diff-s24 | yes | yes | 67 | 92 | 98 |

**The time-varying weights are the mechanism.** Anchoring alone is worth about
11 points at 16 dB; the weights on top are worth another 33. The pilots matter
because they are a mid-frame measurement of each carrier's health, not because
they are a phase reference.

That is what the phase-drift diagnostic says too: the pilots measure a median
12 radians of drift across the frame -- nearly two rotations of the header's
reference. Differential detection in time absorbs that, which is why the
coherent arms never win under fading and `cohe-s48` collapses to 29/100:
coherent detection must reconstruct that drift by interpolation, and cannot.

HC1W's `_eq.carrier_weights(channel.snr_db)` is computed from the 13-symbol
header and applied to all 352 payload symbols. A carrier that is clean at the
header and sits in a null three seconds later keeps its confident weight and
feeds the Viterbi decoder assured noise. Under a 0.5 Hz spread with a
coherence time of order 2 s against a 4.8 s payload, that is what the channel
does.

## Recommendation

**`diff-s16`**: differential detection kept, pilots every 16 symbols
(0.21 s), driving time-varying per-carrier soft-bit weights. 941 B, a 6.4%
capacity cost, at HC1W's own 5.015 s airtime.

Stride 16 over 24 on the disturbed evidence: the two are tied at moderate
(97 vs 98, CI overlapping) and 16 is decisively better at disturbed (79 vs 58
at 24 dB). Density stops paying by 32 and is clearly too sparse at 48.

Keep differential detection. HC1W's choice was right; only the weights were
missing.

## Loose end: the interleaver

HC1W's multiplicative stride 811 against its 16,192-bit grid spreads one OFDM
symbol's 46 bits as little as 20 positions apart in the code, ranking 3,284th
of 3,519 valid strides by that measure. Substituting stride 4375 at unchanged
capacity measured better -- 55 vs 48 at 16 dB, 81 vs 68 at 20 dB
(`results_mechanism.json`).

But the same metric does not generalise: applying its best strides to the
pilot arms made them *worse*, `diff-s24-i5633` scoring 35 against
`diff-s24`'s 77 at 12 dB (`results_final_moderate.json`). So the min-gap
measure is not a validated design rule and the 4375 result is an unexplained
observation, not a recommendation. It is recorded because a free improvement
at zero capacity cost is worth someone's attention, not because it is
understood.

## Reproducing

    python -m pytest experiments/hc1p_pilots/test_waveform.py -q
    python experiments/hc1p_pilots/sweep.py --model watterson \
        --watterson-preset mid_latitude_moderate --points 8 12 16 20 24 \
        --trials 100 --out results_moderate.json
    python experiments/hc1p_pilots/summarise.py results_*.json

One correction is recorded in the code: an early `sweep.py` sent variants to
worker processes as `(stride, detection)` only, so `-flatweight` and `-blind`
arms were rebuilt as their plain counterparts and both ablations read as "no
difference". `test_waveform.py` now round-trips every arm through
`dataclasses.astuple` and asserts the ablation flags change the transmitted
grid. Any ablation reporting byte-identical counts should be disbelieved
first.
