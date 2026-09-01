# hf8_band_placement_v4 — band-placement real-hardware results

**Bottom line: clustering carriers into the empirically best-SNR region did
NOT beat the single-carrier baseline, and only roughly matched (did not
clearly beat) `hf6_multicarrier_v2`'s wide-spread 3-carrier result.** Best
config found: 2 carriers at 1500/2200 Hz (700 Hz spacing), 8PSK, 600 baud
each, mid-frame pilots, ~3.76 s frames, **≈3140 bps net**, 5/5 decoded. That
is below `experiments/hf5_8psk_4k`'s single-carrier 8PSK@1500baud result of
**≈4050 bps** and statistically indistinguishable from
`experiments/hf6_multicarrier_v2`'s 3-carrier result of **≈3200 bps**. All
numbers below are real over-the-air IC-7300(TX) -> IC-705(RX) audio-coupled
trials (`bench.radio_pair`), no simulation used for any go/no-go call.
IC-705 was never keyed (`--direction` hardcoded to `ab`, matching every
prior experiment in this series).

This experiment does not introduce new PHY code: `hardware_test.py` imports
`experiments/hf6_multicarrier_v2/mc.py`'s `CarrierSpec`/`MultiCarrierMode`
verbatim and only varies carrier centre frequencies via `--carrier-hz`.
`hf5`, `hf6`, `hf7`, and `path_probe` were not modified.

## Step 1 — real-time SNR-vs-frequency sweep (today's conditions)

Single carrier (`MultiCarrierMode` with N=1), BPSK, 300 baud, 10 B payload,
3 trials per frequency, no pilots. This is a fast probe, not a throughput
measurement — the point was to map today's actual SNR shape before
committing to a placement, since the task flagged that conditions may have
drifted since `hf5_8psk_4k`'s data.

| Carrier (Hz) | SNR range (dB), 3 trials |
|---|---|
| 700 | 14.5 – 16.0 |
| 900 | 12.3 – 14.1 |
| 1100 | 10.2 – 13.8 |
| 1300 | 12.2 – 15.0 |
| 1500 | 15.4 – 17.4 |
| 1700 | 16.3 – 17.0 |
| 1900 | 15.8 – 16.8 |
| 2100 | 12.4 – 14.8 |

**Finding: the channel shape has drifted from `hf5_8psk_4k`'s original
data and does NOT match the task's stated low-frequency-favoring skew.**
`hf5_8psk_4k`'s RESULTS.md reported ~16 dB at 1200 Hz collapsing to ~8 dB at
2000 Hz and ~2 dB at 2400 Hz — a strong low-frequency preference. Today's
sweep instead shows a **peak in the upper-middle band (1500–1900 Hz, ~15–17
dB)**, a **dip at 1000–1300 Hz (~10–14 dB)**, and decent-but-not-best values
at both the low end (700 Hz, ~14–16 dB) and 2100 Hz (~12–15 dB). This is
close to a mild bimodal/humped shape, not a monotonic low-frequency
preference — most likely day-to-day variation in the IC-7300/IC-705 SSB
filter alignment, ALC state, or ambient RF noise floor, not a fixed
property of the hardware. This matters: it means the "cluster in
600–1800 Hz" placement suggested by the task brief (based on `hf5`'s older
data) is not actually where today's best SNR is — the real best region
today is closer to 1500–1900 Hz. The placement search below follows
today's measured shape rather than the older data.

## Step 2 — carrier-spacing sensitivity within the good region

Two carriers, QPSK, 600 baud, 10 B payload/carrier, no pilots, 3 trials.
600 baud RRC(beta=0.35) occupies ≈600×1.35=810 Hz per carrier.

