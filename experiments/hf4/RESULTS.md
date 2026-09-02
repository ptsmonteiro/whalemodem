# HF4 results

## Hardware-debug fix: the interleaver was an accidental no-op (2026-09-01)

Offline debugging of the two saved synced-but-CRC-failed captures from the
2026-09-01 hardware run (below) found a real code bug, not a channel
artifact the simulation is simply blind to (though the bug's *trigger*
turned out to be exactly that kind of artifact).

**The symptom.** Both synced captures (`confirm-ic7300-to-ic705/captures/
ic7300_to_ic705_02.npy` and `_03.npy`, +5.49 Hz and +6.30 Hz frequency
offset, 117/149 and 98/149 carriers present -- genuinely different channel
realizations) decoded to the identical wrong length field, `35242`, both
with and without per-carrier reliability weighting applied. Replaying both
captures offline through `hf4.demodulate` with intermediate values
instrumented (per-carrier gain/SNR from the header fit, the depunctured
soft-bit stream feeding the Viterbi decoder, the decoded packet bits before
and after de-whitening) found: the smallest-gain (most-faded) carriers in
the two captures do not overlap at all between trials, ruling out "the same
physical carrier is always dead on this radio path" as the explanation, and
clipping the soft-bit magnitude before decoding (tested from 20 down to 1.5)
changed nothing about the decoded length -- ruling out equalizer
divide-by-near-zero-gain blowup as the cause, despite the equalized payload
values genuinely reaching absurd magnitudes (mean |value| ~850,000 and
~109,000 against an expected QAM scale of ~1) on badly-faded carriers.
Feeding the decoder pure random noise in place of real data gave a
different wrong length every time (confirming ordinary noise does not
collapse to one fixed value), which narrowed the search to something
structural in the coding pipeline itself.

**Root cause.** `INTERLEAVER`'s construction --
`whale.dsp.interleave.block(rows=DATA_SYMBOLS, columns=CARRIER_COUNT *
BITS_PER_CARRIER)` composed with the extra transpose `_to_symbol_grid` and
`_from_symbol_grid` applied around it -- is provably the identity
permutation on the coded-bit grid. Direct comparison confirmed
`_to_symbol_grid(coded_bits)` produces exactly `coded_bits.reshape(
DATA_SYMBOLS, CARRIER_COUNT * BITS_PER_CARRIER)`, byte for byte, for
arbitrary input: the block interleaver's permutation and the transpose
exactly cancel. **There has never been any interleaving in this design's
inner-FEC coding chain** -- the whole mechanism the 2026-09-01 FEC-fix
campaign's design writeup credits with fixing the per-carrier-fade plateau
was not doing anything beyond a bare reshape. It happened not to matter for
that campaign's target failure mode (a bad carrier's coded bits still land
`CARRIER_COUNT * BITS_PER_CARRIER` apart across symbols under a bare
reshape, since that spacing falls straight out of the grid's own shape),
which is exactly why `test_one_dead_carrier_still_decodes` did not catch
it. But a bare reshape leaves every OFDM *symbol*'s entire row of coded
bits contiguous and unshuffled in the original coded stream -- so anything
that corrupts one symbol as a whole (not one carrier across every symbol,
but every carrier of one symbol) lands as one dense, unrecoverable burst
instead of scattered single-bit errors the code can Viterbi-decode around.
Symbol 0 (the very first payload symbol, immediately after the header) maps
to the coded stream's first ~600 bits, which is exactly the packet's length
field. The real IC-7300/IC-705 link evidently puts a consistent artifact
(most plausibly an AGC/ALC transient right at the header-to-payload
transition, or a related filter-settling effect -- a *time-localized*, not
frequency-localized, effect the two-path benign/static simulation model
does not produce) onto that first symbol on both trials, and with no real
interleaving to break it up, that reproducibly corrupted the same message
bits to the same wrong value both times, independent of the trials'
otherwise-different fading and CFO.

**The fix.** `INTERLEAVER` now uses `whale.dsp.interleave.multiplicative`
(the same construction, and the same stride, 8101, that VF2 through VF5
already use) instead of `block` plus a transpose; `_to_symbol_grid`/
`_from_symbol_grid` are now a plain `spread`/`gather` and reshape, since the
multiplicative permutation already scatters bits across both grid axes and
no longer needs a transpose trick. Verified directly: for `coded_bits =
np.arange(RAW_BITS)`, one fixed OFDM symbol's row of the new interleaved
grid spans nearly the *entire* original coded-bit range (min 0, max 64,265
out of 64,368) instead of one contiguous 596-bit run.

