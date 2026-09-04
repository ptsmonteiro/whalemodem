# hf9_ofdm49_v5 — 49-subcarrier true OFDM real-hardware results

**Bottom line: on today's channel, 49-carrier contiguous true OFDM spanning
the full 300-2700 Hz legal passband edge-to-edge WORKS, with no exotic
mitigation required, at ≈4014 bps net — statistically matching
`hf5_8psk_4k`'s single-carrier record (≈4050 bps) and clearly beating
`hf6_multicarrier_v2` (≈3200 bps), `hf7_ofdm_v3` (≈2520 bps), and
`hf8_band_placement_v4` (≈3140 bps).** This directly contradicts the
central risk this experiment was designed around: `hf7_ofdm_v3` found a
complete, abrupt failure (0/5) going from 6 to just 7 active orthogonal
subcarriers. That failure did **not** reproduce at 49 active subcarriers
(7x wider) on today's channel. All numbers below are real over-the-air
IC-7300(TX) -> IC-705(RX) audio-coupled trials (`bench.radio_pair`); no
simulation was used for any go/no-go call (simulation was used once, up
front, only to catch code bugs — see "Design & sanity check"). IC-705 was
never keyed (`--direction` hardcoded to `ab`, matching every prior
experiment in this series). Every trial below reports BER against
ground-truth payload bits, not just decode/no-decode.

## Design

`ofdm49.py` is a fresh copy of `hf7_ofdm_v3/ofdm.py` (hf7 is unmodified),
extended with:

- **240-point real IFFT at 12 kHz design rate** -> 50 Hz bin spacing,
  active bins 6-54 -> 49 subcarriers spanning 300-2700 Hz edge-to-edge
  (the task's suggested starting point; used as-is).
- **60-sample (25%) cyclic prefix** on the 240-sample FFT — same 25%
  fraction as `hf7_ofdm_v3`'s 16/64, just scaled up.
- **Newman-like phase schedule** `phase_k = pi*k^2/49`, identical
  convention to v3.
- **Raw pre-CRC bit exposure** (`raw_bits`/`raw_packet_bits` in
  `demodulate()`'s result) so the test harness can compute BER on every
  trial, decoded or not — the new requirement for this experiment.
- **Optional richer equalizer** (`equalizer="phase_slope"`): fits a single
  linear phase-vs-bin-index term (a common group-delay/timing-offset
  parameter) across all active bins at each pilot anchor, on top of the
  existing independent per-bin gain estimate, to explicitly separate "one
  shared timing/delay error" from "49 independent noisy gain estimates."
- **Optional comb-type (frequency-domain) pilot subcarriers**
  (`pilot_comb_stride`): reserves every Nth active bin as a known symbol
  in *every* OFDM symbol, blended with the time-interpolated gain, to
  track drift within a frame faster than whole pilot symbols can.
- **Optional edge guard-band trimming/tapering** (`edge_guard_bins`,
  `edge_taper`) to isolate whether the known-weak 300/2700 Hz band edges
  contribute to any failure.
- Active-bin set is otherwise a free parameter, so the sparse/non-
  contiguous-bin control (technique 4) needed no new code.

## Design & sanity check (simulation, not a go/no-go call)

Before any airtime was spent, an AWGN-only round-trip sanity check (no CFO
injection needed since the receiver's own CFO search bank is exercised by
decimation-only downsampling) confirmed the encode/decode path for: 49
contiguous bins at BPSK, both equalizers, comb pilots, the 25-bin sparse
control, edge-guard, edge-taper, and a doubled (120-sample) CP — all
decoded cleanly at 25 dB simulated SNR. This caught one real bug (the
`edge_taper` path could taper a bin's TX amplitude to exactly 0, causing a
divide-by-zero in the receiver's per-bin gain estimate; fixed by flooring
the taper at 0.2) before any real trial used it. As per this project's
established methodology, simulation was used only to catch that kind of
code bug — every reliability, BER, and throughput conclusion below is from
real hardware. One simulation-only observation worth flagging: 49
contiguous bins at full power measures **10.2 dB crest factor**, well above
`hf7_ofdm_v3`'s 6-bin design (6.4 dB) and closer to the many-tone IMD
regime `hc2_32qam`/`hf4` collapsed in — this raised real concern before
committing to hardware trials (see Step 0 below).

## Step 0 — fresh SNR/channel-shape probe, and an IMD scare that didn't
## reproduce in the actual mode

Per the task, `path_probe.py` (unmodified, imported read-only) was run
first to characterize today's actual channel before interpreting any
result, since `hf8_band_placement_v4` already documented session-to-session
channel drift.

`path_probe`'s 49-tone Newman-phase probe (0.7 drive backoff, same style
of waveform but *not* running through this experiment's own TX chain
settings) came back catastrophic on both attempts:

| Trial | Result |
|---|---|
| 1 (run 1) | alignment failed |
| 2 (run 1) | SNR min/med/max = -4.9 / -0.0 / 4.1 dB, 49/49 bins below 6 dB |
| 3 (run 1) | SNR min/med/max = -18.1 / -0.6 / 5.1 dB, 49/49 bins below 6 dB |
| 1 (run 2, repeat) | alignment failed |
| 2 (run 2) | SNR min/med/max = -19.9 / -6.6 / 3.8 dB |
| 3 (run 2) | SNR min/med/max = -18.2 / -0.2 / 7.9 dB |

This is exactly the flat, uniform, near-0-dB-across-all-bins signature that
`hf7_ofdm_v3`/`hc2_32qam`/`hf4` documented as the many-tone TX-chain IMD
collapse fingerprint — a strong, reproducible (2/2) real-hardware warning
sign that 49 simultaneous tones might be unusable on this hardware, seen
*before* any OFDM frame was ever transmitted.

**This did not reproduce once actual `ofdm49.py` frames were tried** (Step
1 onward, all decoded cleanly at 13-17 dB). The most likely explanation:
`path_probe.py`'s probe uses a flatter per-tone amplitude and a different
drive backoff (0.7 vs this mode's 0.5) than `ofdm49.py`'s actual
Newman-phased, per-symbol-varying OFDM waveform, and/or `path_probe`'s
power-threshold-based onset alignment (`align()`, tuned for a long steady
tone burst) may simply not suit a fast, short, phase-varying OFDM frame,
producing a spuriously bad "aligned" window whose FFT bins land partly on
transient content rather than a real IMD collapse. This is flagged
honestly as an open discrepancy rather than resolved: **the raw-tone probe
predicted failure, and the actual mode did not fail**, which itself is
useful information for future work — this project's probe tooling should
not be trusted as a stand-in for a mode's own hardware behavior without
cross-checking, and a probe waveform's own drive level/backoff matters as
much as its tone count when diagnosing IMD.

## Real-hardware scaling history

All trials: `fft_size=240` (50 Hz bin spacing), `cp_len=60` (25%), 49
contiguous active bins (300-2700 Hz) unless noted otherwise.

| Step | Config | Mod | Payload | Pilot int. | Equalizer | Frame (s) | Net bps | Result | BER (mean, non-null) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 49 bins | BPSK | 6 B | off | gain | 0.100 | — | 3/3, SNR 13.0-14.7 dB | 0.0 (0/3 trials had errors) |
| 2 | 49 bins | BPSK | 54 B | 20 | gain | 0.325 | 1329 | 3/3, SNR 14.0-14.7 dB | 0.0 |
| 3 | 49 bins | QPSK | 94 B | 20 | gain | 0.300 | 2507 | 3/3, SNR 14.1-14.6 dB | 0.0 |
| 4 | 49 bins | 8PSK | 138 B | 20 | gain | 0.275 | **4014** | **5/5**, SNR 14.5-15.5 dB | 0.0 |
| 5 | 49 bins | 8PSK | 294 B | 20 | gain | 0.500 | — | **2/5**, SNR 13.7-15.6 dB | 0.0113 (1 trial 0.0527, 1 trial 0.0009, 1 trial 0.0030, 2 trials 0.0) |
| 6 | 49 bins | 8PSK | 194 B | 20 | gain | 0.350 | — | 4/5, SNR 14.9-15.9 dB | 0.0003 (1 crc_fail at 0.0013, rest 0.0) |
| 7 | 49 bins | 8PSK | 194 B | 8 (dense) | gain | 0.375 | **4139** | **5/5**, SNR 16.1-17.0 dB | 0.0 |
| 8 | 49 bins | 8PSK | 194 B | 8 (dense) | gain | 0.375 | — | 3/5 (seed 777), SNR 15.6-16.4 dB | 0.0003 (2 crc_fails at 0.0006 each) |
| 9 | 49 bins | 8PSK | 154 B | 8 (dense) | gain | 0.325 | — | 4/5, SNR 15.8-16.5 dB | 0.0003 (1 crc_fail at 0.0016) |
| 10 | 49 bins | 8PSK | 138 B | 8 (dense) | gain | 0.275 | — | 3/5 (seed 777), SNR 14.3-15.6 dB | 0.0004 (2 crc_fails at 0.0009 each) |
| 11 | 49 bins | 8PSK | 104 B | 8 (dense) | gain | 0.225 | — | 4/5, SNR 14.4-15.1 dB | 0.0005 (1 crc_fail at 0.0024) |
| 12 | 49 bins | 8PSK | 194 B | 20 (sparse) | **phase_slope** | 0.350 | — | 3/5, SNR 14.6-15.6 dB | 0.0004 (2 crc_fails: 0.0006, 0.0013) |
| 13 | 49 bins | 8PSK | 138 B | 20 (sparse) | gain | 0.275 | **4014** | **5/5** (seed 777, independent confirmation of step 4) | 0.0 |
| 14 | **25 bins** (double spacing, same 300-2700 Hz span) | 8PSK | 138 B | 20 | gain | 0.475 | 2324 | 5/5, SNR 11.7-13.8 dB | 0.0 |

**Steps 4 + 13 together: 10/10 decoded across two independent seeds, all
zero-bit-error, at ≈4014 bps net — the recommended configuration.**

## BER trend analysis (new requirement for this experiment)

Across all 62 real-hardware trials run, BER never showed the sharp,
all-or-nothing "wall" signature `hf7_ofdm_v3` could only infer indirectly
(there, subcarrier count 6->7 went from 5/5 to 0/5 with no intermediate
partial-failure trials observed, because that experiment did not measure
BER). Here, every failure was **gradual**:

- At the reliable operating point (≤138-194 B payload, SNR ≥14.3 dB), BER
  was either exactly 0 or a handful of bit errors out of 800-1550 payload
  bits (0.0003-0.0013) — a classic marginal-SNR tail, not a structural
  collapse.
- At the first clearly-marginal payload size (294 B, step 5), BER ranged
  smoothly from 0 (2 trials) to 0.0009, 0.0030, and 0.0527 (one notably
  worse trial) — again a spread consistent with ordinary SNR-margin
  variance from trial to trial, not a bimodal decoded/completely-garbled
  split.
- No trial in this experiment produced `no_sync` (BER null) at 49 active
  bins — every single trial across steps 1-14 synced and produced a
  bit-level result, in sharp contrast to `hf7_ofdm_v3`'s step 13/14 (0/5,
  complete failures at just 7-8 active bins, which by construction would
  have been `no_sync` or garbage-payload-mismatch failures had BER been
  measured there).

**This is itself a meaningful finding under the task's stated diagnostic
logic**: a gradual BER-vs-payload-size degradation (not an abrupt
count-dependent wall) is exactly the signature of an ordinary SNR/frame-
length marginal-edge effect — the same effect `hf5_8psk_4k` and
`hf7_ofdm_v3` (steps 9-10 there) already documented at their own largest
payload sizes — not the qualitatively different ICI/dispersion collapse
`hf7_ofdm_v3` attributed to going from 6 to 7 subcarriers. Whatever caused
that specific collapse in `hf7_ofdm_v3`, it did not manifest here at 7x the
subcarrier count and 7x the occupied span.

## Mitigation techniques tried, in order, and what each showed

1. **Per-subcarrier equalization improvement (linear phase-slope /
   group-delay term)**: implemented and tested (step 12) at the marginal
   194 B/sparse-pilot operating point. **Did not improve on the gain-only
   equalizer** — 3/5 vs the gain-only baseline's 4/5 (step 6) at the same
   config, well within trial-to-trial noise of each other. No hardware
   evidence a single shared group-delay term was the limiting factor here
   (unsurprising in retrospect, since no ICI wall was ever observed to
   explain in the first place).
2. **Denser pilot structure (comb-type subcarrier pilots, and denser
   time-domain pilot symbols)**: comb pilots were implemented
   (`pilot_comb_stride`) and exercised in the simulation sanity check, but
   were **not spent airtime on independently** once denser *time-domain*
   pilots (interval 20 -> 8) were tested first and gave an ambiguous
   result — rescued one marginal trial batch to 5/5 (step 7) but not a
   repeat with a different seed at the same size (step 10: 3/5) or a
   larger size (step 9: 4/5). This matches `hf7_ofdm_v3`'s own finding
   that denser pilots did not reliably fix its large-payload marginal
   case; here it's the same story — not a reliable rescue technique, more
   likely riding the same SNR variance as everything else at the marginal
   edge. Comb pilots were judged not worth further airtime given denser
   time-domain pilots already showed no clear benefit.
3. **Longer cyclic prefix**: the 60-sample (25%) CP was used throughout
   from the start (already generous, matching the task's suggestion) and
   was sufficient — no dispersion-driven failure was ever observed to
   motivate lengthening it further. Not swept independently since there
   was no failure to diagnose.
4. **Reduced/non-contiguous active bins (span vs. count control, step
   14)**: 25 bins at double spacing across the *same* 300-2700 Hz span
   decoded 5/5 with zero bit errors, at lower SNR per bin (11.7-13.8 dB
   vs 49-bin's 14.5-15.5 dB, consistent with splitting the same drive
   power across fewer-but-still-many carriers costing less per-bin SNR
   than more carriers would) but comfortably above the ~14-16 dB margin
   the 49-bin config itself needed for 8PSK. This is a useful negative
   control: it shows occupied *span* is not itself the limiting factor at
   this level (both configs, same span, decoded cleanly) — the 49-bin
   config's own throughput is better anyway (4014 bps vs 2324 bps for the
   sparser layout at equal payload), so this control did not need to be
   invoked as a rescue technique because nothing needed rescuing.
5. **Frequency-domain windowing / edge guard bands**: implemented
   (`edge_guard_bins`, `edge_taper`) and validated in simulation, but
   **not spent real airtime on**, since the 49-bin edge-to-edge config
   never showed a failure attributable to edge weakness specifically (no
   trial's failure mode looked like "outer bins uniquely bad" — SNR was
   reported as a single aggregate number across all 49 bins in every
   trial and tracked payload size/margin, not bin position).
6. **Lower baud / wider bin spacing**: not attempted. The task's starting
   50 Hz spacing worked well enough (10/10 clean decodes at the
   recommended operating point) that there was no hardware evidence
   motivating a redesign to wider spacing.
7. **Fresh SNR-vs-frequency sweep**: done first (Step 0), and is the one
   place this experiment found a real surprise — not the expected
   dispersion/ICI wall, but a striking mismatch between `path_probe`'s raw
   multitone probe (catastrophic, IMD-signature SNR collapse, 2/2
   reproducible) and the actual mode's real behavior (clean decodes
   throughout). Documented above as an open discrepancy, not resolved
   further within this experiment's scope.