| Config | Spacing | Result |
|---|---|---|
| 1500 + 1900 Hz | 400 Hz (< occupied BW) | **0/3, all crc_fail** — SNR collapsed to 3.2–3.9 dB on both carriers, far below either carrier's ~15–17 dB solo SNR from step 1 |
| 700 + 2100 Hz | 1400 Hz (wide-spread control) | 3/3, SNR 9.3–10.2 dB (700 Hz) / 12.0–13.2 dB (2100 Hz) |
| 1500 + 2200 Hz | 700 Hz | 3/3, SNR 12.1–13.6 dB (1500 Hz) / 9.9–11.8 dB (2200 Hz) |
| 1500 + 2300 Hz | 800 Hz | 3/3, SNR 11.8–13.5 dB (1500 Hz) / 9.2–11.8 dB (2300 Hz) |

**Finding: placing two carriers closer together than their occupied
bandwidth causes catastrophic mutual interference (SNR collapses ~12 dB,
not the gentler few-dB-per-carrier "SNR division" `hf6_multicarrier_v2`
found for wider spacings)** — consistent with `hf6`'s own step D8→D9/D10
observation that spacing matters more than any other single knob, just a
sharper version of it here (400 Hz spacing is well under the 810 Hz
occupied bandwidth at 600 baud, versus `hf6`'s 600 Hz vs 700 Hz comparison
which was much closer to the edge). Once spacing clears the occupied
bandwidth (700–1400 Hz tried here), the clustered (1500/2200, 1500/2300)
and wide-spread (700/2100) configs land in a similar SNR range (9–14 dB) —
**clustering into the "best" single-carrier-probe frequencies did not
produce a decisively higher aggregate SNR than the wide-spread control**,
because putting two carriers inside a narrow high-SNR band still forces
one of them toward the edge of that band (2200/2300 Hz sit past the 1900 Hz
peak, back down in the ~10–12 dB range) — the good region found in step 1
is not wide enough to fit two adequately-spaced carriers without one of
them spilling out of it.

## Step 3 — scaling the clustered config (1500/2200 Hz, 8PSK)

Carriers at 1500/2200 Hz (700 Hz spacing, the best clustered spacing from
step 2), pushed to 8PSK with mid-frame pilots and increasing payload,
following the same incremental methodology as `hf5`/`hf6`/`hf7`.

| Step | Carriers (Hz) | Baud | Mod | Payload/carrier | Pilot | Frame (s) | Net bps | Result |
|---|---|---|---|---|---|---|---|---|
| E1 | 1500, 2200 | 600 | 8PSK | 9 B | off | 0.17 | — | 3/3, SNR 8.1–13.1 dB |
| E2 | 1500, 2200 | 600 | 8PSK | 288 B | 150 | 1.56 | ~2954 | 5/5, SNR 15.0–19.2 dB |
| E3 | 1500, 2200 | 600 | 8PSK | 588 B | 150 | 3.02 | ~3115 | 5/5, SNR 15.2–19.7 dB |
| **E4** | **1500, 2200** | **600** | **8PSK** | **738 B** | **150** | **3.76** | **≈3140** | **5/5**, SNR 15.5–20.3 dB |
| E5 | 1500, 2200 | 600 | 8PSK | 888 B | 150 | 4.48 | — | 4/5 (one crc_fail on the 2200 Hz carrier) — first crack, backed off to E4 per `bench.walk()`'s methodology |

E4 is the last config that cleared the 80% success bar before E5 failed it,
so it is the reported operating point, per the same "stop at the first
failure, report the last good" methodology `bench.walk()` and every prior
experiment in this series used.

## Interpretation

- **The core hypothesis — that clustering carriers into the empirically
  best-SNR region beats spreading them wide — is not supported by this
  channel today.** The wide-spread control (700/2100 Hz) and the clustered
  configs (1500/2200, 1500/2300 Hz) landed in the same SNR range (9–14 dB)
  once both cleared the minimum carrier-spacing requirement. The reason is
  that today's "good" region (1500–1900 Hz) is too narrow to fit two
  adequately-spaced carriers without pushing one of them to its edge
  (2200/2300 Hz), which gives back most of the placement advantage the
  hypothesis was banking on.