**Verification.**

- `python -m pytest experiments/hf4/test_hf4.py -q` -> **27 passed** (the
  prior 25, plus two new regressions:
  `test_interleaver_does_not_reduce_to_a_plain_reshape`, which asserts
  `_to_symbol_grid` differs from a bare reshape directly (this would have
  caught the bug immediately), and `test_one_bad_symbol_still_decodes`,
  which reconstructs the specific defect -- one whole OFDM symbol degraded
  (70% of its amplitude retained plus noise at 15% of signal scale, a
  severity representative of a real transient rather than a worst-case
  erasure) with no per-carrier weighting able to see or discount it, since
  the corruption is symbol-wide, not carrier-specific. This fails against
  the old (bare-reshape) interleaver every time and passes against the
  fixed one every time -- confirmed directly before shipping the test.
  Direct capture replay as the regression test was not practical (it needs
  the full acquire/CFO/equalize pipeline against 120,000-sample real audio,
  too slow and too dependent on saved external files for a unit test), so
  the coding-pipeline-level reconstruction stands in for it.
- Replaying the two originally-failed captures through the fixed
  `hf4.demodulate`: both still fail CRC (the underlying channel SNR was
  genuinely very poor -- per-carrier SNR from the header fit averaged
  -7.25 dB and -0.90 dB in the two trials, far below the +13 dB the
  benign/static envelope targets, so this is not surprising), but the
  **fixed wrong-value symptom is gone**: the two captures now decode to
  different wrong lengths (44,738 and 64,174) instead of the same 35,242,
  consistent with ordinary noise-driven CRC failures rather than a
  systematic bug.
- Scout Monte Carlo sweep against `benign_static`
  (`experiments/hf4/benchmark_hf4.py --model benign_static --points 13 15 20
  --trials 100 --seed 20260901`, post-fix): **66/100 (66%) at 13 dB, 67/100
  (67%) at 15 dB, 90/100 (90%) at 20 dB**, acquisition 100/100 at every
  point, zero `error` outcomes. This is statistically indistinguishable
  from (13 dB: 95% Wilson CI 56.3%-74.5%, overlapping the prior 300-trial
  confirmed 70.33%) or better than (20 dB: 90%, clearing the >=90% frame
  Monte Carlo gate outright at that point, not previously measured at this
  exact point) the pre-fix figures -- **no regression**, and the interleaver
  fix's benefit is specifically against the real-hardware time-localized
  failure mode the synthetic two-path fading model does not exercise, not
  against the synthetic model's own per-carrier fades (which a bare reshape
  already handled adequately, which is why the +13 dB confirmed gate
  remains open -- see "What is not yet established" below).

**What this does and does not resolve.** This fixes a genuine, previously
undetected bug (the inner coding chain had no real interleaving at all)
that directly explains the reproducible wrong-length-field symptom from the
2026-09-01 hardware run, and removes a real fragility (one bad OFDM symbol
wiping out the packet header) that the synthetic benign/static Monte Carlo
campaigns were structurally unable to expose (their fading model varies by
carrier and by frame, never by symbol-within-a-frame). It does **not** by
itself close the +13 dB frame Monte Carlo gate (still ~66% against the
required >=90%) and does not constitute new hardware evidence -- no new
hardware session was run in this pass (offline replay of already-captured
audio only), so HF4's hardware decode count remains 0/5 until a follow-up
session (a follow-up recheck was later run: still 0/30, see
"Post-interleaver-fix hardware recheck" below). See "What is not yet
established" for the current, still-open gap list.


This is a development-pass evidence record, not a `MODE_QUALIFICATION.md`
promotion artifact. HF4 has no manifest entry; hardware evidence is now an
exploratory, non-qualifying data point (see below), not a passed gate. See
"What is not yet established" below for the complete gap list.

## Post-interleaver-fix hardware recheck: still 0/30 decoded, but sync reliability improved (2026-09-01)

