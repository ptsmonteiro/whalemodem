# OFDM on-air results

Measured 2026-08-18 on the IC-705 ↔ Wouxun KG-UV9D Plus FM bench, at the
reduced transmit power used after the USB-desense incident. A result requires
100% byte-for-byte decoding in both directions with no ARQ.

## Channel probe

`probe_channel.py --trials 5` decoded all five probes in both directions. It
reported a common usable band of 300–3000 Hz and worst-direction RMS delay
spread of 0.815 ms, implying a prefix of at least 2.45 ms under the probe's 3×
rule. This correctly selected the 2.5 ms prefix, but its usable-band result was
too optimistic for payload data.

The probe estimates SNR from the scatter of 24 repeated, known training
symbols. Full payload symbols have different peak statistics and need every
carrier decision to be right across roughly one hundred symbols. Consequently
"training carrier above 15 dB" did not imply "CRC-only payload survives" at
the band edges. Treat the probe as a prefix measurement and a band proposal,
not as proof that every proposed carrier is payload-safe.

## Wide-band QPSK

The initial ladder used 300–3000 Hz, QPSK, prefixes of at least 2.45 ms, and
four trials per direction. All five profiles failed every frame despite strong
sync confidence. Drive A/B testing at peak/PAPR settings `(0.9, 9)`, `(0.9,
7)`, `(0.9, 12)`, and `(0.6, 9)` also produced zero decodes, ruling out a
simple drive choice.

A deterministic capture of `ofdm4_50hz_cp8` showed 373 wrong constellation
points out of 5665. Ten of 55 carriers, principally 300–550 Hz and the upper
edge, held 83% of the errors. The clock fit was within the predicted tolerance
and applying it made the result worse; error did not correlate significantly
with ideal symbol peak. The failure was frequency-selective, not sync, clock
drift, or obvious clipping.

## Narrow-band QPSK

At 600–2300 Hz, `ofdm4_50hz_cp8` improved to 3/4 IC-705→HT and 0/4
HT→IC-705. Its diagnostic capture had only 21 wrong points out of 3605, but
CRC-only framing needs zero. Errors concentrated around 2000–2200 Hz and at
the lower edge.

At the cleaner 650–1950 Hz band, the full 691-byte QPSK frame passed 2/4 and
1/4 respectively. A shorter 300-byte frame passed 4/4 and 2/4. Errors occur
throughout the frame, so shortening further gives away the throughput gain
without establishing the required reliability.

## Confirmed result

`ofdm2_50hz_cp8`, BPSK over 650–1950 Hz:

- 50 Hz carrier spacing, 27 carriers
- 2.5 ms cyclic prefix
- 343 payload bytes per 3-second keying
- 914.7 payload bit/s, 1.07× shipped 1200-baud AFSK
- initial screen: 4/4 in each direction
- confirmation: 10/10 in each direction
- total: 28/28 successful frames

The same BPSK profile over 600–2300 Hz passed 4/4 IC-705→HT but only 3/4
HT→IC-705. Removing the diagnosed weak edges was therefore necessary, not
cosmetic.

## Decision

Do not integrate OFDM into `whale/` from this result. The confirmed profile is
reliable and proves the cyclic-prefix design works, but its 914.7 bit/s is
slower than the MFSK experiment's roughly 1011 bit/s winner while adding PAPR,
clock-tolerance, equalisation, and codec-negotiation complexity.

The result could change with FEC or pilot-assisted tracking, but either is a
new frame-format experiment rather than completion work on this CRC-only mode.

## Experimental cross-32-QAM follow-up

Cross-32-QAM was added as an explicit `--bits 5` experiment and tested in both
directions. The profile used 650–1950 Hz, 50 Hz carrier spacing, a 2.5 ms
cyclic prefix, peak amplitude 0.6, and a 12 dB software PAPR target. It carried
1734 bytes, or 4624 payload bit/s arithmetically, in the 3-second keying budget.
All eight frames failed payload decoding: 0/4 HT→IC-705 and 0/4 IC-705→HT.
Sync confidence was 0.953–0.954 and 0.970–0.971 respectively.

