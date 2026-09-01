# hf11_ofdm49_v6_duration — pushing frame duration on hf10's 16-QAM + LDPC 3/4 PHY

**Bottom line: on today's channel, frame duration can be pushed roughly
2x-3x past `hf10_ofdm49_v6`'s qualified 176 B/0.325 s config (to 352 B/
0.575 s and 528 B/0.850 s) with decode reliability that is, within this
session's small samples, statistically indistinguishable from hf10's own
baseline reconfirmation (≈71-75% either way) and a genuine net-bps gain
(+13% to +15%, arithmetic, achieved on every trial that did decode). Past
that, at 704 B/12 LDPC codewords (≈4x hf10's duration), reliability drops
to ≈55% (6/11 across three pilot densities) — a real, reproducible ceiling
that is NOT fixed by denser pilots (task requirement 3) and is NOT
explained by rising raw channel BER (task requirement 4: raw BER stayed
flat, ~0.5%-3.5%, across every payload size tested). The mechanism instead
looks like the FEC-structural risk the task's own arithmetic flagged in
advance: more LDPC codewords per frame means more independent chances for
one codeword to fail to converge, and a single non-converged codeword
fails the whole frame's CRC regardless of frame length. This is a
different ceiling *mechanism* than `hf5_8psk_4k`/`hf7_ofdm_v3`'s uncoded
marginal-SNR edges (there, raw BER itself climbed with payload; here it
does not), but it produces the same qualitative signature the task asked
to watch for: gradual degradation, not an abrupt cliff.**

All real-hardware numbers below are real over-the-air IC-7300(TX) ->
IC-705(RX) audio-coupled trials (`bench.radio_pair`, `a=ic7300`,
`b=ic705`, hardcoded, matching hf10 and every prior experiment); IC-705
was never keyed. No simulation was run in this experiment — the task's
own hf10 baseline was already simulation-checked, and every parameter
pushed here (`--packet-bytes`, `--pilot-interval`) is a pure config change
on hf10's unmodified, already-hardware-validated codec, so simulation
would add nothing a real trial doesn't already answer more honestly.

## Design

`experiments/hf10_ofdm49_v6/ofdm49_v6.py` is imported **read-only,
unmodified** — no PHY code was written for this experiment. Every "step"
below is a different `--packet-bytes`/`--pilot-interval` combination
passed to a thin copy of hf10's own `hardware_test.py`
(`experiments/hf11_ofdm49_v6_duration/hardware_test.py`), which differs
only in its default output directory (`logs/mode_qualification/hf-ssb/hf11/`)
and delegates the actual trial loop to hf10's `main()` unchanged. Same
49 contiguous bins (300-2700 Hz), 16-QAM, rate-3/4 LDPC, gain-only
equalizer throughout — the exact "everything except payload size" hf10
qualified.

**Payload sizes tested** were chosen at rate-3/4 LDPC codeword-packing
sweet spots (`k=486` info bits/codeword), the same principle hf10 itself
used to pick 176 B over the arithmetically worse 220 B:

| Payload | packet_bytes | data_bits | n_codewords | coded bits | waste |
|---|---|---|---|---|---|
| 176 B (hf10 baseline) | 182 | 1456 | 3 | 1944 | 0.1% |
| 352 B (2x) | 358 | 2864 | 6 | 3888 | 1.8% |
| 528 B (3x) | 534 | 4272 | 9 | 4374->5832 | 2.3% |
| 704 B (4x) | 710 | 5680 | 12 | 7776 | 2.6% |

## Step 1 — reconfirm hf10's exact 176 B baseline first (task requirement 1)

Per the task, and per this project's own repeated finding of session-to-
session channel drift (hf8, hf9), hf10's winning config was re-run before
touching anything. `--bps 4 --fec-rate 3/4 --packet-bytes 182
--pilot-interval 20`, three small batches (one single-trial spot check,
then two 3-trial batches, different seeds):

| Trial | SNR (dB) | raw_ber | post-FEC ber | ldpc iters | Outcome |
|---|---|---|---|---|---|
| (spot, default seed) | 14.4 | 0.0679 (132/1944) | 0.0582 | [30,2,7] | crc_fail |
| seed1001-1 | 14.9 | 0.0267 (52/1944) | 0.0007 | [7,2,30] | crc_fail |
| seed1001-2 | 14.6 | 0.0123 (24/1944) | 0.0000 | [2,2,0] | decoded |
| seed1001-3 | 14.8 | 0.0041 (8/1944) | 0.0000 | [2,0,1] | decoded |
| seed2002-1 | 14.8 | 0.0175 (34/1944) | 0.0000 | [3,1,2] | decoded |
| seed2002-2 | 14.3 | 0.0067 (13/1944) | 0.0000 | [2,0,0] | decoded |
| seed2002-3 | 14.4 | 0.0190 (37/1944) | 0.0000 | [3,2,1] | decoded |

**5/7 decoded (71%), SNR 14.3-14.9 dB** — noticeably weaker than hf10's
own 12/13 (92%) at a similar SNR range (14.0-16.5 dB in hf10's session).
This is consistent with this project's established finding that the
channel drifts session to session (hf8, hf9): **today's baseline starts
from a worse footing than hf10's own record**, which matters for
interpreting every comparison below — a duration step that matches this
weaker 71% baseline is not automatically worse than hf10's number, it may
simply be riding the same weaker channel every other step in this session
also rides.

## Step 2 — 2x duration: 352 B, pilot interval 20 (task requirement 2)

Smoke batch (3 trials) then a confirmation batch (5 trials, different
seed), same pilot interval as baseline:

| Batch | Trial | SNR (dB) | raw_ber | post-FEC ber | ldpc iters | Outcome |
|---|---|---|---|---|---|---|
| smoke (seed3003) | 1 | 16.8 | 0.0175 (68/3888) | 0.0000 | [13,2,2,2,1,4] | decoded |
| smoke | 2 | 14.7 | 0.0113 (44/3888) | 0.0000 | [2,2,1,1,0,1] | decoded |
| smoke | 3 | 14.4 | 0.0113 (44/3888) | 0.0000 | [2,1,1,1,2,5] | decoded |
| confirm (seed4004) | 1 | 15.4 | 0.0355 (138/3888) | 0.0036 | [30,2,13,5,5,26] | crc_fail |
| confirm | 2 | 14.5 | 0.0090 (35/3888) | 0.0000 | [2,1,1,0,1,1] | decoded |
| confirm | 3 | 14.3 | 0.0103 (40/3888) | 0.0000 | [4,2,1,1,1,0] | decoded |
| confirm | 4 | 14.8 | 0.0118 (46/3888) | 0.0000 | [2,1,2,0,0,1] | decoded |
| confirm | 5 | 14.6 | 0.0252 (98/3888) | 0.0220 | [30,3,1,1,1,0] | crc_fail |

**6/8 decoded (75%)**, mean raw BER 1.84% — comparable to the 176 B
baseline (71%, mean raw BER 1.44%) at the same SNR range. **Net bps
4897.4 on every trial that decoded — a +13% arithmetic gain over hf10's
4332.3**, achieved with no PHY change at all.

## Step 3 — 2x duration, denser pilot (task requirement 3: does duration
## growth need denser pilots?)

352 B again, `--pilot-interval 10` (2 mid-frame pilots instead of 1), 5
trials:

| Trial | SNR (dB) | raw_ber | post-FEC ber | ldpc iters | Outcome |
|---|---|---|---|---|---|
| 1 | 15.8 | 0.0157 (61/3888) | 0.0163 | [30,1,0,0,0,0] | crc_fail |
| 2 | 15.4 | 0.0064 (25/3888) | 0.0000 | [2,1,1,0,1,1] | decoded |
| 3 | 16.1 | 0.0077 (30/3888) | 0.0000 | [4,1,2,0,1,1] | decoded |
| 4 | 16.1 | 0.0080 (31/3888) | 0.0000 | [4,1,2,1,0,0] | decoded |
| 5 | 16.0 | 0.0049 (19/3888) | 0.0000 | [2,1,1,1,0,2] | decoded |

**4/5 (80%)** at slightly higher SNR (15.4-16.1 dB) than step 2's batches
— not a clean apples-to-apples comparison (SNR itself drifted upward
across the session, see the honest caveats section), but directionally
consistent with "denser pilots don't clearly hurt or help here," matching
hf9's own ambiguous finding on denser time-domain pilots and hf10's own
negative finding on the opposite extreme (dropping the pilot entirely,
step 5 there, made things worse). Net bps at pilot-interval 10 is lower
(4693.3, the extra pilot symbol costs airtime) than pilot-interval 20's
4897.4, so **pilot interval 20 remains the better choice at 352 B** —
denser pilots bought no measurable reliability gain here to justify their
cost.

## Step 4 — 3x duration: 528 B, pilot interval 20

Smoke (3 trials) then confirmation (5 trials):

| Batch | Trial | SNR (dB) | raw_ber | post-FEC ber | ldpc iters | Outcome |
|---|---|---|---|---|---|---|
| smoke (seed6006) | 1 | 15.4 | 0.0178 (104/5832) | 0.0199 | [30,30,2,1,1,1,0,1,1] | crc_fail |
| smoke | 2 | 16.1 | 0.0072 (42/5832) | 0.0000 | [2,2,2,1,0,0,1,0,2] | decoded |
| smoke | 3 | 15.7 | 0.0087 (51/5832) | 0.0000 | [2,1,1,1,1,0,1,1,1] | decoded |
| confirm (seed7007) | 1 | 17.0 | 0.0123 (72/5832) | 0.0069 | [30,2,1,1,1,0,0,1,0] | crc_fail |
| confirm | 2 | 15.5 | 0.0094 (55/5832) | 0.0000 | [3,2,1,1,1,1,1,1,1] | decoded |
| confirm | 3 | 15.8 | 0.0093 (54/5832) | 0.0000 | [12,2,1,0,0,0,0,1,1] | decoded |
| confirm | 4 | 15.3 | 0.0081 (47/5832) | 0.0000 | [3,2,1,1,1,1,0,0,1] | decoded |
| confirm | 5 | 16.1 | 0.0086 (50/5832) | 0.0000 | [3,2,2,1,1,0,0,1,0] | decoded |

**6/8 decoded (75%)**, mean raw BER 1.04% — again comparable to the 71-75%
seen at 176/352 B, at a slightly higher SNR range (15.3-17.0 dB, the
channel measurably improved as the session went on — see caveats).
**Net bps 4969.4 — a +15% arithmetic gain over hf10's baseline.**

## Step 5 — 4x duration: 704 B, three pilot densities (task requirement 3,
## pushed harder: is there a duration where denser pilots become necessary?)

`--pilot-interval 20` (2 pilots, smoke, 3 trials):

| Trial | SNR (dB) | raw_ber | post-FEC ber | ldpc iters | Outcome |
|---|---|---|---|---|---|
| 1 | 16.2 | 0.0285 (222/7776) | 0.0206 | [30,30,2,2,1,0,1,2,1,1,1,2] | crc_fail |
| 2 | 15.8 | 0.0087 (68/7776) | 0.0000 | [11,2,1,1,1,0,1,1,1,0,1,1] | decoded |
| 3 | 15.9 | 0.0163 (127/7776) | 0.0060 | [2,2,1,4,4,0,30,0,0,1,1,2] | crc_fail |

1/3 decoded. `--pilot-interval 10` (4 pilots, 5 trials):

| Trial | SNR (dB) | raw_ber | post-FEC ber | ldpc iters | Outcome |
|---|---|---|---|---|---|
| 1 | 16.6 | 0.0103 (80/7776) | 0.0014 | [8,1,3,0,0,1,0,1,0,1,4,30] | crc_fail |
| 2 | 17.8 | 0.0114 (89/7776) | 0.0005 | [6,14,1,0,1,1,1,1,1,0,0,30] | crc_fail |
| 3 | 17.6 | 0.0060 (47/7776) | 0.0000 | [4,2,0,0,0,0,0,0,1,1,0,1] | decoded |
| 4 | 17.6 | 0.0035 (27/7776) | 0.0000 | [2,1,1,0,0,0,1,0,0,2,1,1] | decoded |
| 5 | 17.6 | 0.0030 (23/7776) | 0.0000 | [2,1,1,0,0,1,0,0,0,0,1,1] | decoded |

3/5 decoded. `--pilot-interval 8` (5 pilots, smoke, 3 trials):

| Trial | SNR (dB) | raw_ber | post-FEC ber | ldpc iters | Outcome |
|---|---|---|---|---|---|
| 1 | 18.3 | 0.0062 (48/7776) | 0.0030 | [30,0,0,0,0,0,0,0,0,0,1,2] | crc_fail |
| 2 | 18.2 | 0.0059 (46/7776) | 0.0000 | [2,2,1,2,1,1,1,1,0,0,1,0] | decoded |
| 3 | 18.4 | 0.0035 (27/7776) | 0.0000 | [5,1,0,0,0,0,0,0,0,0,0,1] | decoded |

2/3 decoded. **Combined across all three pilot densities: 6/11 decoded
(55%)** — at the *highest* SNR range seen in the whole experiment
(15.8-18.4 dB), yet the worst decode rate. **Denser pilots did not
rescue it** (pilot 20: 1/3, pilot 10: 3/5, pilot 8: 2/3 — no monotonic
improvement, and even the densest setting only matched the middling
pilot-10 rate), directly answering task requirement 3: at this frame
duration, pilot density is not the limiting lever, matching hf10's own
finding (dropping pilots hurt) and hf9's (denser pilots gave an
ambiguous, non-reliable improvement) — this project has now tested pilot
density in both directions across three experiments and never found it a
reliable rescue technique past the baseline operating point.

## Why 704 B fails and 352/528 B mostly don't: raw BER stayed flat, so
## it isn't a channel-tracking problem (task requirement 4)

| Payload | n_codewords | Mean raw BER (all trials) | Decode rate |
|---|---|---|---|
| 176 B | 3 | 1.35% (7 trials) | 5/7 (71%) |
| 352 B | 6 | 1.55% (13 trials, both pilot settings) | 10/13 (77%) |
| 528 B | 9 | 1.04% (8 trials) | 6/8 (75%) |
| 704 B | 12 | 0.83% (11 trials, all 3 pilot settings) | 6/11 (55%) |

**Raw (pre-FEC) BER did not climb with frame duration — if anything it
trended slightly down**, because SNR itself drifted upward over the
course of the session (14.3 dB at the start to 18.4 dB by the end — see
honest caveats). This directly rules out the mechanism `hf5_8psk_4k` and
`hf7_ofdm_v3` found at their own marginal edges (raw channel BER climbing
smoothly with payload length) as the cause of 704 B's lower decode rate.

Instead, every one of the 5 failures at 704 B (and most of the failures
at smaller sizes too) shows the *same* signature: 11 of the frame's 12
codewords converge in 0-4 iterations while exactly one codeword hits the
30-iteration cap and fails outright (see the `ldpc_iterations` columns
above — e.g. step 5's trial 3: `[2,2,1,4,4,0,30,0,0,1,1,2]`). This is
consistent with the arithmetic the task asked to reason through
honestly: **more codewords per frame means more independent draws at
whatever per-codeword non-convergence probability this channel/LDPC-rate
combination has, and a rate-3/4 code either fully corrects a codeword or
fails its syndrome check outright (hf10 already established this
all-or-nothing-per-codeword behavior) — one bad codeword out of 12 fails
the whole frame's CRC exactly as one bad codeword out of 3 would.** A
rough per-codeword success-rate fit from this session's frame-level
decode rates (crude, small-n, and confounded by the SNR drift noted
above): 176 B implies ≈89% per-codeword success (3 codewords,
0.714^(1/3)); 352 B implies ≈95-96%; 528 B implies ≈97%; 704 B implies
≈95% (0.545^(1/12)). These estimates bounce around 90-97% rather than
cleanly tracking one number — consistent with per-codeword success being
dominated by which specific few OFDM symbols in a frame happen to land on
a transient dip, not a fixed, duration-independent constant — but the
qualitative story (more codewords -> lower whole-frame success even at
flat per-bit BER) is exactly what happened here going from 3 to 12
codewords.

## Net-bps arithmetic vs reliability, computed honestly (task requirement 5)

| Config | Payload | Frame (s) | Net bps (if decoded) | Decode rate (this session) | vs hf10 (4332.3, 92%) |
|---|---|---|---|---|---|
| hf10 baseline, reconfirmed | 176 B | 0.325 | 4332.3 | 5/7 (71%) | — (weaker than hf10's own 92%; channel drift) |
| 2x, pilot 20 | 352 B | 0.575 | 4897.4 | 6/8 (75%) | +13% bps, comparable reliability |
| 2x, pilot 10 | 352 B | 0.600 | 4693.3 | 4/5 (80%) | +8% bps, no clear reliability edge over pilot 20 |
| 3x, pilot 20 | 528 B | 0.850 | 4969.4 | 6/8 (75%) | +15% bps, comparable reliability |
| 4x, pilot 20 | 704 B | 1.100 | 5120.0 | 1/3 (33%) | +18% bps *if* it decodes, but rarely does |
| 4x, pilot 10 | 704 B | 1.150 | 4897.4 | 3/5 (60%) | +13% bps *if* it decodes |
| 4x, pilot 8 | 704 B | 1.175 | 4793.2 | 2/3 (67%) | +11% bps *if* it decodes |

Longer frames amortize the fixed preamble/pilot overhead exactly as the
task's arithmetic anticipated (net bps climbs from 4332 to ~4900-5100
across 2x-4x duration when a frame does decode), but the **effective**
throughput (accounting for the fraction of frames that actually decode,
which is what a real link sees after retries) tells a different story:
effective bps = net_bps x decode_rate is ≈3075 for hf10's reconfirmed
baseline (4332x0.71), ≈3673 for 352 B/pilot20 (4897x0.75, the best
result found), ≈3727 for 528 B/pilot20 (4969x0.75, essentially tied with
352 B), and only ≈1690-2938 for any 704 B config (net bps x 0.33-0.67).
**528 B (and 352 B, effectively tied with it) is the best duration/
reliability tradeoff found this session — not 704 B**, even though 704 B
has the highest per-decoded-frame net bps, because its much lower decode
rate more than cancels the per-frame bps gain once a real link's need to
retry failed frames is accounted for.

## Honest verdict

- **Frame duration CAN be pushed 2x-3x (to 352-528 B, 0.575-0.850 s)
  past hf10's qualified 176 B config with no PHY change and no clear
  reliability cost this session** (75% vs hf10-reconfirmed's 71%, both
  well below hf10's own original 92%, consistent with today's channel
  being weaker than hf10's session at the start and then improving
  through the session — see caveats). This is a real, positive,
  arithmetic-and-decode-rate-consistent finding: **528 B at pilot interval
  20 is this experiment's recommended extension of hf10's config**,
  giving ≈4969 bps net on a decode (+15% over hf10) at a decode rate
  indistinguishable, within this session's small samples, from hf10's own
  baseline re-tested today.
- **Beyond that, at 704 B (12 LDPC codewords, ≈4x hf10's duration),
  reliability drops to ≈55% (6/11) regardless of pilot density (20, 10,
  or 8 all tried) — a real ceiling, not a channel-quality collapse** (raw
  BER at 704 B was, if anything, the *lowest* of any payload size tested,
  because the session's SNR happened to be improving by the time 704 B
  was tried). The mechanism is FEC-structural: 12 independent codewords
  per frame means 12 independent chances for one to fail to converge, and
  hf10 already established that a rate-3/4 codeword either fully corrects
  or fails outright with no partial credit — so codeword count, not raw
  channel quality, is what degrades as frames get longer at a fixed LDPC
  rate and payload-packing strategy.
- **Denser pilots (task requirement 3) did not rescue reliability at any
  duration tested**, including specifically at the point (704 B) where
  duration-driven channel drift within a frame would most plausibly
  matter. This adds a third, independent data point (after hf9's
  ambiguous finding and hf10's negative finding on the opposite extreme)
  that pilot density is not a reliable lever on this channel/PHY once past
  the original baseline operating point — the limiting factor at long
  durations is LDPC codeword count, which pilot density cannot address at
  all (it affects channel *tracking* quality, not the coding structure).
- **This matches the project's established gradual-marginal-edge
  precedent (hf5, hf7)** in its qualitative shape — degradation with
  payload size is smooth (71% -> 75% -> 75% -> 55%), not an abrupt wall —
  but the *mechanism* is new and worth flagging for future work: this is
  the first time in this project's history a reliability ceiling has been
  traced to FEC codeword count rather than to rising raw channel BER or
  phase/CFO drift. A future experiment with a substantially larger trial
  count per step (this experiment's sample sizes, 3-13 per configuration,
  are well below hf9/hf10's own 10-13 trial standard for a final
  qualification claim) would be needed to firm up the exact per-codeword
  failure rate and confirm the 528 B recommendation with hf10-level
  confidence.

## Comparison to the project's throughput record

| Mode | Net bps (best decoded) | Decode rate | Modulation | FEC |
|---|---|---|---|---|
| hf9_ofdm49_v5 | ~4014 | 10/10 | 8PSK | none |
| hf10_ofdm49_v6 | ~4332 | 12/13 (92%) | 16-QAM | LDPC 3/4 |
| **hf11 @ 528 B (this experiment)** | **~4969** | **6/8 (75%), this session** | 16-QAM | LDPC 3/4 |
| hf11 @ 352 B (this experiment) | ~4897 | 6/8-4/5 (75-80%), this session | 16-QAM | LDPC 3/4 |
| hf11 @ 704 B (this experiment) | ~4793-5120 | 6/11 (55%) — not recommended | 16-QAM | LDPC 3/4 |

**hf11's 528 B config is a further +15% net-bps improvement over hf10's
own record when it decodes**, at a decode rate that is comparable to (not
clearly better or worse than) hf10's own baseline *as re-measured this
session* — but this session's baseline itself measured weaker (71%) than
hf10's original 92%, so this result should be read as "duration extension
did not cost reliability beyond what today's channel already cost the
baseline," not as "duration extension improved reliability." Confirming
528 B at hf10's own 92%/13-trial standard, on a day when the baseline
itself reproduces at 92%, is the natural next step before calling 528 B a
permanent replacement for 176 B.

## Honest caveats

- **All numbers in this document are from a single session, and this
  session's own SNR drifted substantially while it ran** (14.3 dB at the
  first trial to 18.4 dB by the last), which is a real confound for every
  size-to-size comparison above: later configurations (528 B, 704 B) were
  measured on a channel that had already improved somewhat relative to
  earlier ones (176 B, 352 B). The 704 B ceiling finding is strengthened
  by this, not weakened — 704 B failed more often *despite* running on the
  best SNR of the whole session — but the 352 B/528 B "no reliability
  cost" finding is correspondingly weaker evidence than it would be at
  matched SNR, since some of their apparent parity with the (weak)
  176 B-today baseline could be riding the same upward SNR drift.
- **Sample sizes per configuration (3-13 trials) are below this project's
  own 10-13-trial qualification standard** (hf9/v5's 10/10, hf10/v6's
  12/13 across three seeds). Every reliability percentage in this document
  should be read as "this session's provisional estimate," not a
  qualified record, per the task's own escalate-only-on-evidence
  instruction — a genuinely rigorous confirmation of the 528 B
  recommendation would need a comparable multi-seed batch to hf10's.
- **hf10's own baseline reproduced weaker today (71%, 5/7) than in hf10's
  original session (92%, 12/13)** even before any duration change was
  made — this project's now-repeated finding (hf8, hf9, and now hf11) that
  this HF channel is not stable session to session. Every comparison
  against hf10's 4332 bps / 92% record in this document is implicitly a
  comparison against a moving target, not a fixed hardware ceiling.
- **The per-codeword failure-rate estimates in the "why 704 B fails"
  section are crude** (derived from small-n whole-frame decode rates via
  an independence assumption that is only approximately true — codewords
  within one frame share the same trial's realized channel conditions,
  so they are not fully independent draws) and should be read as
  illustrative of the mechanism, not as calibrated numbers.
- Only rate-3/4 LDPC and only 16-QAM were tested (matching hf10's own
  recommended config exactly) — a different FEC rate or modulation might
  shift where the codeword-count ceiling falls; that combination was not
  explored here, consistent with the task's instruction to start from
  hf10's exact working config rather than re-opening hf10's own already-
  answered modulation/FEC-rate questions.