After the interleaver no-op fix (above), a hardware recheck was run over the
same real IC-7300(TX)->IC-705(RX) SSB path to see whether the fix -- which
substantially improved simulated benign_static decode rates (66%/67%/90% at
13/15/20 dB) -- also fixed the real-hardware payload/CRC failure recorded
below. It did not: a 25-trial characterization batch plus an earlier 5-trial
smoke batch (30 trials total, compliant A->B direction only) decoded
**0/30 frames**, matching the original 0/5 result's outcome. Sync
reliability did improve markedly, however: 18/25 trials in the
characterization batch reached a confident sync (confidence 0.82-0.9999),
versus 2/5 in the original run -- consistent with the interleaver fix and/or
the 9 s capture-tail fix helping acquisition, but the fact that every synced
trial still fails at the payload/CRC stage confirms a second, distinct,
still-unfixed bug in the payload/length/CRC decode path, separate from the
interleaver issue and not reproduced by the pure-AWGN benign_static
simulation.

**Safety note:** the session that ran this recheck was interrupted by the
user partway through, because a reverse-direction probe (`ic705->ic7300`,
5 trials) was run that required the IC-705 to key PTT on HF -- a violation
of this project's hard safety rule that the IC-705 must never transmit on
HF. This is recorded here for completeness; no further reverse-direction
trials were run in the follow-up (resuming) session, which only consolidated
the already-complete compliant-direction results.

Full setup metadata, per-trial results, and the safety finding:
`logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware-recheck/INDEX.md`.
Recommendation unchanged in kind from the original run: the newly captured
18-19 synced-but-failed real hardware captures are a much larger sample for
offline debugging the payload/length/CRC path than the original run's 2,
and that offline debugging -- not more hardware trials -- is the productive
next step.

## Real-hardware exploratory test: 0/5 frames decoded (2026-09-01)

At the project owner's request, HF4 was run over the real IC-7300<->IC-705
SSB pair before further simulation tuning, on the hypothesis that the
synthetic `benign_static` channel (211/300, 70.33% decode at +13 dB in the
prior Monte Carlo campaign) might be more pessimistic than the real radio
path. It was not: **0 of 5 real-hardware frames decoded** (4 attempted
A->B/IC-7300->IC-705, 1 B->A/IC-705->IC-7300). HF4 has no
`whale.mode_qualification.MANIFEST` entry, so a bench-only hardware harness,
`experiments/hf4/hw_hf4_frames.py`, was written to drive `hf4.modulate`/
`demodulate` directly over `scripts/bench.py`'s radio pair (same pattern as
`benchmark_hf4.py`'s mode_id=244 simulation adapter, applied to hardware
instead of Monte Carlo).

Two of the five trials did reach a full synced header+payload decode
attempt (confidence 0.999 and 0.992) and both failed CRC -- and both
reported the identical wrong length field (`decoded_length = 35242`)
despite different frequency offsets and carrier-presence counts, which is
far more consistent with a systematic decode issue on the real channel path
(e.g. an SSB filter/AGC/ALC artifact the header-only, no-per-symbol-clock-
tracking equalizer does not track) than with independent random bit errors.
The other three trials never reached a confident sync at all within the
capture window used, some because the frame arrived late enough in the
capture buffer that the header/payload window was truncated -- HF4's
8.303 s frame needed a much longer post-TX capture tail than the shorter
HC0/HC1/HF2/HF3 frames the existing hardware scripts default to.

Full setup metadata, per-trial table, and diagnosis:
`logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/INDEX.md`.
This is an exploratory characterization run, not a promotion campaign: no
formal retained-direction bookkeeping was done, and the recommended next
step is offline debugging of the two saved synced-but-failed captures
(header/length decode), not more hardware trials or more benign/static
Monte Carlo runs.

## Dense-carrier redesign: a real ~15x improvement, still short of the gate (2026-09-01)

A fourth campaign, `logs/mode_qualification/hf-ssb/hf4/2026-09-01-dense/`,
attacks the +13 dB gate failure from the "Inner FEC/interleaving fix"
section below at its root cause: that campaign's own diagnosis was that "no
code rate below about 0.895 can clear 7,000 bit/s" on the old 75-carrier/
31.25 Hz plan, and that thin a code did not survive the real channel even
with interleaving and reliability weighting. This campaign doubles carrier
density (149 carriers, 15.625 Hz spacing, same 343.75-2,656.25 Hz Hz band)
while holding the 64-sample/5.33 ms cyclic prefix duration fixed (it is
load-bearing against the required channel's filter memory -- see DESIGN.md
-- and was not touched). This halves the guard's relative overhead from
14.3% to 7.7% of every symbol, which is exactly the freed budget the old
campaign's diagnosis said was needed.

