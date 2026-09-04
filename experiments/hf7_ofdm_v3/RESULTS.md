# hf7_ofdm_v3 — true OFDM real-hardware scaling results

**Bottom line: true (IFFT + cyclic-prefix) OFDM did NOT beat either earlier
baseline.** Best reliable config found: 6 orthogonal subcarriers
(937.5–1875 Hz, 187.5 Hz spacing on a 64-point real IFFT at the 12 kHz
design rate), 8PSK per subcarrier, 25% cyclic prefix, mid-frame pilot OFDM
symbols every 20 data symbols, ~1.7 s frames, **≈2520 bps net**, 10/10
decoded across two independent trial batches. That is below
`experiments/hf6_multicarrier_v2`'s non-orthogonal small-N result of
**≈3200 bps**, and well below `experiments/hf5_8psk_4k`'s single-carrier
result of **≈4050 bps**. All numbers below are real over-the-air
IC-7300(TX) -> IC-705(RX) audio-coupled trials (`bench.radio_pair`), no
simulation used for any go/no-go call (simulation was used once, up front,
only to confirm the code path was self-consistent before spending
airtime — see "Design & sanity check" below). IC-705 was never keyed
(`--direction` hardcoded to `ab`, matching every prior experiment in this
series).

## Design

`ofdm.py` reuses `hf5_8psk_4k/sc.py`'s bit<->symbol mapping, framing
(length+CRC32+PN whitening), and general acquisition philosophy
(matched-filter correlation over a frequency-offset search bank, then a
phase-based CFO refinement, then pilot-anchored linear interpolation of
per-subcarrier channel gain across the frame) — but the modulation itself
is new and is *true* OFDM, unlike `hf6_multicarrier_v2/mc.py`'s N
independent non-orthogonal `sc.py` copies:

- **Real-valued IFFT.** A 64-point real IFFT at the 12 kHz design rate
  puts bin `k` directly at `k * 12000/64 = k * 187.5` Hz — the OFDM symbol
  is synthesized as a real passband signal straight out of a
  Hermitian-symmetric spectrum, with no separate carrier-mixing stage the
  way `sc.py`/`mc.py` needed for their sinusoidal carriers.
- **Small active-subcarrier count.** Only a handful of the 13 in-band bins
  (300–2700 Hz -> bins 2–14) are active at once, per the task's central
  PAPR precaution.
- **Newman-like deterministic per-bin phase schedule** (`phase_k =
  pi*k^2/N_active`), applied identically to preamble, pilot, and data
  symbols, folded into every OFDM symbol purely to hold down crest factor.
  Because it's applied identically everywhere, the receiver's
  channel-estimate-based per-subcarrier equalization removes it for free.
- **Same headroom convention as sc.py/mc.py**: peak-normalize the whole
  composite waveform, then apply the same 0.5 drive-scale backoff.
- **Cyclic prefix**: 16 samples on a 64-sample FFT (25%).
- **2-symbol repeated-content preamble** for coarse timing (frequency-shift
  search-bank correlation, generalized from sc.py's mixer-based search to
  a Hilbert-transform-based real-signal frequency shift since OFDM has no
  single mixing carrier) + fine CFO (phase rotation between the two
  identical repeats) + initial per-subcarrier LS channel estimate.
- **Mid-frame pilot OFDM symbols**: one whole known OFDM symbol dropped in
  every `pilot_interval` data OFDM symbols, giving additional
  (time, complex-gain) anchors per subcarrier, linearly interpolated the
  same way `sc.py`'s mid-frame pilot chips are — built in from the start
  per the task brief, not discovered as a failure mode after the fact.

## Design & sanity check (simulation, not a go/no-go call)

Before spending airtime, an AWGN + fixed-CFO channel simulator (matching
the ~-8 Hz offset already characterized on this leg by `sc.py`) confirmed
the encode/decode round-trip: BPSK, QPSK, 8PSK, and 16-QAM configurations
all decoded cleanly with correct CRC and correct CFO/channel estimates.
This was used only to catch code bugs before real trials, exactly as the
task specifies — every reliability/throughput conclusion below comes from
real hardware.

## Real-hardware scaling history

All trials: IC-7300(TX) -> IC-705(RX) only. `fft_size=64` (187.5 Hz bin
spacing), `cp_len=16` (25%) throughout.