Saved-capture diagnostics found 791–1831 wrong decisions among 2781 points per
frame (28.4–65.8%). Derotated EVM ranged from -12.8 to -9.3 dB. One capture
contained a fitted +16 ppm clock excursion, far outside this profile's 3.4 ppm
tolerance, but the other captures still contained hundreds of errors without
that excursion. Some excess error remained around 1750–1800 Hz and mild
limiting could not be separated from noise, but neither was the sole cause.

This is not close enough for shorter CRC-only frames, pilot-density changes,
or another small drive sweep to be meaningful. Any further high-order OFDM
work should begin with FEC and interleaving, using these captures as its input,
before spending more airtime.

The nominally stronger IC-705→HT path was not materially better. Its captures
had 1262–1476 wrong decisions (45.4–53.1%) and -11.5 to -10.5 dB derotated EVM.
Five to seven edge carriers were consistently worse, and one frame showed a
-18 ppm clock excursion, but derotation still left 1087 errors. This rules out
path asymmetry as a way to make the uncoded mode useful.

## 16QAM dense-pilot follow-up

16QAM was tested on IC-705→HT with the same 650–1950 Hz band, 50 Hz spacing,
2.5 ms prefix, peak 0.6, and 12 dB PAPR target. Full-band tracking pilots were
inserted every 4, 2, and 1 data symbols, updating the channel estimate every
90, 45, and 22.5 ms. The profiles carried 1116, 927, and 684 bytes per frame
respectively. Each failed all four trials despite 0.970–0.971 sync confidence.

Representative pilot/4, pilot/2, and pilot/1 captures contained 156/2241
(7.0%), 149/1863 (8.0%), and 136/1377 (9.9%) wrong constellation decisions.
Their EVM was approximately -13 dB. Denser tracking therefore did not improve
the residual and in this sample increased the error fraction; channel-estimate
age is not the binding impairment for uncoded 16QAM.

## Per-carrier LLR weighting (software replay, not on air)

**Everything in this section is replay of already-saved captures. No radio was
keyed for it.** On-air confirmation would need a bench session and has not
happened. Read it as evidence about the decoder, not about the link.

The LDPC decode path gave every subcarrier the same LLR scale, so a band-edge
carrier asserted its wrong decisions as confidently as a clean mid-band one.
Replaying the saved captures against the symbols actually sent shows that is a
real mismatch: within a single frame the per-carrier post-equalisation noise
power spans 4x on `band600_2200` and 51x on `band400_2300`, worst at the low
edge in every capture examined.

`_equalise` already computed a per-carrier noise estimate from the training
symbols and the decode path discarded it. Wiring *that* vector in is the
obvious fix and it is the wrong one. Measured against the truth, its log
correlation with the real per-carrier noise is 0.02–0.82 (RMS log error
1.28–1.59), because with `n_train == 2` it samples the noise during two
adjacent symbols while the data is hurt by the channel drifting away from that
estimate over the following ~94. Driving the weighting from it alone took the
saved passing captures from 37/41 down to **4/41**.

The per-carrier hard-decision residual measures the same quantity where the
demapper actually sees it, and correlates 0.91–0.98 (RMS log error 0.10–0.29).
That is what is now used, clamped to 8x either side of the frame median.

Replaying every saved capture and failure, before and after:

| set | profile | carriers | before | after |
|---|---|---:|---:|---:|
| captures | `16qam_ldpc23_p16` | 27 | 8/10 | 8/10 |
| captures | `16qam_ldpc23_p16_band500_2400` | 39 | 13/15 | **15/15** |
| captures | `16qam_ldpc23_p16_band600_2200` | 33 | 16/16 | 16/16 |
| failures | all eight wideband gates + `ldpc34_band600_2200` | 33–45 | 0/9 | 0/9 |

