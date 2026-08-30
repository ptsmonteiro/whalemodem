# Paired signature/HC0 pilot results

Pilot run on 2026-08-30. These results size an experiment; they do not
qualify a new wire format. Raw JSON is under `logs/scratch/signature128/`.

The relevant outcome is a signature miss among trials where the genuine HC0
payload following it decoded and passed CRC. The signature and HC0 frame saw
one continuous realization of the same impairment.

## AWGN

At -16 dB, HC0 decoded almost every trial. The 80 ms candidate had one paired
miss; every candidate of 96 ms or longer had none.

| Signature | HC0 decoded | Paired misses |
| ---: | ---: | ---: |
| 80 ms | 146/150 | 1 (0.68%) |
| 96 ms | 146/150 | 0 |
| 112 ms | 149/150 | 0 |
| 128 ms | 148/150 | 0 |

At -18 dB HC0 itself decoded only one frame in the complete sweep, so that
point contains no useful evidence for choosing the signature duration. This
is the intended consequence of conditioning on HC0 rather than optimizing a
lead that precedes an undecodable frame.

## Moderate mid-latitude fading

The nominal -16 dB point was below HC0's useful range under this Watterson
preset: only 2--5 of 100 frames decoded, depending on signature duration and
therefore the fade phase at which the HC0 preamble began. The useful paired
comparison moved to -12 dB, where HC0 decoded 91--95%.

| Signature | HC0 decoded | Paired misses |
| ---: | ---: | ---: |
| 80 ms | 95/100 | 7 (7.37%) |
| 96 ms | 92/100 | 6 (6.52%) |
| 112 ms | 92/100 | 2 (2.17%) |
| 128 ms | 91/100 | 0 |

The result explains why matching HC0's AWGN floor is necessary but not
sufficient. HC0 obtains time diversity from a 3.3-second coded frame; a
sub-100 ms signature can fall wholly inside a slow fade even when the HC0
payload later succeeds.

## Disturbed fading and ranked candidates

The initial eight-label alphabet was the wrong architecture for an HF-only
lead: HF currently needs to distinguish HC0 and HC1, not spend robustness on
six unused labels. A balanced two-label alphabet reduced errors substantially.

A single global correlation maximum was also too strict. Some failures were
false locks deep inside the following frame; a production receiver rejects
those when the indicated frame fails CRC and tries the next ranked boundary.
With up to 32 ranked candidates on `mid_latitude_disturbed` at -12 dB:

| Signature | HC0 decoded | True signature absent from candidates |
| ---: | ---: | ---: |
| 128 ms | 438/500 | 1 (0.23%) |
| 144 ms | 440/500 | 2 (0.45%) |
| 160 ms | 440/500 | 2 (0.45%) |

The non-monotonic result is genuine: each length currently builds a different
codebook, so code quality matters more than adding one hop at this scale.

## Current assessment

**128 ms (eight 16 ms hops)** remains the best candidate and is 25% shorter
than 171 ms, but it has not yet met the strict claim of being as robust as
HC0: one of 438 decodable disturbed-fading trials lacked the true signature
in the top 32 candidates. A larger run would quantify that failure, not erase
it. The next work should improve the two-codeword construction/scorer or make
the robust control decoder an explicit low-confidence fallback.

Before integration, implement actual candidate/CRC confirmation, repeated
blocks and partial blackout, then repeat with at least 1,000 trials per useful
point across every Watterson preset, clipping and frequency offset.

## Exact HC0 modulation

Replacing the 62.5 Hz experimental grid with HC0's exact 16-tone bank moved
the useful duration below 128 ms. On `mid_latitude_disturbed` at -12 dB with
up to 32 ranked candidates:

| HC0 symbols | Duration | HC0 decoded | Paired misses |
| ---: | ---: | ---: | ---: |
| 8 | 85.3 ms | 446/500 | 3 |
| 9 | 96.0 ms | 441/500 | 2 |
| 10 | 106.7 ms | 453/500 | 1 |
| 11 | 117.3 ms | 445/500 | 4 |
| 12 | 128.0 ms | 440/500 | 1 |

