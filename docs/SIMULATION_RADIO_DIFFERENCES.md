# Simulation-to-radio differences and fixes

This document collects project evidence where a waveform or test looked healthy
in a software channel but behaved differently through real radios. It is meant
as an input to future channel models and mode-design gates, not as a current
qualification table. Current dispositions remain in
[`MODE_QUALIFICATION.md`](../MODE_QUALIFICATION.md).

The strongest general lesson is that clean round trips and AWGN tests are code
checks, not radio predictions. The failures below were usually caused by
stateful, directional behavior: squelch and startup transients, non-flat audio
filters, ALC/limiter effects, phase drift, burst errors, and capture boundaries.
Those effects are poorly represented by a memoryless noise source.

Scope matters. VF6 and several HC2-family candidates have simulation evidence
but no corresponding radio failure because they have not had a comparable
hardware campaign; they are not treated as mismatches here. HF2 is also not a
delivery mismatch: it passed both the simulated frame gate and 43/43 retained
radio frames, although its separate occupied-bandwidth gate still fails. The
historical experimental dense-OFDM design called `hf4` is distinct from the
current production mode named `hf4`, which is the later HF13 fast-sync
single-carrier waveform.

Evidence labels used below:

- **Observed** means a retained real-radio result or saved-capture replay.
- **Diagnosis** means the retained evidence isolates the likely mechanism.
- **Inference** means the explanation fits the evidence but was not isolated by
  a controlled experiment.
- **Resolved** means a later radio run demonstrated the remedy. “Unresolved” is
  kept explicit when software-only improvement did not transfer to the radios.

## Part 1: HF SSB

### Summary

| Mode or experiment | Software expectation | Real-radio difference | What made it work, or current status |
| --- | --- | --- | --- |
| HF3 | Passed both declared simulated boundaries: 300/300 benign/static and 291/300 quiet Watterson | The shipped adapter had been pointing at an HF4-derived prototype; after correction to the documented 36-carrier HF3 waveform, the 2026-09-07 smoke run was 3/3 in each IC-705/IC-7300 direction. The earlier clean 36/40 campaign still leaves the promotion-sized hardware FER gate open. | **Partly resolved.** The default-mode wiring defect is fixed; retain the 40-frame rerun before claiming hardware qualification |
| HF4 experimental dense OFDM | Post-fix benign/static simulation delivered 66%, 67%, and 90% at 13, 15, and 20 dB | Original hardware run was 0/5; after an interleaver fix it was still 0/30, despite 19/30 acquiring | Longer capture tail fixed truncation; a real no-op interleaver bug was fixed, but payload decoding remains **unresolved** |
| Single-carrier HF5 | Software round trips and later synthetic drift tests verified the receiver mechanisms, but did not predict the radio's operating limits | Radios exposed a hard bandwidth wall, about -8 Hz directional CFO, long-frame phase drift, and persistent 16-QAM fragility | Use 1500-baud 8PSK inside the passband, search/refine CFO, and insert mid-frame pilots; formerly failing 1.1-2.2 s frames became 5/5 and operation extended to 5.9 s |
| HF7 small-N OFDM | AWGN sanity decoded all candidate constellations | 16-QAM was 1/5 at 27-33 dB; six carriers worked but adding a seventh caused 0/5 | Back off to six carriers and 8PSK. The abrupt 6-to-7 failure was not reproduced by later HF9 and remains session/path dependent |
| HF10 49-carrier OFDM | Plain 16-QAM and coded 16-QAM both worked in AWGN; the simulator showed genuine coding gain | Plain 16-QAM was 0/3 at 0.4-6.3% raw BER | Rate-3/4 LDPC, soft LLRs, correct codeword packing, and retaining the mid-frame pilot produced 12/13 at 4,332 bit/s |
| QPSK29 / early wide HF OFDM | Code path worked, and training/header acquisition was strong | Upper carriers were nearly erased by the IC-705 filter; random pilot placement left one carrier untracked for three seconds | Lower the LLR floor so absent carriers become erasures and balance pilots over time/frequency; coded QPSK29 reached 4/4, though outside the production bandwidth/keying contract |
| HC1 reverse leg | Simulation showed HC1 working in its envelope | 0/3 on IC-705 to IC-7300, all header-not-found | **Not a simulation gap:** the IC-705 was reportedly on a dummy load. Treat as invalid setup evidence; record RF path and radio state |

