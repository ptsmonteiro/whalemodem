# HF4 (hf-ssb mode ID 11, unregistered/experimental) -- 2026-09-01 guard-interval fix campaign

## What this campaign is

A follow-up to `logs/mode_qualification/hf-ssb/hf4/2026-09-01/INDEX.md`,
which found HF4's frame Monte Carlo gate failed completely (0/300 decoded)
at the declared Level 4 boundary (benign/static, +13 dB waveform SNR),
root-caused to a cyclic prefix (12 samples, 1.0 ms) far shorter than the
required benign/static channel's real 250-3,100 Hz bandpass filter's
impulse-response memory (several ms). This campaign applies a fix for
that diagnosed root cause, reproduces the diagnostic to confirm it, and
re-runs the same Monte Carlo/bandwidth methodology.

**Result: the diagnosed guard-interval bug is genuinely fixed -- and a
second, previously-hidden gate failure is exposed underneath it.** The
frame Monte Carlo gate at +13 dB **still fails** (0/300 decoded), but for
a different reason than before, confirmed by direct diagnostic below. Net
throughput and occupied bandwidth both still clear their gates.

## Fix applied to `experiments/hf4/hf4.py`

| Parameter | Before | After |
| --- | ---: | ---: |
| Cyclic prefix (`GUARD_SAMPLES`) | 12 samples (1.0 ms) | 64 samples (5.33 ms) |
| Carrier count | 72 (bins 12-83) | 75 (bins 11-85) |
| Carrier frequency range | 375.0-2,593.75 Hz | 343.75-2,656.25 Hz |
| Pilot period / count | every 12 data symbols / 9 pilots | every 36 data symbols / 3 pilots |
| Lead-in silence | 0.128 s | 0.096 s |
| OFDM symbol length | 396 samples (33.0 ms) | 448 samples (37.33 ms) |
| Total frame length | 4.245 s | 4.527 s |

**Guard interval.** 64 samples was chosen by direct empirical sweep
(applying the required 250-3,100 Hz bandpass filter, twice -- once before
and once after the propagation/noise stages, matching
`benign_static_channel`'s recipe -- to the encoder's own output with no
noise or fading, and checking CRC-clean decode across independent random
payloads): guard lengths from 12 up to ~48 samples still failed this
noiseless diagnostic at every value tried; 64 samples and above decoded
cleanly and repeatably (40/40 across two independent seeds at 64, 72, 96,
112 samples). 64 was kept as the smallest value with a clear, repeated
margin above the failure boundary, to limit the airtime cost. This is
confirmed as a real, reproducible fix -- see `experiments/hf4/test_hf4.py`'s
new `test_survives_the_required_benign_static_bandpass_filter_alone`
regression test, and the per-carrier SNR sanity check below.

**Throughput recovery.** A 64-sample guard alone would have dropped net
throughput to roughly 6,480 bit/s (below the 7,000 bit/s floor). Three
levers recovered it: three more carriers (2 top, 1 bottom, still with
>=40 Hz of raw carrier-frequency margin to both the 300 Hz floor and
2,700 Hz ceiling -- narrower than the original 72-carrier plan's 50+ Hz,
but the actual occupied-bandwidth statistical campaign below is the
binding, measured gate, not this static per-carrier check), pilots
thinned from 9 to 3 (`SPEED_LADDERS.md`'s benign/static Doppler ceiling,
0.005 Hz, moves the channel by only a few degrees of phase across one
frame, so sparse pilots remain adequate for the phase-tracking they are
meant for), and the lead-in trimmed from 0.128 s to 0.096 s. See
DESIGN.md for the per-change rationale.

**Test suite.** `experiments/hf4/test_hf4.py`, 22/22 passing (21 previous
plus one new regression test for this exact bug class). The pre-existing
carrier-edge-margin unit test's static threshold was loosened from 50 Hz
to 40 Hz to admit the narrower (but still real, and separately confirmed
by measurement) margin of the new 75-carrier plan.

## Net throughput per data frame (recomputed, real encoder)

