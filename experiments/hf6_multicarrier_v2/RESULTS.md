# hf6_multicarrier_v2 — small-N multicarrier real-hardware scaling results

**Bottom line: multicarrier did NOT beat the single-carrier baseline.** Best
config found: 3 carriers (800/1500/2200 Hz), QPSK, 600 baud each, mid-frame
pilots, ~6.7 s frames, **≈3200 bps net**, 5/5 decoded. That is below
`experiments/hf5_8psk_4k`'s single-carrier 8PSK@1500baud result of
**≈4050 bps**. All numbers below are real over-the-air IC-7300(TX) ->
IC-705(RX) audio-coupled trials (`bench.radio_pair`), never simulation for
the actual go/no-go calls; a loopback (no-channel) sanity check was run
once up front just to confirm the code path was correct before spending
airtime. IC-705 was never keyed (`--direction` hardcoded to `ab`, matching
`hf5_8psk_4k/hardware_test.py`).

## Design

`mc.py` reuses `hf5_8psk_4k/sc.py`'s RRC pulse shaping, bit<->symbol
mapping, framing (length+CRC32+PN whitening), and joint time/frequency
preamble acquisition + phase-ramp refinement + pilot-interpolated channel
tracking verbatim (imported, not copied). The only new code parameterizes
what was a module-level constant in `sc.py` (carrier frequency, preamble
PN seed, pilot PN seed) into a `CarrierSpec` so N independent instances can
run at N different frequencies. Each carrier carries **independent data**
(chosen over redundant/duplicate data since the goal was pushing net
throughput; redundancy-across-carriers is the natural fallback if
reliability rather than throughput becomes the priority). Carriers are
**non-orthogonal** — no exact subcarrier spacing, each one self-syncs via
its own decorrelated PN preamble and is demodulated by mixing to its own
frequency and RRC matched filtering the *full* composite capture (no
explicit bandpass/carrier-separation step; the matched filter's own
stopband does the separating). TX combines all carriers' passband
contributions *before* the same peak-normalize + 0.5 headroom scaling
`sc.py` uses for one carrier, so summing carriers does not by itself raise
the drive level into the ALC compared to a single carrier — the specific
precaution against reintroducing the IMD failure seen in
`experiments/hc2_32qam` and `experiments/hf4`.

## Real-hardware scaling history

All trials: IC-7300(TX) -> IC-705(RX) only. Baud values are constrained to
exact divisors of the 12 kHz design rate (500, 600, 750, 800, 1000, ...).

| Step | Carriers (Hz) | Baud | Mod | Payload/carrier | Pilot | Frame (s) | Net bps | Result |
|---|---|---|---|---|---|---|---|---|
| C1 | 800, 2000 | 500 | QPSK | 10 B | off | 0.25 | ~630 | 3/3, SNR 12-15 dB |
| D1 | 1000, 2000 | 800 | QPSK | 10 B | off | 0.16 | ~1000 | 3/3, SNR 12-14 dB |
| D2 | 1000, 2000 | 800 | 8PSK | 9 B | off | 0.13 | ~1660 | 3/3, SNR 12-14 dB |
| D3 | 1000, 2000 | 800 | 8PSK | 294 B | 150 | 1.19 | — | **0/5**, low carrier (1000 Hz) crc_fail every trial, SNR 12.2 dB there vs 17.9 dB on the 2000 Hz carrier |
| D3b | 1000 (solo) | 800 | 8PSK | 294 B | 150 | 1.19 | — | 0/3 — same failure with only ONE carrier present, proving this is an SNR/placement limit, not multicarrier IMD |
| D4 | 1100, 1900 | 800 | 8PSK | 294 B | 150 | 1.19 | — | 0/3, SNR dropped further (9.6-12.9 dB) — closer spacing made it worse |
| D5 | 1000, 2000 | 800 | QPSK | 294 B | 150 | 1.73 | ~2721 | 5/5, SNR 12.2-18.6 dB |
| D6 | 1000, 2000 | 800 | QPSK | 594 B | 150 | 3.38 | ~2813 | 5/5, SNR 12.1-18.8 dB |
| D7 | 900, 1500, 2100 | 600 | QPSK | 10 B | off | 0.21 | ~940 | 3/3, SNR 7.4-10.1 dB (3-way split visibly lowers SNR vs. 2 carriers) |
| D8 | 900, 1500, 2100 | 600 | QPSK | 294 B | 150 | 2.31 | — | **4/5** — one crc_fail on the 900 Hz carrier; the middle (1500 Hz) carrier ran consistently lowest at 7.6-7.8 dB, squeezed from both sides |
| D9 | 800, 1500, 2200 | 500 | QPSK | 294 B | 150 | 2.77 | ~2551 | 5/5, SNR 13.5-21.3 dB — wider spacing (700 Hz vs 600 Hz) fixed the marginal case |
| D10 | 800, 1500, 2200 | 600 | QPSK | 294 B | 150 | 2.31 | ~3062 | 5/5, SNR 11.1-17.5 dB |
| **D11** | **800, 1500, 2200** | **600** | **QPSK** | **894 B** | **150** | **6.71** | **≈3200** | **5/5**, SNR 11.4-18.5 dB |
| D12 | 800, 1500, 2200 | 750 | QPSK | 294 B | 150 | 1.84 | — | **0/5, all three carriers** — SNR collapsed to 6.0-9.2 dB uniformly. Real-hardware failure: pushing baud past 600 at this spacing crosses the same kind of bandwidth/interference wall `hf5_8psk_4k` found for a single carrier, just triggered sooner because 3 carriers' combined occupied bandwidth eats into the 300-2700 Hz passband faster. Backed off to step D11's 600 baud, which stayed 5/5 |
| D13 | 800, 1500, 2200 | 600 | **8PSK** | 9 B | off | 0.17 | — | 1/3 fully decoded (SNR 8.5-12.5 dB) — confirms the SNR-vs-modulation tradeoff from `hf5_8psk_4k`: 8PSK needs ~15 dB+, this config only offers 8-18 dB depending on carrier, so it is unreliable exactly the way `hf5_8psk_4k` predicts |