### Detailed evidence

#### HF3: simulated gate passed, radio confidence interval failed

HF3 passed its simulated Level-3 frame gates at the declared boundaries:
300/300 benign/static frames and 291/300 quiet-Watterson frames. A three-frame
radio smoke also passed. The predeclared clean 40-frame IC-7300-to-IC-705 run,
however, delivered only 36/40. Every frame acquired with high confidence, while
four failed CRC and reproduced on offline capture replay. The resulting 23.1%
FER upper bound exceeded the 10% hardware gate.

This is direct evidence that the simulated filters, frequency error, fading,
and noise did not cover all payload corruption on the bench. No corrective PHY
change is documented, so this remains an open simulation-to-radio gap rather
than a solved mode. See [`experiments/hf3/RESULTS.md`](../experiments/hf3/RESULTS.md)
and the [clean hardware campaign](../logs/mode_qualification/hf-ssb/hf3/2026-09-02-hardware/INDEX.md).

#### HF4: two real bugs, then a persistent unsimulated payload failure

HF4 is the clearest negative case. Simulation improved substantially after its
guard and coding work, but real hardware decoded no frame:

1. The first harness reused a short-frame capture tail for an 8.303 s frame.
   Frames arriving late were truncated before payload decode. Raising the tail
   to 9 s allowed complete attempts. This was a harness defect, not proof of a
   waveform failure.
2. Two synced captures repeatedly decoded the same impossible length, 35,242.
   Offline analysis found that the intended block interleaver and its inverse
   cancelled: the inner coded stream had never actually been interleaved. A
   multiplicative permutation and regression tests fixed that bug.
3. The remedy removed the fixed wrong-length signature and improved software
   results, but the radio recheck was still 0/30. Nineteen frames acquired
   across the smoke and characterization runs, and every acquired frame still
   failed payload/CRC recovery. The second defect or missing channel effect is
   unresolved.

Future simulators must include time-localized within-frame corruption. HF4's
synthetic fading varied mainly by carrier and frame, so it could not expose one
bad OFDM symbol corrupting the un-interleaved length bits. They must also pass
the full channel tail through the model and size capture windows from the mode's
actual duration.

Sources: [`experiments/hf4/RESULTS.md`](../experiments/hf4/RESULTS.md),
[original hardware run](../logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/INDEX.md),
and [post-fix recheck](../logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware-recheck/INDEX.md).

#### HF5: the radio revealed four effects absent from the AWGN sanity test

HF5 used incremental radio trials to locate its limits. Software round trips
verified the receiver code, and a synthetic fixed-offset-plus-drift test later
reproduced the long-frame failure and pilot remedy, but the operating limits
below came from radio evidence:

- A hard occupied-bandwidth wall. At 2,400 baud, the RRC signal extended
  beyond the roughly 300-2,700 Hz SSB audio path and collapsed to about 2 dB
  measured SNR, producing 0/5. Narrowing roll-off did not rescue it.
- A stable direction-dependent carrier offset, roughly -8 Hz from IC-7300 to
  IC-705 and +8 Hz in the reverse direction. The receiver's coarse search and
  preamble phase-ramp refinement handled it.
- Slow phase drift over long frames. Unpiloted 8PSK at 1,500 baud was 0/3 at
  2.18 s and 3/5 at 1.11 s despite unchanged 15-16 dB preamble SNR. A 15-chip
  pilot every 150 data symbols plus interpolated complex gain turned the same
  payload sizes into 5/5 and stayed 5/5 through a 5.92 s frame.
- Plain 16-QAM remained unreliable at apparently adequate SNR and was not
  repaired by phase pilots. This later proved to require coding rather than
  more tracking.