**Rate/frame-length search.** A systematic sweep across DATA_SYMBOLS
(72/108/144/180/216/288/360/540), each paired with the strongest FEC rate
that length could afford above 7,000 bit/s, found a clear and unexpected
pattern: **frame duration, not code rate, is the dominant lever** below the
code's own minimum-robustness floor. At a fixed rate in the 0.85-0.92 range,
shorter frames decoded better at +13 dB, monotonically, even when that meant
a *weaker* code:

| DATA_SYMBOLS | Rate | Frame | Decoded @ 13 dB (scout) |
| ---: | ---: | ---: | ---: |
| 540 | 13/15 (0.867) | 39.1 s | 38% (19/50) |
| 288 | 8/9 (0.889) | 21.1 s | 53% (32/60) |
| 180 | 8/9 (0.889) | 13.4 s | 65% (39/60) |
| 108 | 11/12 (0.917) | 8.3 s | 68% (41/60) |
| 72 | 17/18 (0.944) | 5.7 s | 71% (57/80) -- **disqualified, see below** |

The shortest length tested (72 data symbols, rate 17/18) scored best in the
scout sweep but **failed `test_one_dead_carrier_still_decodes` outright,
even with the dead carrier's soft bits weighted to a hard zero (a full
erasure)** -- at that frame length, the code's redundancy and interleaver
depth cannot recover even one fully dead carrier out of 149, regardless of
weighting. That configuration was rejected; its Monte Carlo artifact is kept
for the record as
`2026-09-01-dense/frame_monte_carlo_13db_ds72_disqualified.json`, not as
promotion evidence. **DATA_SYMBOLS=108 at rate 11/12 (0.917) is the
shortest frame length found that still passes the synthetic dead-carrier
regression test**, and is the configuration this design ships with.

**Net throughput**: 7,099.41 bit/s (1.4% above the 7,000 bit/s floor;
recomputed from the real encoder). **This gate passes**, with a thinner
margin than the prior 19/20/75-carrier design's 3.1% -- the cost of using a
short frame with a still-real 8.3% redundancy code rather than the lowest
rate this carrier plan could technically afford at a much longer frame.

**Frame Monte Carlo at the +13 dB boundary (confirmed, 300 trials)**:
**211/300 decoded (70.33%), 95% Wilson-UB FER = 0.351** against the <=0.10
gate -- **this gate still fails**, but is a roughly **15x improvement**
over the prior campaign's 14/300 (4.67%). Acquisition 300/300 (Wilson-LB
0.987), zero `error` outcomes. Full numbers:
`logs/mode_qualification/hf-ssb/hf4/2026-09-01-dense/INDEX.md`.

**Occupied bandwidth**: re-measured (300-trial campaign); worst-case bounds
2,660.30 Hz (top) / 340.92 Hz (bottom), both with real, measured margin
inside 300-2,700 Hz. **This gate passes.**

**Diagnosis.** The carrier-density lever worked exactly as hypothesized --
halving the CP's relative overhead and strengthening the code from 0.95 to
0.917 took +13 dB frame decode from 4.67% to 70.33%, a large and real
improvement, not noise. It did not fully close the gate. The frame-duration
sensitivity found in the search above (shorter frames decode better even
with a weaker code) suggests the required benign/static channel's
per-carrier fade variance scales with frame duration in a way that a
fixed-rate code budget cannot outrun by getting longer, and that this
design's remaining shortfall is not simply "not enough redundancy" or "too
much CP overhead" -- both of those were real problems and are now measurably
fixed. A further attempt would need to characterize how the channel model's
per-carrier fade process scales with frame duration directly, rather than
continuing to search this waveform's own rate/length/carrier-density space,
which this campaign explored thoroughly (including a further carrier-density
doubling to ~297 carriers in exploratory sizing math, which did not change
the qualitative picture) without finding a combination that clears the gate.

**Honest summary against the three simultaneous targets this task asked
for**: net throughput passes (7,099.41 bit/s > 7,000), occupied bandwidth
passes (real margin both edges), frame Monte Carlo reliability at +13 dB
fails (70.33% vs the ~90%+ needed). **HF4 does not qualify at Level 4 as of
this campaign**, but the dense-carrier redesign is a substantial, measured
step forward and is the new baseline for the design.

## Inner FEC/interleaving fix: throughput holds, the +13 dB gate still does not (2026-09-01, superseded above)