## Interpretation

- **No IMD collapse was observed at any point**, even at 3 carriers. Every
  failure above traces to an ordinary SNR/bandwidth cause (bandwidth-wall
  crowding, carrier placement, or modulation order needing more margin
  than available), the same mechanisms `hf5_8psk_4k/RESULTS.md` already
  characterized for one carrier — not the -5..+4 dB IMD signature from
  `experiments/hc2_32qam`/`experiments/hf4`'s many-tone failures. Keeping
  the combined drive level pre-normalized to the same peak `sc.py` uses
  for one carrier appears to have been enough headroom to avoid
  retriggering the IC-7300 ALC/compressor's intermodulation at N=2 or 3.
- **Splitting the passband into N carriers divides available SNR margin
  roughly by N**, not for free. Two well-separated carriers (1000/2000 Hz,
  1000 Hz apart) kept 12-18 dB — almost as good as `sc.py`'s single 1500 Hz
  carrier at 15-16 dB. Three carriers packed into the same 300-2700 Hz
  passband (necessarily closer together, or pushed nearer the band edges)
  dropped to 7-18 dB depending on placement and spacing, i.e. each
  carrier gets less passband and more mutual crowding than a lone carrier
  would. This is a capacity-division effect, not a hardware defect.
- **Carrier placement/spacing matters as much as carrier count.** The same
  3-carrier, same-baud config went from marginal (4/5, one carrier stuck
  at ~7.6 dB) to solid (5/5, 11-21 dB) just by widening spacing from 600 Hz
  to 700 Hz between adjacent carriers (steps D8 vs D9/D10) — worth more
  than any other single knob tried in this experiment.
- **The bandwidth wall from `hf5_8psk_4k` reappears at a lower baud once N
  carriers share the passband.** A single carrier's wall was ~2000-2200 Hz
  occupied bandwidth; with 3 carriers sharing 300-2700 Hz, the wall showed
  up already at 750 baud (step D12, total occupied bandwidth ≈
  3×750×1.35 ≈ 3038 Hz across three RRC skirts packed into a 2400 Hz
  passband) — the same underlying constraint, just reached sooner because
  N carriers' combined occupied bandwidth counts against the same fixed
  300-2700 Hz budget.
- **Higher-order modulation (8PSK) does not help here.** Every multicarrier
  configuration tested offers less SNR per carrier than `hf5_8psk_4k`'s
  single carrier at its sweet spot (15-16 dB), so 8PSK — which needed
  that much margin even in the single-carrier case — is unreliable on
  every multicarrier config tried (steps D2-D4 marginal/failing at 294 B
  payload, D13 1/3 at short payload). QPSK is the ceiling for the SNR this
  design leaves on the table per carrier.
- **Net result: 3× the carriers bought back roughly 3062-3200 bps against
  a single carrier's 4050 bps** — an improvement over the naive 2-carrier
  attempts (~2700-2800 bps) but still short of the single-carrier
  baseline, because the per-carrier SNR loss from splitting the passband
  costs more throughput than the extra parallel carriers add back at the
  QPSK rate they can each sustain.

## Recommended configuration (if multicarrier is used)

- **Carriers**: 3, at 800 Hz / 1500 Hz / 2200 Hz (700 Hz spacing)
- **Modulation**: QPSK (2 bits/symbol) per carrier — 8PSK is unreliable at
  the SNR this split leaves available
- **Baud**: 600 symbols/second per carrier (750 baud fails outright — see
  step D12)
- **Pilot interval**: 150 data symbols per carrier (same mid-frame pilot
  design as `hf5_8psk_4k` round 3), needed once frames run past ~1 s
- **Frame**: up to ~6.7 s tested (894 B payload/carrier, 2682 B total), 5/5
  decoded, kept comfortably under the 10 s RX capture buffer the same way
  `hf5_8psk_4k` did
- **Net throughput**: **≈3200 bits/second**, measured
  (`logs/mode_sweeps/hf6_multicarrier_v2-20260901T152050Z/result.json`)
- **Comparison to baseline**: **~21% below** `hf5_8psk_4k`'s single-carrier
  8PSK@1500baud result of ≈4050 bps
- **What limited further scaling**: not IMD (never observed at N=2 or 3)
  but the ordinary SNR-division-by-N and bandwidth-wall effects above —
  going to 4+ carriers would divide the passband further, was judged
  very unlikely to help given the trend from 2->3 carriers already showed
  diminishing (in fact negative-vs-baseline) returns, and was not tried in
  the interest of stopping at a clear, well-evidenced negative result
  rather than continuing to search a large parameter space for a result
  the trend already argues against.

## Honest overall conclusion

For this specific real audio-coupled IC-7300->IC-705 SSB path, **a single
well-placed carrier beats splitting the same passband into multiple
carriers**, because the passband is narrow enough (300-2700 Hz, and
practically ~2000-2200 Hz before the bandwidth wall) that dividing it
among carriers costs more per-carrier SNR/bandwidth than the extra
parallelism buys back at the modulation order that SNR can sustain. This
is a different failure mode from the many-tone OFDM IMD collapse seen
earlier in `experiments/hc2_32qam`/`experiments/hf4` — small-N
non-orthogonal multicarrier does NOT reintroduce IMD — but it still does
not beat the single-carrier baseline on this channel. `hf5_8psk_4k`'s
8PSK@1500baud-with-pilots config (≈4050 bps) remains the best known
configuration for this path.