The working real-radio design was therefore 8PSK at 1,500 baud, centered at
1,500 Hz, with fixed-CFO handling and mid-frame pilots. See
[`experiments/hf5_8psk_4k/RESULTS.md`](../experiments/hf5_8psk_4k/RESULTS.md).

#### HF multicarrier and OFDM: passband sharing and session dependence

The HF6-HF9 series shows why a software code-path pass is not a carrier-plan
qualification:

- HF6 avoided the known many-tone IMD collapse by peak-normalizing the combined
  signal and using only two or three carriers. The radios still divided usable
  SNR among carriers, penalized tight placement, and hit the fixed SSB bandwidth
  wall. Three QPSK carriers at 600 baud worked; 750 baud was 0/5.
- HF7 passed AWGN sanity but plain 16-QAM was 1/5 on air even at high reported
  SNR. Six-carrier 8PSK worked, while adding only one upper carrier changed the
  result to 0/5. The report inferred ICI or group-delay dispersion.
- HF8 measured a different SNR-versus-frequency shape from the prior session.
  A 400 Hz carrier spacing caused a roughly 12 dB collapse, while 700 Hz
  spacing worked. Placement tuned to one day's path did not transfer cleanly.
- HF9 later ran 49 contiguous carriers over 300-2,700 Hz successfully at
  10/10. A separate raw multitone probe had predicted catastrophic IMD on that
  same day. Thus neither the HF7 “seven-carrier wall” nor the probe's result is
  a stable property of tone count alone. Waveform drive/backoff, alignment, and
  session state matter.

For simulation, randomize measured session profiles instead of treating one
frequency response as the hardware. Preserve waveform PAPR and drive through a
stateful nonlinearity, and compare candidate modes back-to-back through the same
channel realization. Sources:
[`HF6`](../experiments/hf6_multicarrier_v2/RESULTS.md),
[`HF7`](../experiments/hf7_ofdm_v3/RESULTS.md),
[`HF8`](../experiments/hf8_band_placement_v4/RESULTS.md), and
[`HF9`](../experiments/hf9_ofdm49_v5/RESULTS.md).

#### Plain 16-QAM: repeatedly good in AWGN, repeatedly fragile on radios

This is the most repeated HF modeling miss. Plain 16-QAM worked in the
project's AWGN checks, but failed on three different radio PHY arrangements:
single carrier, small-N OFDM, and 49-carrier OFDM. The failures persisted at
reported SNRs where 8PSK was nearly error-free, suggesting a real-chain
constellation impairment rather than ordinary additive noise. The reports
attribute it to ALC/compression or related time-localized amplitude/phase
distortion; that attribution is plausible but not fully isolated.

The demonstrated remedy was soft-decision rate-3/4 LDPC:

- HF10 changed 49-carrier plain 16-QAM from 0/3 into 12/13 coded frames, with
  0.51-4.22% raw BER and zero residual errors whenever all codewords converged.
- Removing the mid-frame pilot fell to 1/3, so FEC did not eliminate the need
  for channel tracking.
- HF12 transferred the same idea to one carrier and achieved 5/5 at both 966 B
  and 3,494 B, correcting up to 0.69% raw BER to zero. It matched rather than
  beat uncoded 8PSK because rate-3/4 exactly cancels 16-QAM's extra bit per
  symbol in the asymptotic rate arithmetic.

Future SSB simulation should not qualify high-order QAM with AWGN alone. It
needs a calibrated, drive-dependent compressor/ALC/limiter model with memory,
plus captured burst statistics. See
[`experiments/hf10_ofdm49_v6/RESULTS.md`](../experiments/hf10_ofdm49_v6/RESULTS.md)
and [`experiments/hf12_sc_fec_v7/RESULTS.md`](../experiments/hf12_sc_fec_v7/RESULTS.md).

#### QPSK29: edge erasures and pilot coverage

The first real QPSK29 capture acquired and corrected CFO, but only 14/17 LDPC
codewords converged. The IC-705 sharply removed the upper edge: measured header
SNR fell from 10.7 dB at 2,906 Hz to -14.9 dB at 3,094 Hz, and the last two
carriers contained 290 of 353 raw errors. A minimum LLR weight of 0.25 caused
nearly absent carriers to assert random bits too confidently. Reducing the
floor to 0.01 treated them as erasures. Replacing random pilot placement with
a balanced time/frequency lattice also prevented three-second untracked gaps.
The corrected LDPC-3/4 mode then passed 4/4.

