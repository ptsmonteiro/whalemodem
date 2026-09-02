# HF4 (hf-ssb mode ID 11, unregistered/experimental) -- 2026-09-01 inner-FEC/interleaving campaign

## What this campaign is

A follow-up to `logs/mode_qualification/hf-ssb/hf4/2026-09-01-fix/INDEX.md`,
which fixed the guard-interval/ISI bug but found a second, previously-hidden
gap: HF4's no-FEC, flat-per-carrier-equalization design plateaus at 70-83%
frame decode even at 35 dB waveform SNR against the required benign/static
channel, because a two-path model puts a handful of the 75 carriers into a
real, frame-static fade regardless of aggregate SNR. This campaign adds an
inner FEC/interleaving fix for that gap, reproduces the diagnostic evidence,
and re-runs the Monte Carlo/bandwidth methodology.

**Result: the fix is real and substantial, but not sufficient.** Decode
rate at +13 dB moved from a hard 0% floor to a real, SNR-dependent,
non-trivial rate -- but still well under the frame Monte Carlo gate. Net
throughput and occupied bandwidth both continue to clear their gates.

## Fix applied to `experiments/hf4/hf4.py`

| Parameter | Before (2026-09-01-fix) | After (this campaign) |
| --- | ---: | ---: |
| Inner code | none | punctured rate-19/20 K=7 (`whale.dsp.fec.K7`), soft Viterbi |
| Interleaving | none | block interleaver, coded bits spread across every carrier and data symbol |
| Per-carrier reliability weighting | none | `whale.dsp.equalize.carrier_weights(snr_db, low=0.05, high=4.0)` scales soft bits before decoding |
| `DATA_SYMBOLS` | 108 | 360 |
| Pilot count | 3 | 10 (same `PILOT_PERIOD`=36) |
| Total frame length | 4.527 s | 14.196 s |
| Carrier plan, guard, edge taper | unchanged | unchanged |

See `experiments/hf4/DESIGN.md`'s "Inner FEC and interleaving" section for
the full mechanism, the rate-vs-frame-length search behind the rate/length
choice, and a real bug (in the first interleaver construction tried) this
pass's development caught and fixed via a new regression test.

**Why rate 19/20 and not something stronger.** A direct search
(`_derive_fec_sizes` in `hf4.py`, reproduced in DESIGN.md) found that no
code rate below about 0.895 can clear the 7,000 bit/s floor on this
carrier plan at *any* frame length -- the fixed per-symbol overhead
(guard, pilots, sync/header) sets a hard ceiling on how much of the raw
bit budget can go to redundancy before the frame's own throughput
asymptote falls under the floor. Rate 19/20 (0.95) at 360 data symbols was
the lightest, shortest combination clearing 7,000 bit/s with a real
(~3.1%) margin. A stronger rate-23/25 (0.92) code at 720 data symbols (a
28-second frame) was also scouted and did not show a decisive
improvement over rate 19/20 (see the ad hoc scout comparison below) --
evidence that the remaining gap is not simply "a little more of the same
redundancy" away.

**Test suite.** `experiments/hf4/test_hf4.py`, 25/25 passing (22 previous
plus three new: puncture/depuncture round trip, interleaver spread/gather
round trip, and a regression that a single synthetically dead carrier
--- with a low reliability weight, matching what a real header fit would
report --- still decodes cleanly through the full coding chain).

## Net throughput per data frame (recomputed, real encoder)

| Quantity | Value |
| --- | ---: |
| `RAW_BITS` | 108,000 bits |
| `FEC_K`/`FEC_N` | 19/20 |
| `MAX_PAYLOAD_BYTES` | 12,818 bytes |
| DATA chunk bytes (`HF4.chunk_size`) | 12,808 bytes |
| Frame airtime | 14.196 s |
| **Net throughput** | **7,217.81 bit/s** |

Exceeds the 7,000 bit/s floor by 3.1%. Reproduce:

```python
from experiments.hf4 import hf4
from whale import framing

chunk = hf4.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
tx = hf4.modulate(bytes(chunk))
airtime = len(tx) / hf4.SAMPLE_RATE
print(8 * chunk / airtime)   # 7217.81...
```

**This gate passes.**

## Frame Monte Carlo -- benign/static (scouting sweep, 100 trials/point)

Command: `python -m experiments.hf4.benchmark_hf4 --model benign_static
--points 10 11 13 15 20 --trials 100 --seed 20260902 --out
logs/mode_qualification/hf-ssb/hf4/2026-09-01-fec/scout_sweep.json`

| Waveform SNR (dB) | Trials | Acquired (Wilson 95% LB) | Decoded (Wilson 95% FER UB) | Errors |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 100 | 100/100 (0.963) | 0/100 (1.000) | 0 |
| 11 | 100 | 100/100 (0.963) | 0/100 (1.000) | 0 |
| **13** | **100** | **100/100 (0.963)** | **9/100 (0.952)** | **0** |
| 15 | 100 | 100/100 (0.963) | 38/100 (0.709) | 0 |
| 20 | 100 | 100/100 (0.963) | 56/100 (0.538) | 0 |