| Step | Active bins (Hz) | Mod | Payload | Pilot int. | Frame (s) | Net bps | Result |
|---|---|---|---|---|---|---|---|
| 1 | 6 (937.5–1875) | BPSK | 4 B | off | 0.107 | — | 3/3, SNR 22.7–30.6 dB |
| 2 | 6 (937.5–1875) | QPSK | 14 B | off | 0.107 | — | 2/3, then 4/5 (SNR 22–26 dB) — very short frame, matches the known startup-transient effect (`bench.py`'s documented note: a frame's first ~100 ms competes with the PTT/audio-chain settling transient) |
| 3 | 6 (937.5–1875) | QPSK | 114 B | 20 | 0.573 | ~1592 | 5/5, SNR 29–34 dB — a longer frame past the transient window fixed step 2's marginal result |
| 4 | 6 (937.5–1875) | 8PSK | 164 B | 20 | 0.547 | ~2399 | 5/5, SNR 28–33 dB |
| 5 | 6 (937.5–1875) | 16-QAM | 194 B | 20 | 0.487 | — | **1/5**, SNR 27–33 dB — fails badly despite ample SNR; matches the *identical* real-hardware-only 16-QAM fragility already documented in `hf5_8psk_4k` (round 2: 3-4/5 even at 15-16 dB) and never reproduced in this project's own AWGN simulation. Treated as the same known constellation-margin/real-channel effect, not re-investigated further here. |
| 6 | 10 (562.5–2250) | 8PSK | 244 B | 20 | 0.487 | — | **0/5**, aggregate SNR 20.8–22.4 dB. Per-bin diagnostic (separate probe, BPSK, same 10 bins) showed 13.9–23.1 dB *spread across bins*, worst bin 13.9 dB — an ordinary SNR-division-by-N effect (splitting the same drive power over more subcarriers lowers each one's share), the same mechanism `hf6_multicarrier_v2` found for splitting the passband into more carriers, not IMD (no uniform -5..+5 dB floor) |
| 7 | 6 (937.5–1875) | 8PSK | 294 B | 20 | 0.953 | ~2469 | 5/5, SNR 29–34 dB |
| 8 | 6 (937.5–1875) | 8PSK | 444 B | 20 | 1.413 | ~2514 | 5/5, SNR 31–36 dB |
| 9 | 6 (937.5–1875) | 8PSK | 594 B | 20 | 1.887 | — | 4/5, SNR 27–41 dB — one crc_fail at the largest payload tried |
| 10 | 6 (937.5–1875) | 8PSK | 594 B | 8 (denser pilots) | 2.020 | — | 3/5 — denser pilots did **not** rescue it (in fact slightly worse), ruling out sparse-pilot phase drift as the cause |
| **11** | **6 (937.5–1875)** | **8PSK** | **544 B** | **20** | **1.733** | **~2513** | **5/5**, SNR 31–41 dB |
| 12 | 6 (937.5–1875) | 8PSK | 538 B | 20 | 1.713 | ~2513 | 5/5, SNR 31–40 dB — independent repeat with a different seed, confirming step 11 |
| 13 | 7 (937.5–2062.5) | 8PSK | 294 B | 20 | 0.820 | — | **0/5, all trials**, SNR 23.5–27.3 dB — adding just ONE more subcarrier at the top edge broke the link completely, despite adequate average SNR |
| 14 | 8 (750–2062.5) | 8PSK | 444 B | 20 | 1.067 | — | **0/5, all trials**, SNR 25.9–27.4 dB — same complete-failure signature as step 13 |

## Interpretation

- **No classic many-tone IMD signature was ever observed.** Every failure
  above shows either (a) a clear SNR-vs-bin-count division effect (step 6,
  per-bin SNR spread 14–23 dB, consistent with splitting the same drive
  power across more subcarriers — the exact mechanism already documented
  in `hf6_multicarrier_v2`), or (b) the marginal-edge/frame-length effect
  familiar from `hf5_8psk_4k` (steps 9–10), or (c) 16-QAM's
  already-known real-hardware fragility (step 5), or (d) the distinct
  failure described next. None of the failures show the flat, uniform
  -5..+4 dB SNR floor that characterized `hc2_32qam`/`hf4`'s many-tone IMD
  collapse — crest factor stayed low throughout (5.5–7.9 dB measured,
  worst case at 10 active bins) thanks to the small subcarrier count plus
  the Newman-like phase schedule, and that appears to have worked exactly
  as intended: **no IMD was triggered anywhere in this experiment.**
- **A new, OFDM-specific failure appeared going from 6 to 7 active
  subcarriers (steps 13–14), and it is NOT an SNR-division effect.**
  Adding just one more subcarrier (2062.5 Hz) to the reliable 6-bin set
  broke the link completely (0/5), even though the *average* SNR reported
  (23.5–27.3 dB) was comfortably above what 8PSK needs (`hf5_8psk_4k`
  found ~15-16 dB sufficient), and a separate per-bin probe of that same
  region (step 6's diagnostic) had shown the individual 2062.5 Hz bin
  itself sitting around 19.9 dB — not a bad bin in isolation. Because
  the failure is complete (0/5, not a gradual degradation) and appears the
  moment the occupied span crosses a threshold rather than scaling
  smoothly with bin count, the most likely explanation is **inter-carrier
  interference (ICI) from the channel's own dispersion / non-flat group
  delay across a wider band** — something a single-tap-per-symbol,
  per-subcarrier gain-only equalizer (this design's whole channel model)
  cannot correct, and something neither `sc.py`'s single carrier nor
  `mc.py`'s *non-orthogonal* multicarrier approach are exposed to, because
  neither of those designs depends on subcarrier orthogonality being
  preserved end-to-end the way true OFDM does. This is a genuinely
  different failure mode from anything seen in `hf5`/`hf6`/`path_probe`,
  and it is the reason this experiment stopped scaling subcarrier count.
- **Denser pilots did not fix the large-payload marginal case (steps
  9–10)**, which argues against slow phase/gain drift as the cause there
  and is consistent with it being an ordinary marginal-edge effect (like
  `hf5_8psk_4k`'s own 594 B step ceiling) rather than something pilot
  tracking is positioned to fix.
- **16-QAM's failure reproduces exactly** what `hf5_8psk_4k` already
  found: unreliable independent of SNR margin (27-33 dB here, still only
  1/5), which this project's own AWGN simulation cannot reproduce either
  — strong evidence this is a specific real-hardware nonlinearity/artifact
  that generically hurts tightly-packed constellations on this path,
  not something particular to OFDM.

## Recommended configuration

- **FFT size / CP**: 64-point real IFFT at 12 kHz design rate (187.5 Hz
  bin spacing), 16-sample (25%) cyclic prefix
- **Active subcarriers**: 6, at 937.5/1125/1312.5/1500/1687.5/1875 Hz
- **Modulation**: 8PSK (3 bits/symbol) per subcarrier
- **Pilot interval**: 20 data OFDM symbols between mid-frame pilot OFDM
  symbols
- **Frame**: up to ~1.7 s tested reliably (544 B payload), 10/10 decoded
  across two independent seeds; 594 B (1.9 s) was the first crack (4/5,
  then 3/5 with denser pilots — a marginal edge, not fixed by more pilots)
- **Net throughput**: **≈2510-2520 bits/second**, measured
  (`logs/mode_sweeps/hf7_ofdm_v3-20260901T153724Z/result.json` and
  `...-20260901T154022Z/result.json`)
- **Crest factor**: 6.4 dB measured for this 6-subcarrier 8PSK config
  (5.5–7.9 dB across all configs tried) — no IMD was ever triggered
- **Comparison to baselines**: **~38% below** `hf5_8psk_4k`'s single-carrier
  8PSK@1500baud result of ≈4050 bps, and **~21% below**
  `hf6_multicarrier_v2`'s non-orthogonal 3-carrier result of ≈3200 bps
- **What limited further scaling**: not IMD (never observed at any active
  bin count 6-10) and not raw SNR at the 6-bin operating point (31-41 dB,
  plenty of margin) — the ceiling was a **true-OFDM-specific failure**
  that appeared abruptly the moment the occupied span grew from 6 to 7
  active subcarriers (complete 0/5 failure despite 23-27 dB average SNR),
  most likely inter-carrier interference from the channel's frequency-
  dependent group delay/dispersion across that wider a span, which this
  design's per-subcarrier gain-only equalizer has no way to correct.
  Widening to more subcarriers with a richer equalizer (fractional-timing
  tracking, or a proper channel-dispersion/ICI-aware equalizer instead of
  independent per-bin gain interpolation) is the natural next lever, but
  was judged out of scope for this conservative first pass.

## Honest overall conclusion

True OFDM's central risk on this hardware — TX-chain IMD from summing many
simultaneous tones — was successfully avoided at every subcarrier count
tried (6 through 10), validating the small-N-plus-low-PAPR-phase-schedule
approach the task asked for. But true OFDM introduced a *different* real
ceiling that neither of the earlier, simpler designs hit: an abrupt
orthogonality/dispersion-sensitivity failure once the occupied bandwidth
grew past a fairly narrow single-carrier-equivalent span, which capped
useful throughput at ≈2520 bps — below both `hf6_multicarrier_v2`'s
non-orthogonal multicarrier result (≈3200 bps) and well below
`hf5_8psk_4k`'s single-carrier result (≈4050 bps), which remains the best
known configuration for this path. The honest reading is that this
specific IC-7300->IC-705 audio-coupled channel rewards *simplicity*
(one clean carrier) over *parallelism* (whether non-orthogonal
multicarrier or true orthogonal OFDM) at every level tried so far: each
step away from a single carrier bought back less throughput than it cost
in per-subcarrier SNR or in new failure modes the added structure itself
introduced.