The confirmed 4/4-both-directions mode (`band600_2200`, 1022 B, 2725.3 bit/s)
is unaffected, and two previously-undecodable `band500_2400` frames now decode.

**No saved wideband failure was rescued.** The frame-level result is the whole
story only if frames are the right unit; they are not, because a frame needs
every one of its ~20–26 codewords. At codeword level the change is large:

| capture | codewords | flat | weighted |
|---|---:|---:|---:|
| `band400_2300` | 23 | 10/23, 22.7 it | 20/23, 11.9 it |
| `band400_2400` | 24 | 11/24, 18.6 it | 12/24, 16.2 it |
| `band500_2200` | 20 | 17/20, 8.6 it | 18/20, 6.7 it |
| `band500_2300` | 22 | 18/22, 9.6 it | 20/22, 7.2 it |
| `band500_2500` | 24 | 12/24, 16.3 it | 13/24, 15.4 it |
| `band500_2600` | 25 | 21/25, 14.1 it | 23/25, 9.9 it |
| `band500_2700` | 26 | 13/26, 16.5 it | 13/26, 16.3 it |
| `band600_2400` | 22 | 13/22, 15.6 it | 12/22, 15.6 it |
| `ldpc34_band600_2200` | 19 | 18/19, 9.8 it | 18/19, 8.0 it |
| **total** | **205** | **133** | **149** |

Mean LDPC iterations fall on the passing captures too (`band500_2400` 5.6 →
4.0), which is margin that did not previously exist.

### What this says about the band-edge hypothesis

A genie bounds it. Handing the decoder the *true* per-carrier noise power —
unattainable, since it is measured against the payload the receiver is trying
to recover — reaches 155/205 codewords and rescues exactly **one** of the nine
failures (`band500_2600`). So the ceiling on per-carrier reliability weighting
on these captures is 1/9, and the implemented estimator already captures 16 of
the 22 codewords available under it. Per-carrier weighting is worth having and
is not what is holding wideband back.

A second genie says where the impairment actually is. Weighting per individual
symbol-carrier *cell* with its true error rescues 8/9. The errors are therefore
localised in time as well as in frequency: per-carrier error counts are
8.5–18.9x overdispersed relative to binomial, but per-*symbol* counts are also
1.6–3.6x overdispersed, and the worst 8 symbols in each failing frame carry
35–51% symbol error against frame averages of 15–28%. That is the signature of
burst events inside the frame, not of a handful of permanently bad band-edge
carriers.

The raw symbol error rates make the same point from the other side: the failing
wideband frames sit at 15–28%, and the confirmed passing mode sits at 17%. The
failures are not a little short of decodable, and no reweighting of a 28%-wrong
constellation closes that.

### Suggested next experiment

Time-localised impairment, not frequency-localised, is what the evidence
points at. The cheapest software-only follow-ups, in order:

1. Per-symbol as well as per-carrier reliability — a rank-1 (carrier x symbol)
   noise model costs nothing on air and the cell genie says most of the
   available gain lives there.
2. Check whether the bad symbols cluster near the tracking-pilot boundaries,
   which would make this an interpolation failure rather than a channel one.
3. Only then spend airtime, and spend it on a `band500_2600` re-run, since that
   is the only failure any per-carrier scheme can reach.

## Front training, drift, and the transmitter's limiter (software replay)

**Replay of already-saved captures. No radio was keyed for any of it.** The
nine saved wideband failures and all 41 saved passing captures were laid
against the payloads actually sent. The harness reproduces the previous
section's numbers exactly (149/205 codewords on the failures, 39/41 passing
captures), which is what licenses the rest.

The question was whether a longer coherent header -- 15 training symbols
rather than 2 -- is the thing we are missing, tested jointly with channel
drift because a front estimate and its decay are one mechanism. Both halves
came back negative, and looking for the drift found the real impairment
instead.

### The bad symbols do not sit at the pilot boundaries

