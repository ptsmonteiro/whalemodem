# hf12_sc_fec_v7 — FEC on the v1 single-carrier PHY

Goal: take the v1 baseline (`experiments/hf5_8psk_4k/sc.py`, 8PSK @ 1500
baud, no FEC, ≈4050 bps net, read-only reference) and see whether adding
LDPC FEC (`experiments/qpsk29/ldpc.py`, IEEE 802.11n QC-LDPC, rates 1/2,
2/3, 3/4, read-only) can push net throughput past it, or past the current
best committed record (`experiments/hf10_ofdm49_v6`, 49-carrier OFDM +
16-QAM + LDPC 3/4, ≈4332 bps qualified; `hf11_ofdm49_v6_duration`'s
≈4969 bps is provisional, n=8).

New module `sc_fec.py` reuses `sc.py`'s carrier, RRC pulse shaping, PN
preamble, mid-frame pilot tracking (linear complex-gain interpolation),
length+CRC32+PN-whitened framing, and symbol mapping tables by import (not
copy) -- `hf5_8psk_4k` itself was never touched. Added: an optional LDPC
codec stage between the whitened bitstream and the symbol mapper, a
max-log-MAP soft-bit LLR demapper (same brute-force-constellation-table
pattern as `hf10_ofdm49_v6`'s `_soft_bit_llrs`, reimplemented locally since
that module isn't meant to be imported cross-experiment), and a block
interleaver that spreads each 648-bit LDPC codeword evenly across the
whole coded-bit stream in time (rows = codewords, write row-major, read
column-major) -- the hypothesis being that this channel's constellation
corruption (ALC/compression transients, established as the mechanism
behind 16-QAM's real-hardware fragility) is time-local/bursty, and LDPC
needs a codeword's bits spread out in time to correct it.

## Honest arithmetic gate, before any airtime

`net_bps = payload_bits / frame_seconds`, with `frame_seconds` including
preamble + pilot overhead. Coded net rate per symbol = `bits_per_symbol *
code_rate`. Key established constraints from `hf5_8psk_4k/RESULTS.md`
(not re-litigated, only applied here):

- Hard bandwidth wall around 2000-2200 Hz occupied bandwidth -- 1500 baud
  (occupied ≈2025 Hz at beta=0.35) is close to the practical ceiling for
  order>=3 modulation; SNR craters at 2000+ baud for 8PSK.
- At 1500 baud the channel has ample SNR margin (15-20 dB), i.e. margin is
  not what limits uncoded 8PSK -- 1500-baud 8PSK is already SNR-headroom-rich
  and FEC's only possible lever there is trading coding-rate tax for either
  higher order or higher baud, not for more margin it doesn't need.
- 16-QAM's failure without FEC is a real-hardware-only, SNR-independent
  fragility (never reproduces in this project's AWGN sim), previously only
  fixed by LDPC 3/4 spread across many OFDM subcarriers (`hf10`).

Given those, the reachable ceiling for *any* FEC'd config on this single
carrier is bounded by `bits_per_symbol * code_rate` at the best baud this
channel supports for that order, since framing overhead (preamble+pilot)
is the same architecture on both sides and only amortizes with frame size,
asymptoting from below. Computed net_bps (payload/frame, pilot_interval
150, before touching the radio):

| Config | baud | bps | rate | eff bits/sym | packet_bytes | frame (s) | net_bps (arithmetic) |
|---|---|---|---|---|---|---|---|
| 8PSK uncoded (v1 baseline, round 3) | 1500 | 3 | none | 3.00 | 2994 | 5.91 | 4048 |
| 8PSK + LDPC 3/4 | 1500 | 3 | 3/4 | 2.25 | 2200 | 5.91 | 2970 |
| 16-QAM + LDPC 1/2 | 1500 | 4 | 1/2 | 2.00 | 1000 | 3.01 | 2640 |
| 16-QAM + LDPC 3/4 | 1500 | 4 | 3/4 | 3.00 | 1000 | 2.07 | 3845 |
| 16-QAM + LDPC 3/4 (larger frame) | 1500 | 4 | 3/4 | 3.00 | 3500 | 6.94 | 4030 |
| 16-QAM + LDPC 3/4 (asymptote, ~13 s frame) | 1500 | 4 | 3/4 | 3.00 | 6500 | 12.8 | 4072 |
| 8PSK + LDPC 3/4 @ higher baud | 2000 | 3 | 3/4 | 2.25 | 1500 | 3.00 | 3982 |

**Gating conclusion:** every rate available in `ldpc.py` (1/2, 2/3, 3/4)
either directly reduces the effective bits/symbol below uncoded 8PSK's 3.0
(rates 1/2 and 2/3, and 3/4 on 8PSK itself, all net *less* than 3.0
eff bits/sym) or, at best, exactly *ties* it: 16-QAM + rate-3/4 gives
`4 * 0.75 = 3.00` eff bits/sym, identical to uncoded 8PSK's raw 3 bits/sym.
Since both share the same preamble/pilot architecture, 16-QAM+3/4 can only
approach the uncoded-8PSK asymptote from below as frames get larger, never
exceed it -- this was already noted qualitatively in
`hf5_8psk_4k/RESULTS.md` round 2 ("coding 16-QAM could only ever tie
8PSK@1500, never beat it") and this round's arithmetic confirms it
quantitatively. No rate/order combination available in this project's LDPC
library can beat the v1 baseline on this single carrier, and all of them
fall well short of the OFDM line's 4332 bps qualified record. Per the
methodology ("only pursue real-hardware trials for combinations that
plausibly beat the current record"), that ruled out chasing a new
*throughput* record here -- but 16-QAM+LDPC-3/4 was still worth confirming
on real hardware, because the interesting open question wasn't "can it
beat 4050/4332 bps" (arithmetic says no) but "does FEC finally make
16-QAM *usable* on this single carrier at all", given 16-QAM's established,
SNR-independent real-hardware fragility. That's a qualitatively different,
useful result even at a lower bps ceiling, so real-hardware trials were
run to test it (not to chase a new record).

## Real-hardware results

All trials: IC-7300(TX) -> IC-705(RX) only, `bench.radio_pair`, 1500 baud,
RRC beta=0.35, carrier 1500 Hz, pilot_interval=150 (identical pilot
mechanism to `hf5_8psk_4k` round 3), block-interleaved LDPC unless noted.
Every trial reports raw (pre-FEC, hard-decision) BER against the full
coded+interleaved ground-truth bitstream and residual (post-FEC) BER in
the payload domain (see `hardware_test.py`).

| Stage | Config | Payload | Frame (s) | Result | Raw BER | Post BER | Net bps |
|---|---|---|---|---|---|---|---|
| A (sanity) | 8PSK + LDPC 3/4, interleaved | 966 B | 2.59 | **3/3** | 0.0000 | 0.0000 | 2988 |
| B | 16-QAM + LDPC 3/4, interleaved | 966 B | 1.95 | **5/5** | 0.0028-0.0046 | 0.0000 | 3963 |
| C | 16-QAM + LDPC 3/4, interleaved, bigger frame | 3494 B | 6.94 | **5/5** | 0.0001-0.0069 | 0.0000 | **4030** |
| D (ablation) | 16-QAM + LDPC 3/4, **no interleave** | 966 B | 1.95 | **5/5** | 0.0000-0.0024 | 0.0000 | 3963 |

(logs: `logs/mode_qualification/hf-ssb/hf12_sc_fec_v7/20260901T172431Z/`
stage A, `.../20260901T172458Z/` stage B, `.../20260901T172538Z/` stage C,
`.../20260901T172653Z/` stage D)

### What worked

- **Stage A** confirmed the whole FEC pipeline (LDPC encode/decode, soft
  LLR demapper, interleaver, BER accounting) is correct end-to-end on real
  hardware before spending airtime on the riskier 16-QAM case: 3/3, raw
  BER exactly 0 (SNR ~20 dB, comfortably above 8PSK's noise floor), all 16
  codewords converged in 0 min-sum iterations (i.e. the hard-decision
  channel output already satisfied every parity check).
- **Stage B is the actual finding**: 16-QAM + LDPC 3/4 decoded **5/5** on
  real hardware, with a real (non-zero) raw BER of 0.28-0.46% cleanly
  corrected to zero residual errors by LDPC. This is the first time in
  this project that 16-QAM has decoded reliably on a *single carrier* --
  every prior 16-QAM attempt on `sc.py`/`hf5` (uncoded) failed at a
  significant fraction regardless of SNR margin (15-20 dB available both
  there and here). The fragility mechanism established earlier in this
  project (ALC/compression corruption, not additive noise, hence
  SNR-independent) evidently produces error bursts small enough per LDPC
  codeword for a rate-3/4 code to correct outright at this baud/frame size
  -- consistent with, and a single-carrier confirmation of, the same
  16-QAM+LDPC-3/4 recipe `hf10_ofdm49_v6` already established works
  (there, spread across 49 OFDM subcarriers instead of interleaved in time
  on one carrier).
- **Stage C** pushed the payload up to 3494 B (6.94 s frame, matched to
  the arithmetic table's near-asymptotic point) and stayed 5/5, with net
  throughput climbing to **4030 bps** -- within 0.4% of the uncoded 8PSK
  baseline (4048 bps) and confirming the arithmetic-predicted asymptotic
  approach from below. Raw BER varied session-to-session between the five
  trials (0.01%-0.69%) consistent with this project's established
  session-to-session channel drift, but LDPC absorbed all of it (0
  residual errors in every trial, every codeword converged in <=2
  iterations).
- **Stage D (interleaver ablation)** was inconclusive as a controlled
  comparison: it also ran 5/5 with interleaving *disabled*, but at
  noticeably better SNR (18.8-20.0 dB vs stage B's 14.4-15.2 dB) --
  consistent with ordinary session-to-session drift rather than evidence
  that the interleaver is unnecessary. The two stages were not run back to
  back on matched channel conditions, so this project's own controls don't
  support a real that-explains-it conclusion here either way; a same
  session A/B toggle would be needed to isolate the interleaver's
  contribution and wasn't run given the time budget.

### What didn't clear the bar (per the arithmetic gate, not tested on
hardware)

- 8PSK + any LDPC rate: strictly worse than uncoded 8PSK (confirmed by
  stage A's measured 2988 bps, matching the 2970 bps arithmetic
  prediction) -- there is no SNR deficit at 1500 baud for 8PSK to spend
  redundancy fixing, so the coding-rate tax is pure loss.
- 16-QAM + LDPC 1/2 or 2/3: arithmetically worse than 16-QAM+3/4 (lower
  eff bits/symbol), not tested.
- Pushing baud past 1500 (with or without FEC): `hf5_8psk_4k` already
  established the ≈2000-2200 Hz occupied-bandwidth wall is a hard filter
  edge, not a gradual SNR rolloff -- FEC cannot buy back bandwidth that
  isn't there; the 2000-baud 8PSK+3/4 arithmetic estimate (3982 bps) still
  falls short of the 1500-baud uncoded baseline, so it wasn't tested.
- 32/64-QAM: not attempted -- even the more favorable 16-QAM+3/4 case only
  ties the existing baseline; a higher-order constellation needs a
  *higher*-rate code than 3/4 to net any gain at all (this project's LDPC
  library tops out at 3/4), and would also compound 16-QAM's already
  border-line real-hardware margin with a harder constellation, judged not
  worth the airtime.

## Best final candidate and qualification status

**16-QAM (4 bits/symbol) + IEEE 802.11n QC-LDPC rate 3/4, block-interleaved,
1500 baud, RRC beta=0.35, pilot_interval=150, 3494 B payload / 6.94 s
frame: ≈4030 bps net, real-hardware confirmed 5/5, raw BER up to 0.69%
corrected to 0 residual BER.**

- **This does not beat either existing record** -- it sits ~0.4% below
  the already-qualified uncoded-8PSK v1 baseline (4048 bps,
  `hf5_8psk_4k`) and well below the OFDM line's 4332 bps qualified /
  4969 bps provisional records. Per the arithmetic gate above, no
  FEC'd config on this single-carrier PHY, at the code rates available in
  this project's LDPC library, can beat uncoded 8PSK@1500-baud -- 16-QAM
  + rate-3/4 is the ceiling of what's arithmetically reachable here, and
  it lands at parity, not a win.
- **What this experiment does establish**: FEC (specifically rate-3/4
  LDPC) makes 16-QAM reliably decodable on a *single carrier* for the
  first time in this project, at real hardware SNRs (14-20 dB) that had
  previously always failed 16-QAM outright without FEC, confirming the
  16-QAM-fragility-is-SNR-independent-but-FEC-fixable finding from the
  OFDM line transfers to a plain single-carrier design too, not just to
  OFDM's per-subcarrier structure.
- **Qualification status: provisional only.** All stages ran 3-5 trials,
  well below this project's usual 10-13 trial qualification bar; nothing
  here should be treated as a qualified operating point. Given it does not
  beat the existing qualified baseline, it was not pushed to a full
  qualification run -- the honest-arithmetic conclusion (FEC cannot beat
  8PSK@1500 uncoded on this single carrier given the available code rates)
  was the more important, and more airtime-cheap, thing to establish and
  document than running a 13-trial qualification on a config already known
  to be at best a tie.

## Recommendation for future work

If a materially higher single-carrier throughput is wanted, the missing
piece per this round's arithmetic is a **higher-rate LDPC code (e.g. 7/8)**
-- something `hf5_8psk_4k`'s round 2 already flagged as unavailable and not
worth building from scratch there, and this round's numbers make precise
why: only a code rate above `(bits_per_symbol_target /
bits_per_symbol_current) = 3/4` for the *next* modulation order up (i.e.
above 0.75 for 16-QAM to beat 8PSK, or above 0.6 for 32-QAM to beat 8PSK)
can turn "more redundancy" into a net throughput win on this specific,
already SNR-rich, bandwidth-limited channel -- rate-3/4 is exactly the
break-even point for 16-QAM, not a margin. Absent that, the OFDM line
remains the better throughput lever on this channel (more parallel
subcarriers amortizing overhead differently, not a modulation/rate
trade-off on one carrier).