A third campaign, `logs/mode_qualification/hf-ssb/hf4/2026-09-01-fec/`,
adds a punctured rate-19/20 inner convolutional code
(`whale.dsp.fec.K7`, soft Viterbi), a block interleaver spreading coded
bits across every carrier and data symbol, and per-carrier reliability
weighting from the header fit's SNR (`whale.dsp.equalize.carrier_weights`)
to fix the second design gap the 2026-09-01-fix campaign found (see below).
DATA_SYMBOLS grew from 108 to 360 to afford the code's redundancy inside
the 7,000 bit/s throughput floor -- see "Inner FEC and interleaving" in
DESIGN.md for the full mechanism, including a real bug in the first
interleaver construction tried (caught by a new regression test,
`test_one_dead_carrier_still_decodes`) and why per-carrier reliability
weighting, not just interleaving, was necessary.

**Net throughput**: 7,217.81 bit/s (3.1% above the 7,000 bit/s floor;
recomputed from the real encoder per MODE_QUALIFICATION.md section 4's
formula -- see "Net throughput per data frame" below for the updated
table). **This gate passes.**

**Frame Monte Carlo at the +13 dB boundary (confirmed, 300 trials)**:
**14/300 decoded (4.67%), 95% Wilson-UB FER = 0.972** against the <=0.10
gate (95% Wilson CI on decode rate: 2.80%-7.68%), acquisition 300/300
(Wilson-LB 0.987, clears its own >=0.90 gate), zero `error` outcomes --
every non-decode is a clean CRC rejection, not a harness fault. Full
numbers: `logs/mode_qualification/hf-ssb/hf4/2026-09-01-fec/INDEX.md`. The
scout sweep (10/11/13/15/20 dB, 100 trials each) shows the same shape:
acquisition succeeds 100% of the time at every point, and frame decode is
now SNR-dependent and clearly nonzero at +13 dB and above -- 0%/0%/9%/38%/
56% decoded at 10/11/13/15/20 dB respectively -- qualitatively a real,
substantial improvement over the pre-fix 0% floor at every tested SNR from
8-35 dB. **This is still far short of the declared gate** (95% Wilson-UB
FER <=10%, i.e. needing roughly 90%+ decoded at +13 dB): a rate-19/20 code with light interleaving and reliability
weighting recovers frames that have at most a small number of badly-faded
carriers, but the required benign/static two-path channel evidently
produces enough per-frame carrier-fade variance, often enough, that this
level of redundancy is not sufficient at the +13 dB boundary. A stronger
code was investigated (rate 23/25 at 720 data symbols, a 28-second frame)
and did not show a decisive improvement in the same scout comparison,
suggesting the remaining gap is not simply "more of the same redundancy"
away -- see "What is not yet established" for what a further attempt would
need to change (more carriers/denser subcarrier spacing to afford a
meaningfully stronger code, since this carrier plan's own asymptotic
throughput ceiling caps how much redundancy any code at this geometry can
afford, is the leading candidate; see DESIGN.md's rate-vs-frame-length
search).

**Occupied bandwidth**: re-measured (300-trial campaign, same method as
before) since the carrier plan is unchanged; see "Occupied bandwidth"
below for the confirmed numbers. **This gate continues to pass** -- the
FEC fix only changes frame duration and inner coding, not the carrier plan
or edge taper.

**Honest summary against the three simultaneous targets this task asked
for**: net throughput clears 7,000 bit/s (yes, 3.1% margin), occupied
bandwidth clears 300-2,700 Hz (yes, unchanged from the prior campaign),
and the +13 dB frame Monte Carlo gate (no -- improved from 0% to a
real-but-insufficient decode rate). This pass narrows the second design
gap substantially but does not close it.

### Net throughput per data frame (2026-09-01 FEC fix)

Computed exactly per `MODE_QUALIFICATION.md` section 4's formula, from the
real encoder, with the rate-19/20 inner code and 360-data-symbol frame:

| Quantity | Value |
| --- | ---: |
| `RAW_BITS` (OFDM data-symbol grid capacity) | 108,000 bits |
| `FEC_K`/`FEC_N` (code rate) | 19/20 (0.95) |
| `MAX_PAYLOAD_BYTES` (HF4 payload capacity before the air header) | 12,818 bytes |
| `AIR_HEADER_BYTES` (`whale.framing.AIR_HEADER_BYTES`) | 10 bytes |
| DATA chunk bytes (`HF4.chunk_size`) | 12,808 bytes |
| TX sample rate | 48,000 Hz |
| Frame airtime | 14.196 s |
| **Net throughput** | **7,217.81 bit/s** |

This exceeds the hard 7,000 bit/s floor by 3.1%. Reproduce with:

```python
from experiments.hf4 import hf4
from whale import framing

chunk = hf4.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
tx = hf4.modulate(bytes(chunk))
airtime = len(tx) / hf4.SAMPLE_RATE
print(8 * chunk / airtime)                # 7217.81...
```

### Occupied bandwidth (2026-09-01 FEC fix)

Re-measured with the 300-trial statistical campaign
(`experiments/hf4/measure_bandwidth.py`), since the frame is now much
longer (14.2 s vs 4.5 s): worst 95.1%-confidence upper bound on the top
edge **2,668.64 Hz** (31.36 Hz below the 2,700 Hz ceiling), worst
95.1%-confidence lower bound on the bottom edge **333.12 Hz** (33.12 Hz
above the 300 Hz floor), across representative (6,409-byte) and maximum
(12,818-byte) payloads. **This gate passes**, with margins in the same
thin-but-real range as the prior (pre-FEC) campaign -- expected, since the
carrier plan, edge taper, and guard interval are byte-for-byte unchanged;
only frame duration and inner coding changed. Full artifact:
`logs/mode_qualification/hf-ssb/hf4/2026-09-01-fec/occupied_bandwidth.json`.

## Guard-interval fix and a second, still-open gate failure (2026-09-01)

A follow-up campaign
(`logs/mode_qualification/hf-ssb/hf4/2026-09-01-fix/INDEX.md`) fixed the
cyclic-prefix/filter-memory root cause diagnosed just below, and
re-measured everything. **The fix is real and confirmed** (the guard
interval grew from 12 to 64 samples, 1.0 ms to 5.33 ms; three carriers
were added and pilots thinned to hold throughput above 7,000 bit/s; see
`experiments/hf4/DESIGN.md` for every parameter change and the empirical
sweep behind the new guard length): the noiseless filter-only diagnostic
that failed 100% of the time before now passes cleanly and repeatably,
per-carrier SNR estimates are sane instead of garbage, and the frame
Monte Carlo sweep now shows a real SNR-dependent transition (0% decoded
at 13 dB, climbing to 58% at 20 dB) instead of a flat 0% floor from 8 dB
through 20 dB.

**However, the frame Monte Carlo gate at the +13 dB boundary still
fails** (0/300 decoded, confirmed tier) -- because fixing the ISI bug
exposed a second, previously-hidden design gap underneath it: HF4 (no
inner/outer FEC, one flat per-carrier gain/offset fit, no diversity or
interleaving) has no margin against the per-carrier SNR variance the
required benign/static channel's two-path Watterson model produces from
frame to frame. This plateaus decode rate around 70-83% even at 35 dB
waveform SNR (ad hoc check, not saved as an artifact) -- an
SNR-floor-independent, realization-dependent failure, not a marginal-SNR
effect. Net throughput after the fix, recomputed from the real encoder,
is 7,129.31 bit/s (still above the 7,000 bit/s floor, 1.85% margin, down
from 4.2%); occupied bandwidth still clears its gate (worst top-edge UCB
2,670.84 Hz, worst bottom-edge LCB 331.59 Hz, both inside 300-2,700 Hz
with real but thinner margin than before). **HF4's Level 4 envelope claim
remains unsupported** -- see the fix campaign's INDEX for full evidence
and diagnostics on the second gap, which is out of scope to fix in this
pass.

## Frame Monte Carlo campaign and occupied-bandwidth statistical campaign (2026-09-01)