Data symbol *i* is equalised by interpolating between the anchors at
coordinates `16*(i//16)` and the next one, so an interpolation failure has to
show as an excess in the middle of each group. Pooling the nine failures with
each frame normalised by its own mean symbol-error rate:

    phase in group  0     1     2     3     4     5     6     7
    relative error  0.86  0.96  0.94  1.09  1.07  0.99  1.09  1.15
                    8     9    10    11    12    13    14    15
                    0.93  1.09  0.92  0.99  1.01  0.92  1.02  0.96

There is a mid-group excess and it is real: mid (phases 6-9) against edge
(15, 0, 1, 14) is +0.117 in these units, permutation p = 0.0088 against
reorderings of each frame's own per-symbol counts, which holds the
overdispersion fixed and tests only position. It is also worth almost
nothing. Flattening every phase to the edge-of-group rate would remove
**5.1%** of the frame's errors, against the 35-51% concentration the worst
eight symbols carry. The phase profile as a whole is not distinguishable from
noise (p = 0.14); only the pre-specified mid-versus-edge contrast is.

A second measurement settles it. Genie pilots -- the symbols actually sent,
used as anchors at spacing N, linearly interpolated, error counted only on
the symbols that were *not* anchors, since an anchor corrects itself exactly
and that is not a decode:

| genie pilot spacing | 16 | 8 | 4 | 2 |
|---|---:|---:|---:|---:|
| cell error on interpolated symbols | 24.3% | 23.1% | 22.4% | 21.4% |

Noise-free anchors every second symbol -- eight times the deployed density --
move the error by 12% relative and never beat the deployed 21.5%. This is the
same answer the on-air pilot-density sweep gave from 1 through 32, obtained
without airtime, and it rules out interpolation as the problem.

### More front training helps a little, and not for the reason expected

`n_train` cannot be replayed directly, because the saved captures contain two
training symbols and no more. It can be emulated exactly: a training symbol
is a *known* symbol, and the payload is known here, so combining the two real
training symbols with the first `K - 2` data symbols by least squares
reproduces the geometry of a K-symbol header. The one difference is waveform
-- a real training symbol is constant-modulus Newman-phase, a data symbol is
16QAM -- and the `|X|^2` weighting in the combiner is what that costs.

Front-only equalisation, tracking pilots ignored so the front estimate is
fully exposed. EVM of the equalised data against the symbols sent, dB, by
symbol index, pooled over the nine failures. `de-a` is the same measurement
with the frame's one common amplitude scale removed first:

| | 13-15 | 16-31 | 32-47 | 48-63 | 64-79 | 80-95 |
|---|---:|---:|---:|---:|---:|---:|
| raw K=2 | -8.21 | -7.50 | -6.76 | -6.27 | -6.19 | -5.81 |
| raw K=15 | -10.84 | -9.76 | -8.71 | -7.80 | -7.32 | -6.84 |
| de-a K=2 | -7.05 | -7.29 | -7.05 | -7.28 | -6.99 | -6.44 |
| de-a K=4 | -8.64 | -8.77 | -8.78 | -9.08 | -8.55 | -7.99 |
| de-a K=8 | -9.19 | -9.42 | -9.34 | -9.67 | -9.04 | -8.45 |
| de-a K=15 | -9.37 | -9.61 | -9.56 | -9.85 | -9.14 | -8.59 |

Two things. A longer header does improve the front estimate, by 1.6 dB from
K=2 to K=4 and 0.7 dB more by K=15 -- more than the 0.8 dB simple noise
averaging predicts, because the first two symbols after key-up are themselves
disturbed and averaging dilutes them. And the raw rows appear to drift 2.4-4.0
dB across the frame while the `de-a` rows are flat to within 0.8 dB. **The
apparent drift is the amplitude bias, not the channel.** Over a 2.9 s frame at
50 Hz spacing this channel does not move enough to measure.