This result argues for simulation assertions on worst-carrier response and
pilot age, not only aggregate SNR. It is a design lesson rather than a current
production candidate because its occupied band and keying duration violate the
present contracts. See [`experiments/qpsk29/RESULTS.md`](../experiments/qpsk29/RESULTS.md).

#### Setup and measurement differences that must not become channel models

- HC1's 0/3 reverse direction was later attributed to transmitting into a
  dummy load. HC0's extra robustness survived the same loss. This was an
  invalid bench state, not proof that the HC1 simulation missed an impairment.
- HF14's first “capture” was an effectively muted IC-705 input at RMS
  `2.57e-09`; its correlation values were noise artifacts. Once the input was
  restored, five BPSK geometries passed all 51 benign-path frames.
- Reducing received amplitude by 9-10 dB did not normally reduce decoder SNR,
  consistent with AGC scaling signal and noise together. Audio RMS is therefore
  not a substitute for calibrated RF SNR.
- Several HF reports show 5-6 dB session-to-session SNR changes and a drifting
  band shape. Sequential, unmatched mode comparisons can mistake session drift
  for a design effect.

Sources: [HC0/HC1 hardware record](../logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-28-hardware/INDEX.md)
and [`experiments/hf14_ofdm_bpsk_watterson/RESULTS.md`](../experiments/hf14_ofdm_bpsk_watterson/RESULTS.md).

### HF SSB simulation improvements suggested by the evidence

1. Add directional, measured TX and RX audio responses, including phase/group
   delay rather than magnitude only. Keep RF/IF filter state across a keying.
2. Add drive- and PAPR-dependent ALC/compressor/limiter behavior with attack,
   release, and memory. Calibrate it from single-tone and multitone captures.
3. Always include measured fixed CFO (about +/-8 Hz on the retained pair),
   residual drift, oscillator wander, and independent sample clocks.
4. Add within-frame burst events and slowly varying common phase/amplitude.
   Per-frame or per-carrier stationary fading alone missed important failures.
5. Run acquisition over realistic leading silence, PTT/radio startup, audio
   buffering, and a drained channel tail. Size the receive buffer from waveform
   duration, not a shared short-frame constant.
6. Sweep full-capacity and multiple-duration frames. Short training probes and
   tiny payloads systematically overstated usable bands and reliability.
7. Preserve exact waveform peak statistics and drive settings. A generic
   multitone probe is not a substitute for the candidate waveform.
8. Randomize or replay multiple measured session profiles, and compare modes on
   paired channel realizations. Do not fit a universal radio response to one
   day's band shape.
9. For QAM and OFDM, retain per-symbol and per-carrier truth: BER, erasures,
   pilot age, EVM, clipping, and codeword convergence. Frame CRC alone hides the
   mechanism.
10. Continue separate Watterson and bench-radio claims. A benign cabled-radio
    pass does not validate fading performance, and Watterson success does not
    validate the radio front end.

## Part 2: VHF FM

### Summary