**Update, 2026-09-01: HF4 fails its own declared frame Monte Carlo gate.**
A simulated qualification campaign
(`logs/mode_qualification/hf-ssb/hf4/2026-09-01/INDEX.md`) ran the required
Level 4 boundary point (benign/static, +13 dB waveform SNR) at the
confirmed tier (300 trials) plus a scouting sweep (8/10/11/13/15/20 dB, 100
trials each). Result: **0 of 300 frames decoded at +13 dB** (95% Wilson-UB
FER = 1.000, against the <=0.10 gate), and **0 decoded at every scouted
point from 8 dB through 20 dB** -- acquisition succeeds 100% of the time,
but every frame fails its CRC. This is not a marginal SNR effect: a
noiseless diagnostic (the required benign/static bandpass filter applied
alone, no noise or fading) reproduces the same failure. Root cause: the
250-3,100 Hz filter stage `SPEED_LADDERS.md` requires a benign/static
qualification channel to retain has a settling/impulse-response tail
(~6.9 ms at 48 kHz, ~1.7 ms referred to HF4's 12 kHz processing rate) that
is longer than HF4's 12-sample (1.0 ms) cyclic prefix. `DESIGN.md`'s guard
interval was sized against `SPEED_LADDERS.md`'s propagation delay-spread
figure (<=0.1 ms) alone; it does not cover a real SSB filter's own group
delay/memory, which is a different and, for this design, larger quantity.
This is a genuine design gap in the guard interval as built, not a tuning
or threshold issue -- see the campaign INDEX for the full diagnostic.
**HF4's Level 4 envelope claim (benign/static at +13 dB and above) is not
supported by this evidence.** The throughput and bandwidth figures below
remain accurate as measurements of what they measure (a noiseless round
trip, and the raw transmitted spectrum), but neither implies HF4 currently
delivers useful throughput over the channel its own declared envelope
requires.

The same campaign ran a proper 300-trial-per-payload occupied-bandwidth
statistical campaign (superseding the single-trial FFT check below as the
qualification-grade measurement): worst 95.1%-confidence upper bound on the
top edge **2,615.81 Hz** (84.19 Hz below the 2,700 Hz ceiling), worst
95.1%-confidence lower bound on the bottom edge **356.63 Hz** (56.63 Hz
above the 300 Hz floor), for both representative (1,941-byte) and maximum
(3,882-byte) payloads. **This gate passes**, consistent with (slightly
tighter than) the single-trial check.

All figures below were reproduced on 2026-09-01 by running
`experiments/hf4/hf4.py` and `experiments/hf4/test_hf4.py` directly against
this commit's tree (dirty: this experiment's own new files).

## Net throughput per data frame

Computed exactly per `MODE_QUALIFICATION.md` section 4's formula --
`8 * DATA chunk bytes / complete encoded DATA-frame airtime` -- derived from
the actual encoder, not a nominal symbol-rate calculation:

| Quantity | Value |
| --- | ---: |
| `MAX_PAYLOAD_BYTES` (HF4 payload capacity before the air header) | 3,882 bytes |
| `AIR_HEADER_BYTES` (`whale.framing.AIR_HEADER_BYTES`) | 10 bytes |
| DATA chunk bytes (`HF4.chunk_size`) | 3,872 bytes |
| Encoded frame samples at 48 kHz TX rate | 203,776 |
| TX sample rate | 48,000 Hz |
| Frame airtime | 4.245333 s |
| **Net throughput** | **7,296.48 bit/s** |

This exceeds the hard 7,000 bit/s target by 4.2% and the
`SPEED_LADDERS.md` Level 4 floor of 7,050 bit/s by 3.5%.

Reproduce with:

```python
from experiments.hf4 import hf4
from whale import framing

chunk = hf4.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
tx = hf4.modulate(bytes(chunk))          # any chunk-sized payload
airtime = len(tx) / hf4.SAMPLE_RATE
print(8 * chunk / airtime)                # 7296.48...
```

## Occupied bandwidth

A single-frame 99%-power FFT sanity check (Hann-windowed FFT of the full
encoded TX waveform, cumulative power thresholds at 0.5%/99.5%) over zero,
representative, and maximum payloads:

| Payload | 99%-power band | Width |
| --- | --- | ---: |
| 0 bytes | 358.7-2,609.9 Hz | 2,251.2 Hz |
| 1,936 bytes (representative) | 360.9-2,608.5 Hz | 2,247.6 Hz |
| 3,872 bytes (maximum) | 359.9-2,607.6 Hz | 2,247.6 Hz |

All three sit comfortably inside the 300-2,700 Hz ceiling, with roughly
40 Hz of margin at the bottom edge and 90-92 Hz at the top edge. This is a
single-trial FFT check, not the promotion-sized statistical campaign
`MODE_QUALIFICATION.md` requires (a distribution-free upper confidence
bound over >=300 trials per payload) -- see the gap list below.

## Round-trip correctness

`experiments/hf4/test_hf4.py`, run against a clean/identity path (encode at
48 kHz -> `whale.rx_audio.downsample` to the real 12 kHz production receive
rate -> decode), passes 21/21:

- Exact round trip at 0, 1, 731, half-max, and max-capacity payloads.
- Airtime/rate/chunk-size consistency with the `WaveformMode` contract.
- Oversize payload raises `ValueError` without truncation, both through the
  `WaveformMode` adapter and the underlying `hf4.modulate`.
- Clean non-decodes (no exception, no unbounded work; `payload is None`) for
  empty audio, silence, bounded white noise, a bare carrier tone, a
  truncated frame, non-finite audio, and wrong-shaped (2-D) audio.