The codebook changes with length, so the non-monotonic tail is expected. This
screen used one non-repeating word and therefore answered the modulation-rate
question, but not the proposed repeated-block wire format.

## Literal repeated block

The experiment now constructs a shorter balanced identity block and sends it
twice. The receiver ranks boundaries and tries both HF labels at each boundary;
CRC therefore resolves a weak label decision. On
`mid_latitude_disturbed` at -12 dB with 32 ranked boundaries:

| Block | Total duration | HC0 decoded | Paired boundary misses |
| ---: | ---: | ---: | ---: |
| 4 symbols x2 | 85.3 ms | 446/500 | 8 (1.79%) |
| 5 symbols x2 | 106.7 ms | 454/500 | 5 (1.10%) |
| 6 symbols x2 | 128.0 ms | 439/500 | 1 (0.23%) |
| 7 symbols x2 | 149.3 ms | 448/500 | 2 (0.45%) |
| 8 symbols x2 | 170.7 ms | 446/500 | 2 (0.45%) |

Literal repetition is worse than a non-repeating word of equal duration under
this fading model because it obtains only half as many distinct time/frequency
observations. More airtime is not monotonically better because every block
length has a different sequence.

**128 ms is the shortest promising literal repeated-block candidate in this
screen.** It is 25% shorter than 170.7 ms, but one miss among 439 HC0-decodable
trials means it is not yet qualified as HC0-equivalent. The next useful change
is a repetition-aware detector/sequence search trained and validated on
separate seeds, followed by clipping and partial-blackout tests. Simply making
the block longer did not fix the residual errors.

## Repetition-aware scorer and held-out seeds

Follow-up on 2026-08-30 separated detector selection seeds (`0x51ec7100` and
up) from held-out validation seeds (`0xa11da710` and up). The codeword
construction seed (`0x1285f`) is separate from both. The frozen blocks remain:

```text
HC0: 9, 6, 12, 15, 0, 3
HC1: 12, 3, 15, 6, 9, 0
```

`pair-max` combines the repetitions as diversity branches by selecting the
stronger centred-tone observation for each repeated symbol. It did not improve
the paired result and is therefore rejected in favor of the existing `sum`
scorer:

| Seeds / scorer | HC0 decoded | Misses in top 32 |
| --- | ---: | ---: |
| selection, sum | 225/250 | 1 |
| selection, pair-max | 225/250 | 1 |
| held out, sum | 222/250 | 4 |
| held out, pair-max | 222/250 | 4 |

Increasing only the diagnostic boundary budget on the held-out `sum` run
reduced misses to 1 at 64 boundaries and 0 at 128. This shows that the four
top-32 misses were ranking failures, not complete absence of the true
correlation peak. It does **not** recommend 128 production boundaries: trying
both checked frame decoders at each one would permit 256 expensive attempts.
The bounded body-acquisition fallback is the practical safety mechanism.

Exact commands (Python 3.11 with project requirements installed):

```sh
python experiments/signature128/screen_signature128.py --trials 250 \
  --hops 12 --snr -12 --watterson mid_latitude_disturbed \
  --seed 1374449920 --scorer sum pair-max
python experiments/signature128/screen_signature128.py --trials 250 \
  --hops 12 --snr -12 --watterson mid_latitude_disturbed \
  --seed 2703075088 --scorer sum pair-max
python experiments/signature128/screen_signature128.py --trials 250 \
  --hops 12 --snr -12 --watterson mid_latitude_disturbed \
  --seed 2703075088 --scorer sum --candidate-limit 64
python experiments/signature128/screen_signature128.py --trials 250 \
  --hops 12 --snr -12 --watterson mid_latitude_disturbed \
  --seed 2703075088 --scorer sum --candidate-limit 128
```

These 250-trial screens are a focused pilot, not qualification. The frozen
`sum` scorer and blocks still need at least 1,000 held-out trials per useful
AWGN/Watterson point plus offset, clipping, partial-blackout, false-signature,
and combined-impairment cases. Qualification must include the production
receiver and record lead-induced losses after its bounded CRC/body fallback,
not require the true lead boundary to rank in the top 32 by itself.