| Mode or experiment | Software expectation | Real-radio difference | What made it work, or current status |
| --- | --- | --- | --- |
| Production AFSK/CPFSK at high baud | Loopback framing worked with a fixed 63-bit sync and short pads | A roughly fixed-duration squelch/AGC startup artifact consumed proportionally more of 900/1,200-baud frames; sync could lock while length/payload failed | Add duration-sized head symbols, then scale sync itself to about 210 ms; move clipping protection into transmitted frame symbols |
| VF2 | 10/10 software tests, including AWGN, dispersive taps, and 75 ppm clock error | First radio captures had accumulated phase, persistent bad carriers, and a false lock on periodic idle audio | Vary training, add interleaved convolutional FEC, per-carrier phase loops, and rank acquisition candidates by known-header fit; final 6/6 |
| VF3 | 8/8 software tests | Coherent payload was 4/6; two captures developed permanent 90-degree cycle slips partway through the frame | Change payload to differential QPSK; final 6/6 |
| VF4 | 10/10 software tests | Initial star-8-QAM radio probes were 1/2 twice, with 6.5-7.3% raw BER on the weak direction | Separate radial/angular metrics, fit ring levels, model differential phase noise, then concatenate shortened RS blocks outside Viterbi; final 6/6 |
| VF5 | 12/12 software tests | Pilotless 16-QAM was 0/2; radio phase moved tens to over 100 degrees, then residual errors arrived in RS-overloading bursts | Add full-band pilots, use conventional Gray I/Q labels, and round-robin-interleave RS bytes before convolutional coding; final 6/6 |
| CRC-only OFDM | Channel probe proposed 300-3,000 Hz with strong training SNR | Every wideband payload failed; edge carriers held most errors, and more pilots did not make QPSK/16-QAM reliable | Trim to 650-1,950 Hz and BPSK for 28/28, or add LDPC plus per-carrier LLR weighting for 3,301 bit/s at 500-2,400 Hz |
| MFSK candidates | AWGN screen passed 19/24 candidates; some appeared to have 4 dB margin | Only one candidate worked on both radio directions; weak-leg failures were adjacent low-tone confusion | Use wider relative tone separation: 4-FSK, 650 baud, spacing 0.833 symbol rates; keep software screen for ordering only |
| VF5 with Baofeng UV-B5 | VF5 was already 6/6 with the original HT | Drop-in radio result fell to 2/6; UV-B5 squelch removed the first 50-110 ms and acquisition chose false starts | Reduce clipping, but crucially open squelch; unchanged receiver then returned to 6/6 |

### Detailed evidence

#### Startup protection: fixed bit counts shrank at high baud

Early radio sweeps found frames at 900 baud and above acquiring strongly but
then failing CRC or parsing. The artifact was approximately fixed in time,
while the 63-bit sync shrank from 210 ms at 300 baud to 52 ms at 1,200 baud.
The slower modes happened to absorb the damaged opening; faster modes exposed
length and payload bits before the radio had settled.

The project first added a duration-sized head pad (commit `7a88478`), increased
it for slower squelches (commit `794ad3a`), then made sync duration itself
approximately constant across baud rates (commit `4a81f6d`). Timing protection
was later moved into explicit transmitted head/tail symbols (commit `5082048`)
so radio clipping is observable and can eventually be calibrated. The current
contract is in [`FRAMING.md`](../FRAMING.md) and
[`ADAPTIVE_TIMING.md`](../ADAPTIVE_TIMING.md).

Simulation implication: model startup loss in seconds, not symbols or bits,
and vary it by receive radio and squelch state. A mode must survive the full
capture/acquisition path, not only a perfectly aligned waveform buffer.

#### VF2: real captures forced synchronization, tracking, and coding changes

VF2's software suite covered full-capacity clean frames, AWGN plus a delayed
three-tap channel, 75 ppm clock mismatch, CRC rejection, and false-sync tests.
The first radio probe nevertheless exposed accumulated carrier phase and
persistent bad-carrier errors. Saved captures led to three changes: distinct
coherent training symbols, interleaved rate-1/2 convolutional coding, and a
per-carrier decision-directed phase loop.

A later reverse-direction probe locked to periodic idle receiver audio at
sample 5,813 instead of the RF frame at 34,111. Ranking repeat-correlation
proposals by fit to the varying known header corrected acquisition. The final
mode passed 6/6 full-size bidirectional radio frames. See
[`experiments/vf2/RESULTS.md`](../experiments/vf2/RESULTS.md).

#### VF3: coherent carrier loops suffered permanent cycle slips

VF3 passed software tests that included dispersive AWGN and clock mismatch.
The coherent-QPSK radio confirmation delivered 4/6. Failed captures showed
permanent 90-degree cycle slips beginning partway through the payload, losing
six and fourteen carriers. Soft Viterbi recovered the smaller event only.