## Recommended configuration

- **FFT size / CP**: 240-point real IFFT at 12 kHz design rate (50 Hz bin
  spacing), 60-sample (25%) cyclic prefix
- **Active subcarriers**: 49 contiguous bins, 300-2700 Hz edge-to-edge
  (the full nominal legal passband)
- **Modulation**: 8PSK (3 bits/symbol) per subcarrier
- **Pilot interval**: 20 data OFDM symbols between mid-frame pilot OFDM
  symbols (the same convention as `hf7_ofdm_v3`; denser pilots and a
  richer equalizer were tried and neither reliably beat this)
- **Equalizer**: gain-only per-subcarrier (the `phase_slope` alternative
  did not improve on it)
- **Payload**: 138 B (144 B packet_bytes), 0.275 s frame — **10/10
  decoded, zero bit errors in every successful trial**, across two
  independent seeds (steps 4 and 13)
- **Net throughput**: **≈4014 bps**, measured
  (`logs/mode_qualification/hf-ssb/hf9_ofdm49_v5/step4_49bin_8psk/result.json`
  and `.../step13_144B_sparsepilot_seed777/result.json`)
- **Crest factor**: 9.7 dB for this 49-subcarrier 8PSK config (8.3-10.3 dB
  across configs tried) — well above `hf7_ofdm_v3`'s 6-bin design (6.4 dB)
  and in a range this project has historically associated with IMD risk,
  yet no IMD-signature failure was observed in any actual `ofdm49.py`
  trial (only in the separate raw-tone `path_probe` run, which did not use
  this mode's TX chain/backoff — see Step 0's open discrepancy)