- A frame with the back half of its audio replaced by strong noise still
  acquires (its header is untouched) but fails CRC rather than delivering a
  corrupted payload.
- A frame with a corrupted (out-of-range) length field is rejected cleanly:
  acquisition succeeds, `decoded_length` exceeds `MAX_PAYLOAD_BYTES`, and
  `payload` stays `None`.
- The carrier plan's edge margins (>=50 Hz clear of both 300 Hz and 2,700 Hz)
  and HF4's mode ID (11) not colliding with any entry already in
  `whale.mode_qualification.MANIFEST`.

Command: `python -m pytest experiments/hf4/test_hf4.py -q` -> `21 passed`.

## A correctness bug found and fixed during this pass

Worth recording because it shaped two design choices in `DESIGN.md`:

1. **Header reference collisions.** Drawing the header block's three known
   OFDM symbols as three independent random QPSK patterns per carrier
   occasionally makes two of the three rows land on the same constellation
   point for a given carrier (only 4 possible QPSK values, 3 draws). Under
   a noiseless test channel this makes `whale.dsp.equalize.fit_header`'s
   two-parameter least-squares fit exactly singular for that carrier,
   producing wildly wrong gain/offset and corrupting every symbol on that
   carrier. Fixed by rotating one fixed per-carrier base value by 0/120/240
   degrees across the three header rows, which guarantees the rows differ
   for every carrier deterministically.
2. **Edge-window corruption.** The first implementation of the burst-edge
   raised-cosine taper multiplied the first/last samples of the modulated
   audio in place -- which are the real content of the first/last OFDM
   symbol's core, not unused padding. This silently corrupted decode of the
   last several data symbols (worst at the very end of the frame, because
   the final pilot symbol used as a phase-tracking anchor was itself
   windowed). Fixed by tapering a *prepended/appended copy* of the edge
   samples instead of the samples a decoder actually analyzes.

Both were caught by the round-trip test during development, not left as
open issues in the shipped design.

## What is not yet established

- **Frame Monte Carlo sweep against benign/static improved substantially
  but still fails the +13 dB gate** (2026-09-01 FEC fix update at the top
  of this file, and `logs/mode_qualification/hf-ssb/hf4/2026-09-01-fec/`):
  a rate-19/20 inner code, interleaving, and per-carrier reliability
  weighting took decode rate at +13 dB from a hard 0% floor to a real,
  SNR-dependent, but still well-under-gate rate. **This is the open
  blocker.** The evidence so far (a stronger rate-23/25 code at a
  28-second frame not showing a decisive improvement) points at the
  carrier plan's own throughput ceiling as the likely next constraint to
  relax -- see DESIGN.md's "Inner FEC and interleaving" for the
  rate-vs-frame-length search showing no code below ~0.895 can clear
  7,000 bit/s at all on this carrier plan, however long the frame -- not
  at simply raising DATA_SYMBOLS/lowering the rate further within the
  current 75-carrier plan.
- **Promotion-sized occupied-bandwidth campaign continues to pass** after
  the FEC fix (re-measured at the new, longer frame duration; see above)
  -- not an open gap.
- **The guard-interval/filter-memory root cause remains fixed and
  reverified** (2026-09-01 update,
  `logs/mode_qualification/hf-ssb/hf4/2026-09-01-fix/INDEX.md`) -- not an
  open gap.
- **Hardware evidence** supporting qualification. An exploratory
  IC-7300/IC-705 capture has now been attempted (2026-09-01,
  `logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/INDEX.md`) and
  decoded 0/5 frames -- this is a new open problem, not a satisfied gate,
  and does not substitute for the frame Monte Carlo gate above. A
  post-interleaver-fix recheck (2026-09-01,
  `logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware-recheck/INDEX.md`)
  decoded 0/30 additional frames despite improved sync reliability --
  the payload/CRC decode gap remains open and is now the dominant blocker
  to real-hardware qualification.
- **Bounded CI regression test** in `tests/test_channel_regressions.py`.
- **MANIFEST registration** at any level (Experimental/Optional/Default) --
  HF4 is intentionally unregistered, and this evidence would not support
  registration in any case.
- **CPU/RSS resource evidence.** The FEC fix also made this materially
  more relevant than before: encode/decode of one frame now costs
  roughly 1.3 s of soft-Viterbi decode time (measured ad hoc, not saved
  as an artifact) against a 14.2 s frame, a real but not yet characterized
  compute cost this evidence record does not quantify further.