Changing the payload to differential QPSK removed dependence on an absolute
quadrant after each slip. The revised waveform passed 6/6, even with 2.1-2.8%
pre-FEC BER on the harder direction. See
[`experiments/vf3/RESULTS.md`](../experiments/vf3/RESULTS.md).

#### VF4: high raw BER became a burst-code problem

The star-8-QAM software suite passed, but the first two weak-direction radio
probes failed at 7.27% and 6.54% raw BER. Separating radial from angular
metrics, fitting received ring levels, and accounting for the noise of two
differential phase samples rescued one saved capture but did not make a fresh
transmission reliable.

After Viterbi, the new failure contained only seven bad bytes, five in one
packet block. Ten shortened RS(216,192) blocks, each correcting twelve bytes,
converted this residual into correctable erasures/errors. The resulting mode
passed 6/6 radio frames. See
[`experiments/vf4/RESULTS.md`](../experiments/vf4/RESULTS.md).

#### VF5: tracking fixed phase; interleaving fixed burst concentration

VF5's pilotless 16-QAM candidate passed its software suite but failed both
first radio frames at 21-23% raw BER. Captures showed phase moving by tens to
more than 100 degrees during one frame, making the original rotation-orbit
labels ambiguous.

Ten full-band pilots repaired phase tracking. Conventional Gray I/Q mapping
reduced weak-direction BER further, but post-Viterbi errors were contiguous and
overloaded two RS blocks. Round-robin byte interleaving across RS codewords
spread the burst so no block exceeded its correction budget. The final mode
passed 6/6 at up to about 10% raw BER. See
[`experiments/vf5/RESULTS.md`](../experiments/vf5/RESULTS.md).

#### OFDM: training-band estimates overstated payload-safe bandwidth

The initial probe measured a 300-3,000 Hz common band and 0.815 ms worst-path
delay spread. It selected the 2.5 ms cyclic prefix correctly, but every
wideband QPSK payload failed despite strong sync. Ten of 55 carriers, mostly
at 300-550 Hz and the upper edge, held 83% of errors. Training SNR from repeated
known symbols did not predict whether every carrier could remain correct over
roughly 100 differently peaked payload symbols.

Trimming to 650-1,950 Hz and dropping to BPSK produced 28/28 radio frames.
Later 16-QAM work showed that denser pilots alone still failed. Rate-2/3 LDPC
and per-carrier LLR weights derived from payload hard-decision residuals—not
from only two adjacent training symbols—produced 24/24 at 500-2,400 Hz and
3,301 bit/s. The wider 500-2,600 Hz control stayed 0/4, so the remedy has a
measured boundary.

See [`experiments/ofdm/RESULTS.md`](../experiments/ofdm/RESULTS.md).

#### MFSK: AWGN ranked candidates but did not predict radio viability

The MFSK AWGN waterfall cleared 19 of 24 candidates relative to the baseline;
some candidates appeared to have 4 dB of margin and then decoded 0/5 on the
weak radio leg. The nearest failure had only 8 wrong symbols in 1,596, all tone
0 mistaken for tone 1 and spread through the frame. The evidence ruled out
clock drift and a missing low tone and instead implicated adjacent-tone leakage
through the real FM/audio chain.

The working corner was the highest symbol rate that allowed wider spacing:
four tones at 650 baud, separated by 0.833 symbol rates. It passed 45/45 in
each direction across two sessions. AWGN remains useful for ordering and
rejecting broken candidates, but not as the pass/fail gate. See
[`experiments/mfsk/RESULTS.md`](../experiments/mfsk/RESULTS.md).

#### A different handheld: squelch, clipping, and acquisition

VF5 passed 6/6 with the Wouxun but only 2/6 after substituting a Baofeng UV-B5.
All IC-705-to-UV-B5 captures lost the first 50-110 ms when squelch opened, and
the receiver selected a repetitive squelch tail or a damaged-header alignment.
Constrained offline acquisition found the real starts and decoded all frames.