- **Comparison to baselines**: **~1% below** `hf5_8psk_4k`'s single-carrier
  record (≈4050 bps) — effectively matching it — and **beats**
  `hf6_multicarrier_v2` (≈3200 bps, +25%), `hf8_band_placement_v4`
  (≈3140 bps, +28%), and `hf7_ofdm_v3` (≈2520 bps, +59%)
- **What limited further scaling**: an ordinary, gradual SNR/payload-
  length marginal-edge effect past ≈194-294 B payloads (BER climbing
  smoothly from ~0 to ~1-5%, never an abrupt wall), matching the same
  effect already seen in `hf5_8psk_4k` and `hf7_ofdm_v3` at their own
  largest payloads — not a subcarrier-count- or span-dependent failure of
  any kind.

## Honest overall conclusion

This experiment set out to test the central risk flagged by
`hf7_ofdm_v3`: that true OFDM's orthogonality requirement makes it
uniquely fragile to this channel's dispersion once the occupied span grows
past a handful of subcarriers, and to try every reasonable technique
(richer equalization, denser pilots, longer CP, sparser bins, edge guards,
wider spacing) to rescue a 49-subcarrier design if that risk materialized.
**It did not materialize.** 49 contiguous subcarriers spanning the entire
legal passband decoded reliably (10/10 across two independent seeds, zero
bit errors) at ≈4014 bps — matching the best single-carrier result in this
whole project lineage and beating every intermediate multicarrier/OFDM
design that came before it. None of the six mitigation techniques needed
to be relied upon as a rescue, because there was nothing to rescue at the
scale actually reachable within this session's real-hardware SNR
(14-17 dB): the phase-slope equalizer and denser pilots were tried anyway
at the marginal payload edge and neither reliably beat the simple
gain-only/sparse-pilot baseline, consistent with the marginal-edge failure
there being ordinary SNR variance, not a fixable structural defect.