In the deployed configuration the front estimate reaches only the first 16 of
~95 data symbols, because the first tracking pilot re-anchors at coordinate
16. Cell error and decoded codewords, nine failures, tracking pilots on:

| n_train | symbols 0-15 | whole frame | codewords | frames |
|---:|---:|---:|---:|---:|
| 2 | 23.18% | 21.47% | 149/205 | 0/9 |
| 4 | 19.48% | 20.83% | 153/205 | 0/9 |
| 8 | 17.75% | 20.54% | 155/205 | 1/9 |
| 15 | 17.41% | 20.46% | 156/205 | 0/9 |

### The airtime, honestly

At 22.5 ms a symbol, 13 extra training symbols is 292.5 ms. LDPC block
granularity quantises what that costs on the confirmed mode:

| n_train | payload | bit/s | keying |
|---:|---:|---:|---:|
| 2 | 1022 B | 2725.3 | 2.91 s |
| 4 | 1022 B | 2725.3 | 2.95 s |
| 8 | 968 B | 2581.3 | 2.93 s |
| 15 | 914 B | 2437.3 | 2.97 s |

`n_train = 4` is free in payload bytes and costs 45 ms of keying. It is also
worth 4 codewords out of 205 and no frames.

The break-even for `n_train = 8` is exact and it lands on zero. Its one
replay rescue is `band500_2200`, 35 carriers. At `n_train = 8` that profile
carries 1022 B at **2725.3 bit/s** -- byte for byte and bit for bit the
confirmed 600-2200 mode at `n_train = 2`. The six extra training symbols eat
the two extra carriers exactly. Even granting the rescue on air, which one
replayed frame does not, it buys nothing.

### Verdict on the 15-symbol header

Ruled out as a lever here. It is not free (10.6% of payload), the advantage it
buys is confined to the 16 symbols before the first tracking pilot, it is
saturated by `n_train = 8`, it rescues at most one of nine saved failures and
not monotonically, and the drift it was supposed to fight does not exist on
this link once the amplitude bias is removed. If `n_train` is raised at all it
should be to 4, on the grounds that it is free rather than that it is useful.

### What is actually limiting wideband 16QAM

Looking for the drift found it. Fit one complex least-squares scale per frame,
`a = sum(E conj(X)) / sum(|X|^2)`, between the equalised grid and the symbols
sent. Every capture, passing and failing, gives the same answer:

- `|a|` = 0.81-0.91, so the equalised constellation lands 10-20% *inside*
  where the decision boundaries are;
- `arg a` = -1.7 to +0.7 degrees. It is amplitude, not phase. 83-92% of the
  residual energy is in the magnitude;
- a software loopback through `modulate` and `_equalise` with no channel at
  all gives `|a|` = 0.9989 at the deployed 12 dB PAPR target. **The deficit is
  not ours.**

It is the radio's limiter, and two measurements say so. It splits by
transmitting radio: on `band600_2200`, `1/|a|` is 1.125-1.162 for the eight
HT->IC-705 captures and 1.197-1.240 for the eight IC-705->HT ones, with no
overlap. And within every one of 35 captures examined, a symbol's own scale
correlates *negatively* with the peak of its own ideal unclipped waveform,
r = -0.26 to -0.67 against a null standard deviation of 0.10. High-peak
symbols come back smaller. That is `diagnose_ofdm.amplitude_axis`' limiter
signature, measured on the scale rather than on the EVM.

The training and tracking-pilot symbols escape it, and that is the whole
mechanism. They are Newman-phase constant-modulus at 5.4 dB PAPR against the
data's 12.2 -- chosen deliberately so the limiter would touch them least, and
the consequence is that the channel estimate they produce is calibrated to a
gain the data never sees. QPSK and BPSK never noticed, because their decisions
are signs. 16QAM decisions are amplitudes.

Correcting the scale, at codeword level on the nine saved failures:

| correction | codewords | frames |
|---|---:|---:|
| flat (as shipped) | 149/205 | 0/9 |
| genie per frame | 183/205 | 6/9 |
| genie per symbol | 185/205 | 6/9 |
| genie rank-1 (symbol x carrier) | **205/205** | **9/9** |

A rank-1 correction rescues every saved wideband failure. That is the same
place the previous section's cell genie pointed, with the mechanism supplied:
the symbol factor is the limiter's per-symbol compression and the carrier
factor is the band shape. It is a deterministic bias, which is why weighting
its reliability recovered so little of it.

A receiver can reach part of this without a genie and without airtime. A
six-point scale grid (1.00, 1.05, ... 1.25) tried at the decoder, accepted
only on a full LDPC-plus-CRC pass, rescues **6 of the 9 saved failures** and
regresses nothing: all 41 passing captures still decode, every one of them at
s = 1.00 on the first try in under a second. The construction is
self-validating -- a wrong scale fails the code -- so it cannot convert a
passing frame into a failing one, only spend CPU. The cost is real and is the
open question: a frame that never decodes now costs up to six times the LDPC
work, on top of the existing block-count search, and that has to fit the
link's poll budget before it can ship.

Blind moment estimation is not yet the answer. The M2M4 estimator
(`a^4 = (2 m2^2 - m4) / (2 - kx)`, `kx` = 1.32 for 16QAM) tracks the genie to
within 2-4% on the passing captures but undershoots by up to 17% on the
failures and returns nothing at all on two of them, and undershooting is worse
than not correcting: driven instead from a scale that minimises distance to
the nearest constellation point, which is biased small for the same reason,
the saved failures went from 149 codewords to 118.

### Suggested next experiment

Software, no airtime: a per-symbol times per-carrier scale correction driven
from tentative decisions, with the decoder's own CRC as the accept test. The
genie says it is worth 9/9 on these captures and the scale grid already banks
6/9 of it with a construction that cannot regress. Settle the CPU budget
first -- that, not the estimator, is what decides whether it ships.

Only then spend airtime, and spend it on `band500_2600`: 43 carriers,
3589.3 bit/s, +31.7% on the confirmed mode, and the widest band any of these
corrections rescues.

## Interleaved tracking-pilot follow-up

Pilot-assisted tracking was implemented after the initial result. A known
full-band symbol is inserted after every configurable group of data symbols.
The receiver searches locally for each pilot (making it a symbol-timing
anchor), refreshes every carrier's complex channel estimate, and interpolates
timing and EQ between adjacent pilots. Pilot airtime is included in
`max_payload`; intervals 4, 8, and 16 therefore deliver 556, 617, and 650-byte
QPSK payloads respectively over 650–1950 Hz.

The on-air screen was deliberately run only on the established weak
HT→IC-705 path. Results, four full-budget frames each:

| pilot interval | payload | payload bit/s | decoded |
|---|---:|---:|---:|
| 16 | 650 B | 1733.3 | 1/4 |
| 8 | 617 B | 1645.3 | 0/4 |
| 4 | 556 B | 1482.7 | 0/4 |

No candidate reached weak-path confirmation, so the stronger path was not
keyed. This is now the sweep's normal strategy: screen and confirm the known
weak path first, then validate the strong path once for the final winner.

A weak-path-only drive repeat on interval 16 compared `(peak, PAPR)` settings
`(0.9, 9)`, `(0.9, 7)`, `(0.9, 12)`, and `(0.6, 9)`. They decoded 0/4, 0/4,
3/4, and 0/4 respectively. The chosen `(0.9, 12)` setting then repeated at
2/4. Higher software PAPR is clearly the least damaging setting on this radio,
but it remains far from the 100% criterion; the stronger path was again not
run.

A deterministic interval-16 capture separated estimator behavior from frame
luck. Front-only EQ left five wrong QPSK decisions; full interleaved
per-carrier replacement left four, as did smoothed and common-phase-only
updates. The remaining errors were isolated at 750, 800, 1000, and 1600 Hz,
not concentrated late in the frame or at a band edge. Tracking is therefore
real but not the binding impairment: the CRC-only frame now needs coding gain,
not denser pilots.