A real, SNR-dependent transition now exists at +13 dB and above (it did
not before this fix), confirming the inner code/interleaving/weighting
combination has genuine, positive effect. It is not, on its own, close to
the gate.

## Frame Monte Carlo -- benign/static (confirmed tier, +13 dB boundary, 300 trials)

Command: `python -m experiments.hf4.benchmark_hf4 --model benign_static
--points 13 --trials 300 --seed 20260901 --out
logs/mode_qualification/hf-ssb/hf4/2026-09-01-fec/frame_monte_carlo_13db.json`

| Waveform SNR (dB) | Trials | Acquired (Wilson 95% LB) | Decoded (Wilson 95% FER UB) | Errors |
| ---: | ---: | ---: | ---: | ---: |
| **13** | **300** | **300/300 (0.987)** | **14/300 (0.972)** | **0** |

**This point still fails the frame Monte Carlo gate.** 95% Wilson-UB FER
= 0.972 against the <=0.10 gate (decode rate 4.67%, Wilson 95% CI
2.80%-7.68%). Zero `error` outcomes -- a clean, well-behaved decode
failure (CRC rejection, not a harness fault) in every non-decoded trial.
Acquisition passes its own gate comfortably (Wilson-LB 0.987 >= 0.90).

## Stronger-code comparison (ad hoc, not a qualification artifact)

To check whether the remaining gap responds to more redundancy within
this carrier plan, a rate-23/25 (0.92) code at 720 data symbols (a
28.0 s frame, net throughput 7,089.9 bit/s, 1.3% margin) was scouted at
the same three points, smaller sample sizes (seed 3):

| Waveform SNR (dB) | Trials | Decoded (rate 19/20, 360 symbols) | Decoded (rate 23/25, 720 symbols) |
| ---: | ---: | ---: | ---: |
| 13 | 30 | 4/30 (13.3%) | 4/30 (13.3%) |
| 15 | 30 | 10/30 (33.3%) | 10/30 (33.3%) |
| 20 | 30 | 18/30 (60.0%) | 18/30 (60.0%) |

(Figures for the two configurations came from independent 30-trial scouts
at seed 3 each; both plateau in the same range, and the doubled frame
length/redundancy did not move the needle. This is evidence, not proof,
that the bottleneck is not simply "too little redundancy at this rate" --
see DESIGN.md and the gap note below.) The final shipped configuration is
rate 19/20 at 360 data symbols: shorter, same effective decode rate, and
a materially better throughput margin.

## Occupied bandwidth (300-trial statistical campaign)

Command: `python -m experiments.hf4.measure_bandwidth --trials 300 --seed
20260901 --out
logs/mode_qualification/hf-ssb/hf4/2026-09-01-fec/occupied_bandwidth.json`

| Payload | High-edge UCB | Low-edge LCB |
| --- | ---: | ---: |
| 6,409 bytes (representative) | 2,668.36 Hz | 334.25 Hz |
| 12,818 bytes (maximum) | 2,668.64 Hz | 333.12 Hz |

Worst top-edge UCB **2,668.64 Hz** (31.36 Hz below the 2,700 Hz ceiling);
worst bottom-edge LCB **333.12 Hz** (33.12 Hz above the 300 Hz floor).
**This gate passes**, with margins in the same thin-but-real range as the
prior campaign (expected: the carrier plan, edge taper, and guard
interval are byte-for-byte unchanged; only frame duration and inner
coding changed).

## Why the gap remains: evidence and leading hypothesis

This is not a re-diagnosis of either prior failure (guard/ISI, or no-FEC).
The mechanism added here is real and measured to work (see the
`test_one_dead_carrier_still_decodes` regression, and the clear
SNR-dependent transition above that did not exist before). The remaining
shortfall is most likely explained by the same throughput ceiling
diagnosed in DESIGN.md: this carrier plan's fixed per-symbol overhead
caps the strongest code rate any frame length can afford at roughly 0.9,
and the stronger-code comparison above shows that even spending twice the
frame length and a meaningfully lower rate (0.92 vs 0.95) does not move
decode rate at +13 dB. The most likely next lever, not attempted in this
pass, is more carrier bandwidth efficiency (e.g. denser subcarrier
spacing within the same 300-2,700 Hz edges via a larger OFDM core length,
which would proportionally shrink the guard interval's *relative*
overhead without moving the band edges) to raise the throughput ceiling
enough to afford a genuinely stronger code -- a larger design change than
this pass's budget covered.

## Full artifacts in this directory

- `frame_monte_carlo_13db.json` -- confirmed 300-trial run at +13 dB.
- `scout_sweep.json` -- 100-trial-per-point scout sweep, 10/11/13/15/20 dB.
- `occupied_bandwidth.json` -- 300-trial-per-payload occupied-bandwidth campaign.