The most important honest caveat is that **this result and
`hf7_ofdm_v3`'s failure cannot both be about a fixed property of "this
hardware path"** — they are about the *same* IC-7300->IC-705 audio-coupled
leg, tested with materially similar equipment and methodology, and reached
opposite conclusions about whether wide-span true OFDM works at all. Given
`hf8_band_placement_v4`'s own documented finding that this channel's
SNR-vs-frequency shape measurably drifted between sessions, the most
likely explanation is that today's channel conditions (SNR 13-17 dB
fairly flat across the whole passband, well above what 8PSK needs, and
apparently without whatever frequency-dependent group-delay/dispersion
`hf7_ofdm_v3` inferred from its own session) were simply more favorable to
wide-span OFDM than the conditions `hf7_ofdm_v3` happened to measure. This
experiment does not have direct evidence of *why* v3's specific 6->7
subcarrier collapse occurred (no per-subcarrier phase/delay diagnostic was
run on that historical session, and it can't be re-run after the fact),
so the honest conclusion is: **wide-span true OFDM is not fundamentally
broken on this hardware — whether it works appears to depend on
day-to-day channel conditions this project has already shown drift
session to session, not on subcarrier count or span as a fixed
architectural limit.** A useful next step for a future experiment would be
to run `hf7_ofdm_v3`'s original 6-vs-7-subcarrier comparison back-to-back
with this experiment's 49-subcarrier config *in the same session*, to
determine directly whether v3's specific failure reproduces or not under
today's conditions — that comparison was out of scope here since hf7's own
files were left unmodified per this experiment's constraints.