The UV-B5 input was also 3-4 dB hotter and clipped up to 0.152% of samples.
Reducing volume removed clipping but also reduced useful margin and did not fix
acquisition. Opening squelch removed the blackout; the unchanged modem then
passed 6/6 with no clipping. The current operational remedy is therefore
open squelch, not simply lower audio gain. See the
[UV-B5 comparison](../experiments/vf5/results/BAOFENG_UVB5_COMPARISON.md).

#### A simulator-boundary defect that could masquerade as mode behavior

The first complex-FM CPFSK campaign made the 1,200-baud mode look unreliable:
454/600 frames. All 229 diagnostic failures acquired and differed in exactly
the final body-CRC bit. Processing 10 ms of post-frame audio through the same
stateful FM channel recovered known failures. The benchmark had appended
padding after channel processing, so the receive filter's response to the final
samples was absent.

Adding an explicit channel `drain()` contract raised the 1,200-baud rerun to
591/600 and all CPFSK rungs passed. This was not a real-radio discrepancy, but
it belongs here because an inaccurate finite-buffer boundary can create or hide
the same sort of difference. See the [CPFSK campaign](../logs/mode_qualification/vhf-fm/cpfsk/2026-08-29/INDEX.md)
and [`CHANNELS.md`](../CHANNELS.md).

### VHF FM simulation improvements suggested by the evidence

1. Use the complex RF-to-audio path, not recovered-audio AWGN alone: TX/RX
   filters, pre/de-emphasis, limiter, discriminator, squelch, clock mismatch,
   and channel drain are all load-bearing. The current model in
   [`CHANNELS.md`](../CHANNELS.md) is the right integration point.
2. Treat profiles as directional and radio-specific. Preserve measured audio
   magnitude, clock offset, delay spread, clipping level, and leading mute.
   Do not apply the Wouxun's 110 ms blackout as a universal handheld model.
3. Add distributions for squelch-open delay and damaged-header audio, including
   periodic idle/squelch tails. Run the real ranked acquisition algorithm over
   a long capture with plausible false candidates.
4. Retain a configurable open-squelch case and test it separately from closed
   squelch. Radio setup can be the remedy even when the PHY is unchanged.
5. Calibrate filter phase/group delay and adjacent-tone leakage. The measured
   presets currently reconstruct magnitude with a minimum-phase approximation;
   the original measurement did not identify actual phase.
6. Test payload symbols with their real PAPR and length. Repeated training-tone
   SNR is useful for prefix and candidate-band proposals, not payload safety.
7. Inject carrier-local cycle slips, common phase drift, and time-localized
   bursts. Verify differential modulation, pilots, FEC, and outer interleaving
   against each impairment separately.
8. Measure per-carrier/per-symbol errors and decoder convergence. Aggregate SNR
   and CRC outcome could not distinguish weak edges, burst concentration, or
   stale channel estimates.
9. Pair every software screen with a small weak-direction radio probe before a
   large sweep. Preserve failed captures and use replay to select the next
   change; VF2-VF5 all benefited from this loop.
10. Always drain stateful simulated stages, and surround frames with realistic
    pre/post audio inside the channel rather than appending silence afterward.

## Recommended future validation sequence

For either channel family:

1. Clean round trip to catch framing and implementation errors.
2. AWGN waterfall for coarse ordering, never final selection.
3. Full scenario with measured directional response, timing, nonlinearity,
   clock error, and realistic capture padding.
4. Ablations that isolate one impairment at a time and retain per-symbol,
   per-carrier, and pre/post-FEC diagnostics.
5. A few full-capacity frames over the known weak radio direction, saving every
   capture—not only failures.
6. Offline replay of those exact captures before changing the waveform.
7. Back-to-back A/B radio trials in the same session and direction.
8. Only then a predeclared, clean-tree qualification-sized run with complete
   RF, filter, squelch, audio-level, PTT, antenna/load, and direction metadata.

This sequence follows the evidence policy in
[`docs/TESTING.md`](TESTING.md), [`docs/HARDWARE.md`](HARDWARE.md), and
[`MODE_QUALIFICATION.md`](../MODE_QUALIFICATION.md), while making the
simulation-to-radio comparison an explicit design artifact rather than an
after-the-fact surprise.