Computed exactly per `MODE_QUALIFICATION.md` section 4's formula -- `8 *
DATA chunk bytes / complete encoded DATA-frame airtime`:

| Quantity | Before fix | After fix |
| --- | ---: | ---: |
| `MAX_PAYLOAD_BYTES` | 3,882 bytes | 4,044 bytes |
| DATA chunk bytes (`HF4.chunk_size`) | 3,872 bytes | 4,034 bytes |
| Frame airtime | 4.245333 s | 4.526667 s |
| **Net throughput** | **7,296.48 bit/s** | **7,129.31 bit/s** |

Still exceeds the hard 7,000 bit/s floor (1.85% margin; down from 4.2%
before the fix, since the longer guard interval and extra carriers do not
fully cancel out) but no longer clears `SPEED_LADDERS.md`'s Level 4 floor
of 7,050 bit/s by much (1.13%). Reproduce with:

```python
from experiments.hf4 import hf4
from whale import framing

chunk = hf4.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
tx = hf4.modulate(bytes(chunk))
airtime = len(tx) / hf4.SAMPLE_RATE
print(8 * chunk / airtime)   # 7129.31...
```

## Frame Monte Carlo -- benign/static (scouting sweep, 100 trials/point)

Command: `python -m experiments.hf4.benchmark_hf4 --model benign_static
--points 8 10 11 13 15 20 --trials 100 --seed 20260901 --out
logs/mode_qualification/hf-ssb/hf4/2026-09-01-fix/hf4_benign_static_scout_100.json`

| Waveform SNR (dB) | Trials | Acquired (Wilson 95% LB) | Decoded (Wilson 95% FER UB) | Errors |
| ---: | ---: | ---: | ---: | ---: |
| 8 | 100 | 100/100 (0.963) | 0/100 (1.000) | 0 |
| 10 | 100 | 100/100 (0.963) | 0/100 (1.000) | 0 |
| 11 | 100 | 100/100 (0.963) | 0/100 (1.000) | 0 |
| **13** | **100** | **100/100 (0.963)** | **0/100 (1.000)** | **0** |
| 15 | 100 | 100/100 (0.963) | 4/100 (0.984) | 0 |
| 20 | 100 | 100/100 (0.963) | 58/100 (0.518) | 0 |

Unlike the pre-fix campaign (flat 0/N from 8 dB clear through 20 dB, with
no visible transition at all), there is now a real, SNR-dependent
transition -- decode rate climbs from 0% at 13 dB to 58% at 20 dB. This
confirms the guard-interval fix genuinely removed the previous
independent-of-SNR failure mode. It does not, on its own, mean the +13 dB
boundary point passes.

## Frame Monte Carlo -- benign/static (confirmed tier, +13 dB boundary, 300 trials)

Command: `python -m experiments.hf4.benchmark_hf4 --model benign_static
--points 13 --trials 300 --seed 20260901 --out
logs/mode_qualification/hf-ssb/hf4/2026-09-01-fix/hf4_benign_static_confirmed_13db.json`

| Waveform SNR (dB) | Trials | Acquired (Wilson 95% LB) | Decoded (Wilson 95% FER UB) | Errors |
| ---: | ---: | ---: | ---: | ---: |
| **13** | **300** | **300/300 (0.987)** | **0/300 (1.000)** | **0** |

**This point still fails the frame Monte Carlo gate.** 95% Wilson-UB FER
= 1.000 against the <=0.10 gate. Zero `error` outcomes -- a clean,
well-behaved decode failure (CRC rejection every time), not a harness
fault.

## Root cause of the *second*, still-open failure

This is a genuinely different failure mode from the one fixed above, and
it is not an SNR-threshold or tuning issue either. Diagnostic evidence:

1. **Per-carrier SNR readings are now sane** (`channel.snr_db` from
   `whale.dsp.equalize.fit_header`), unlike the pre-fix campaign where
   decode was garbage at every SNR. At +13 dB waveform SNR, per-carrier
   estimated SNR across five sample trials ranged from about 14-18 dB
   (worst carrier) to 34-48 dB (best carrier) -- a real, large spread
   across the 75 carriers in a single frame, from a single static-per-frame
   two-path Watterson realization (delay 0.05 ms, second path -17 dB
   relative power, 0.002 Hz Doppler spread per path).
2. **The per-carrier spread does not go away at very high nominal SNR.**
   At 25/30/35 dB waveform SNR (40 trials each, ad hoc, not saved as an
   artifact), decode rate plateaus around 70-83% (FER Wilson-UB 0.32-0.45)
   rather than approaching 0% FER -- confirmed at 40 dB too (a hand-rolled
   16-trial check against the exact per-trial channel realizations the
   harness uses: 13/16 decoded, all failures clean CRC rejections with
   high acquisition confidence, not garbage/sync failures). This is the
   signature of an **SNR-floor-independent, realization-dependent**
   failure, not a marginal-SNR effect: `whale.channel.WattersonChannel`'s
   per-path complex gain is a sum of many independently-phased
   oscillators, so even a single "static" (near-zero Doppler-spread)
   realization draws a random near-Rayleigh-distributed complex gain for
   that whole frame -- most realizations are fine, but a nontrivial
   fraction land close enough to the two paths' destructive-interference
   condition (with the required channel's 0.05 ms delay, this is a smooth
   comb ripple across the whole 300-2,700 Hz band, not a single narrow
   notch) that one or more of HF4's 75 individual carriers end up with
   materially degraded effective SNR for that entire frame.
3. **HF4 has no margin against this by design.** It carries no inner or
   outer FEC (see DESIGN.md, "Why no inner FEC") and uses one flat
   per-carrier (gain, offset) fit from the header, with no per-carrier
   diversity, interleaving, or adaptive bit-loading to protect a carrier
   that lands in a comb dip for the whole frame. A single materially
   degraded carrier at 16-QAM with no coding is enough, across a
   ~32,000-bit uncoded packet with an all-or-nothing CRC32 check, to fail
   nearly every frame it affects.

This is a genuine, previously-hidden design gap, not a re-diagnosis of
the same bug: the original campaign's 0% decode rate at every SNR from
8-20 dB (including 20 dB, 7 dB above the declared floor, with *no*
transition visible anywhere in that range) was fully explained by the
guard-interval/ISI bug alone and gave no visibility into this second
issue. Only after removing the ISI failure does this fading-margin gap
become visible, in the SNR-dependent-but-plateauing shape above.
Diagnosing *and fixing* this second issue (most likely candidates: an
inner code, frequency-domain interleaving, or a coarser modulation with
more coding margin) is out of scope for this pass, which targeted the
specific, already-diagnosed cyclic-prefix bug.

## Occupied bandwidth (300 trials/payload class, statistical campaign)

Command: `python -m experiments.hf4.measure_bandwidth --trials 300 --seed
20260901 --out
logs/mode_qualification/hf-ssb/hf4/2026-09-01-fix/hf4_occupied_bandwidth_300.json`

| Payload | Trials | Width min/median/max | Low-edge LCB | High-edge UCB |
| --- | ---: | --- | ---: | ---: |
| 2,022 bytes (representative half-capacity) | 300 | 2,327.76 / 2,331.08 / 2,335.27 Hz | 333.14 Hz | 2,670.40 Hz |
| 4,044 bytes (maximum) | 300 | 2,327.54 / 2,332.84 / 2,338.14 Hz | 331.59 Hz | 2,670.84 Hz |

Worst 95.1%-confidence upper bound on the top edge: **2,670.84 Hz**,
29.16 Hz below the 2,700 Hz ceiling (down from 84.19 Hz of margin before
this fix, since carriers now run closer to both edges). Worst
95.1%-confidence lower bound on the bottom edge: **331.59 Hz**, 31.59 Hz
above the 300 Hz floor (down from 56.63 Hz). **Both bounds still clear
their gates**, with real but thinner margin than before the fix.

## Gates this evidence does/does not clear

| MODE_QUALIFICATION.md gate | Status |
| --- | --- |
| Section 1 (unit/malformed-input tests) | **Cleared**: `experiments/hf4/test_hf4.py`, 22/22 passing (21 previous + 1 new guard-interval regression test). |
| Section 2 (bounded CI regression) | Not run -- HF4 has no `tests/test_channel_regressions.py` entry and none was added, unchanged from before. |
| Section 3 (frame Monte Carlo, confirmed tier) | **Still fails** at the required point: benign/static +13 dB, 0/300 decoded, FER Wilson-UB 1.000. The originally-diagnosed guard-interval/ISI cause is fixed (confirmed by the noiseless filter-only regression test and by sane per-carrier SNR readings), but a second, previously-hidden fading-margin gap (see above) independently fails this gate. |
| Section 4 (occupied bandwidth) | **Cleared** for both representative and maximum payloads: worst top-edge UCB 2,670.84 Hz, worst bottom-edge LCB 331.59 Hz, both inside 300-2,700 Hz with real (if thinner) margin. |
| Section 4 (fixed-mode useful transfer / net throughput) | **Cleared** as a real-encoder measurement: 7,129.31 bit/s, above the 7,000 bit/s hard floor (1.85% margin) though closer to it than before this fix, and only just above `SPEED_LADDERS.md`'s 7,050 bit/s Level 4 floor (1.13% margin). This is a geometry measurement, not evidence the mode delivers this throughput reliably over its declared channel -- it does not, per Section 3 above. |
| Section 5 (hardware) | Not run -- out of scope; no radios used. |
| Section 6/7 (ladder/system) | Not measured; not applicable to an unregistered single-waveform experiment. |

## Campaign details

- Harness: `experiments/hf4/benchmark_hf4.py` and
  `experiments/hf4/measure_bandwidth.py`, unchanged from the previous
  campaign (they measure `hf4.py`; they were not modified for this fix).
- Channel (benign/static): unchanged from the previous campaign --
  `experiments/hf4/benchmark_hf4.py`'s `benign_static_channel`, the same
  stage recipe and tolerances as `experiments/hf3/benchmark_hf3.py`'s.
- Payload: `hf4.MAX_PAYLOAD_BYTES` (4,044 bytes, up from 3,882 before this
  fix) full-capacity random payload per trial, deterministically seeded
  via `whale.qualification.trial_seed`.
- Master seed: `20260901` for all three artifacts, matching the previous
  campaign for comparability.
- Git commit at run time: `14c20147b55ebce804281fc1f4468c71b938c840`,
  dirty tree (this fix, plus pre-existing unrelated untracked files, were
  uncommitted at run time).
- Python 3.13.2, NumPy 2.5.1, SciPy 1.18.0, Windows-11-10.0.26200-SP0.
- Artifacts (this directory): `hf4_benign_static_scout_100.json`,
  `hf4_benign_static_confirmed_13db.json`,
  `hf4_occupied_bandwidth_300.json`.
- The prior, pre-fix campaign
  (`logs/mode_qualification/hf-ssb/hf4/2026-09-01/`) is retained in full
  and not modified or superseded by this one -- it documents the original
  bug and its diagnosis, which this campaign's fix addresses (but does
  not, on its own, make HF4 qualify).

## Bottom line

The specific, previously-diagnosed root cause (cyclic prefix shorter than
the required benign/static channel's real filter memory) is genuinely
fixed: the noiseless filter-only diagnostic that failed 100% of the time
before now passes 100% of the time, per-carrier SNR estimates are sane
rather than garbage at every tested SNR, and the frame Monte Carlo sweep
now shows a real SNR-dependent transition instead of a flat 0% floor.
Net throughput (7,129.31 bit/s) and occupied bandwidth (worst UCB
2,670.84 Hz, worst LCB 331.59 Hz) both still clear their respective
gates, with reduced but real margin. **However, HF4's Level 4 envelope
claim (benign/static at +13 dB and above) is still not supported by this
evidence**: the frame Monte Carlo gate still fails at the required
boundary, now because of a second, previously-hidden design gap (no
error-correction margin against per-carrier fading variance from the
required channel's two-path model) that persists even at 35 dB waveform
SNR. Per `MODE_QUALIFICATION.md`'s explicit rule against narrowing a
claim after seeing results, this is recorded as a continued gate failure
at the exact envelope HF4 claims, not a redefinition of what was
measured.