- **What did matter more than placement: carrier spacing relative to
  occupied bandwidth.** The single clearest, largest effect in this whole
  experiment was the 1500/1900 Hz (400 Hz spacing) catastrophic failure —
  an order of magnitude worse than any SNR-division or edge-of-band effect
  seen anywhere in `hf6`/`hf7`. That is a spacing bug waiting to happen for
  any future placement search, and worth flagging above the frequency
  question itself.
- **Net throughput (≈3140 bps, 2 carriers, 8PSK) roughly matches, but does
  not clearly beat, `hf6_multicarrier_v2`'s 3-carrier wide-spread result
  (≈3200 bps, QPSK)** — a materially different modulation/carrier-count
  trade landing at essentially the same point. Both remain well below
  `hf5_8psk_4k`'s single-carrier ≈4050 bps.
- **Honest reading of the placement axis specifically**: this channel's
  SNR-vs-frequency shape drifted since `hf5`'s original characterization
  and, at least under today's conditions, is not favorable to a
  clustering strategy — the good region is a narrow hump rather than a
  wide low-frequency-favoring shelf, so there isn't a wide "good" zone to
  cluster multiple adequately-spaced carriers into. A different day's
  channel conditions (e.g. one that reproduces `hf5`'s original wide
  low-frequency shelf) might make the same clustering idea pay off better;
  today's data does not show that.

## Recommended configuration (if this placement is used)

- **Carriers**: 2, at 1500 Hz / 2200 Hz (700 Hz spacing — the minimum that
  cleared the occupied-bandwidth interference cliff found in step 2)
- **Modulation**: 8PSK (3 bits/symbol) per carrier
- **Baud**: 600 symbols/second per carrier
- **Pilot interval**: 150 data symbols per carrier
- **Frame**: up to 3.76 s tested reliably (738 B payload/carrier), 5/5
  decoded; 888 B (4.48 s) was the first crack (4/5)
- **Net throughput**: **≈3140 bits/second**, measured
  (`logs/mode_sweeps/hf8_1500_2200_8psk_744/result.json`)
- **Comparison to baselines**:
  - ~22% below `hf5_8psk_4k`'s single-carrier 8PSK@1500baud result (≈4050 bps)
  - within ~2% of `hf6_multicarrier_v2`'s 3-carrier wide-spread result
    (≈3200 bps) — not a meaningful improvement
  - well above `hf7_ofdm_v3`'s true-OFDM result (≈2520 bps)

## Honest overall conclusion

The band-placement hypothesis — cluster carriers into the empirically
best-SNR region instead of spreading them wide, to spend less bandwidth
per carrier on skew/packing cost — **did not pay off on this channel
today**, for two related reasons found by real hardware evidence rather
than assumed: (1) the actual SNR-vs-frequency shape measured today is a
narrow hump around 1500–1900 Hz rather than the wide low-frequency-favoring
shelf `hf5_8psk_4k`'s older data suggested, so there isn't enough width in
the "good" region to fit multiple carriers at safe spacing without one
spilling to the region's edge; and (2) carrier spacing relative to occupied
bandwidth turned out to be a much larger, more binary effect (a >10 dB SNR
cliff at 400 Hz spacing vs 700+ Hz) than which specific frequencies were
chosen, once spacing was adequate. The final clustered config (1500/2200
Hz, 8PSK@600baud, ≈3140 bps) is a reasonable point on the same
carrier-count/modulation trade-off surface `hf6_multicarrier_v2` already
mapped, not a new best. `hf5_8psk_4k`'s single-carrier 8PSK@1500baud
config (≈4050 bps) remains the best known configuration for this path;
this experiment adds evidence that the channel's frequency-dependence is
time-varying enough that a placement strategy tuned to one day's shape may
not transfer to the next, which is itself useful information for any
future multicarrier attempt on this hardware.