### Expanded interval sweep

A follow-up filled in both sides of the original 4/8/16 grid, again screening
only HT→IC-705 at `(0.9, 12 dB)` drive:

| pilot interval | payload | payload bit/s | screen |
|---|---:|---:|---:|
| 32 | 671 B | 1789.3 | 1/4 |
| 24 | 664 B | 1770.7 | 0/4 |
| 12 | 637 B | 1698.7 | 0/4 |
| 6 | 596 B | 1589.3 | 0/4 |
| 3 | 515 B | 1373.3 | 0/4 |
| 2 | 461 B | 1229.3 | 0/4 |
| 1 | 340 B | 906.7 | 4/4, then 8/10 confirmation |

Interval 1 found a genuine density threshold but not a usable mode. Spending
every second OFDM symbol on a pilot reduced the payload rate slightly below
the confirmed BPSK OFDM profile (914.7 bit/s), and two failures remained in
ten confirmation frames. Since weak-path confirmation failed, IC-705→HT was
not tested. This closes the pilot-density question from 1 through 32 symbols:
tracking alone cannot meet the CRC-only reliability requirement.

## On-air validation of per-carrier LLR weighting, 2026-08-19

Measured on the radios, not replayed. Same bench and the same reduced transmit
power as every result above, `--amplitude 0.6 --papr 12`. The methodology is
the established one: screen the weak `ht->ic705` path, confirm it, then the
strong path.

### New confirmed mode

`ofdm16_50hz_cp120samp_p16_ldpc23` over **500-2400 Hz**: 39 carriers, 50 Hz
spacing, 2.5 ms prefix, tracking pilots every 16 data symbols, rate-2/3 LDPC.
1238 payload bytes per 2.97 s keying.

| stage | direction | decoded | sync confidence |
|---|---|---:|---|
| screen | ht->ic705 (weak) | 4/4 | 0.802-0.807 |
| confirm | ht->ic705 (weak) | 10/10 | 0.802-0.809 |
| confirm | ic705->ht (strong) | 10/10 | 0.918-0.923 |

**24/24 total, 100% in both directions. 3301.3 payload bit/s**, 3.87x the
shipped 1200-baud profile and +21.1% on the previously confirmed 2725.3 bit/s
600-2200 Hz mode. This supersedes that mode as the fastest profile confirmed on
this bench; the BPSK figure in "Confirmed result" above remains the historical
record of the CRC-only ladder and is not comparable.

The only change since this band last flew is the per-carrier LLR weighting
described in the replay section above. The same band and profile previously
managed 6/9 on the weak path (3/3, a 1/1 gate, then 2/4 on repeat).

### The control, and the ceiling

The 6/9-to-14/14 comparison spans two sessions, so on its own it cannot
separate the code change from a better day. `band500_2600` settles it. It
failed its gate 0/1 before and was re-run in this session:

| band | carriers | payload | bit/s | weak path |
|---|---:|---:|---:|---:|
| 500-2400 | 39 | 1238 B | 3301.3 | 14/14 |
| 500-2600 | 43 | 1346 B | 3589.3 | 0/4 |

Sync confidence on the failures was 0.808-0.813, indistinguishable from the
passing band's 0.802-0.809, so this is payload decoding failing and not sync.
Conditions in this session were therefore not uniformly better, and the
500-2400 Hz result is attributable to the weighting.

It also bounds the gain. The replay analysis predicted `band500_2600` was the
one failure a per-carrier scheme could rescue, but that prediction came from a
*genie* holding the true per-carrier noise, which a receiver cannot have. The
implemented estimator does not reach it. **Per-carrier LLR weighting is worth
21.1% and stops at 500-2400 Hz.** Going wider needs the amplitude correction
for the transmitter's limiter, which is not implemented.

No radio control errors occurred in any of the 28 keyings.
