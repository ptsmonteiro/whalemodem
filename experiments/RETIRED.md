# Retired experiments

Thirty-four experiment directories were retired on 2026-09-08. For each one the
Python, captures and per-trial data were deleted from the working tree and the
markdown evidence record was kept, so prose elsewhere in the repository that
cites `experiments/<name>/RESULTS.md` still resolves.

Nothing was rewritten out of git history. Every deleted file is still present in
commit `676770b` (the commit immediately before this pass), which is also the
commit that moved all shipped DSP out of `experiments/` into `whale/phy/` and
`whale/dsp/`.

## Recovering a deleted file

```sh
# print one file
git show 676770b:experiments/hf10_ofdm49_v6/ofdm49_v6.py

# restore one file into the working tree
git checkout 676770b -- experiments/hf10_ofdm49_v6/ofdm49_v6.py

# restore a whole directory
git checkout 676770b -- experiments/hf10_ofdm49_v6/

# list what a retired directory used to contain
git ls-tree -r --name-only 676770b experiments/hf10_ofdm49_v6/
```

Some data was exempted from deletion because surviving code, tests or docs read
it by path:

- `experiments/ofdm/results/measurements/bandwidth.json` - read by
  `whale/fm_channel.py` and `tests/test_fm_channel.py`.
- `experiments/vf3/results/final_dqpsk_both_3.json` - cited from
  `MODE_QUALIFICATION.md`.
- `experiments/vf3/results/captures/` (32 `.npy` on-air captures) - replayed by
  `tests/test_vf3_capture_replay.py` through `scripts/make_vf3_golden.py`, which
  builds the path rather than spelling it out.

One further directory, `experiments/hf5_8psk_4k_profiling/`, was emptied by an
earlier pass and its now-empty directory was removed here. It was the read-only
profiling investigation that prototyped the fused-FFT sync search without
touching the single-carrier PHY; that prototype shipped as
`whale/phy/fast_sync.py`, and `experiments/hf13_fast_sync_v1/RESULTS.md` holds
its equivalence and speedup evidence. Recover it the same way, from `676770b`.

"Retired" means the directory's own code is gone from the working tree. Several
of these experiments did ship: their DSP was rewritten into `whale/` in
`676770b`, and what is retired is the exploratory copy, not the idea.

---

## VF / VHF FM frame family

Five successive 5.200 s, 214-symbol VHF FM OFDM frames, each raising the
modulation order over broadly the same geometry.

| Directory | What it was | Why it is retired |
| --- | --- | --- |
| `vf2` | 29-carrier QPSK OFDM frame, rate-1/2 convolutional code, 714 B payload; decoded 6/6 on the IC-705/HT bench. | Superseded by VF3's full-density 58-carrier layout at the same airtime; nothing in `whale/` ever imported it. Its bit/byte helpers survive as `whale/dsp/bits.py`. |
| `vf3` | 58-carrier differential-QPSK OFDM, 1,436 B. A coherent payload was tried first and dropped for differential after a 4/6 confirmation. | Shipped: the mode lives in `whale/modes/vf3.py`. The directory is evidence only. |
| `vf4` | The same geometry at aligned two-ring star 8-QAM plus a shortened Reed-Solomon outer code, 1,914 B. | Shipped: `whale/modes/vf4.py`. |
| `vf5` | The same geometry at pilot-assisted Gray square 16-QAM with an RS outer code, 2,381 B; decoded 6/6 on air. | The record states no reason for abandonment. VF6 jumped straight to 256-QAM on the same frame and is what shipped; VF5 has no counterpart in `whale/modes/`. |
| `vf6` | Square 256-QAM on the VF5 frame: an 87,696-bit grid and 43 byte-interleaved shortened RS(254,238) codewords. | Shipped as opt-in mode ID 6 via `whale.modes.experimental_registry()`; code is `whale/modes/vf6.py`. Its campaign never statistically established 99% frame delivery, and the evidence is specific to synthetic `flat_nbfm` under otherwise excellent conditions. |

## HF SSB waveform experiments

| Directory | What it was | Why it is retired |
| --- | --- | --- |
| `hf2` | From-scratch pilot-assisted OFDM targeting Level 2 of `SPEED_LADDERS.md` (500 bit/s); passed its >=300-trial envelope gate in simulation. | Shipped: the waveform is `whale/phy/hf2.py` and the mode `whale/modes/hf2_mode.py`. Kept for the DESIGN/PLAN/RESULTS record. |
| `hf3_fec34` | HF4's 16-QAM OFDM geometry on a shorter 36-symbol frame with a punctured rate-3/4 K=7 code and coded bits duplicated across separated carrier groups. | Shipped: `whale/phy/hf3.py`, `whale/modes/hf3_mode.py`. The directory already held only its README before this pass. |
| `hf4` | From-scratch maximum-speed Level 4 waveform for a deliberately narrow benign/static envelope at +13 dB and above. | Shipped: `whale/phy/hf4.py`, `whale/modes/hf4_mode.py` (mode 11, currently DEFAULT in `MANIFEST`). The documented qualification failures outside that envelope still stand. |
| `hf5_8psk_4k` | Single-carrier 8PSK at 1500 baud with mid-frame pilots, about 4,050 bps net on the IC-7300 to IC-705 path. The bench's throughput record for a long time. | Shipped: `whale/phy/sc.py`. It was also the frozen read-only reference that hf6 through hf13 measured themselves against. |
| `hf6_multicarrier_v2` | Small-N multicarrier: 3 carriers at 800/1500/2200 Hz, QPSK, 600 baud each, mid-frame pilots. | Did not beat the single-carrier baseline: about 3,200 bps against hf5's 4,050 bps. |
| `hf7_ofdm_v3` | True IFFT plus cyclic-prefix OFDM: 6 orthogonal subcarriers, 8PSK, 25% CP. | Did not beat either earlier baseline: about 2,520 bps, the weakest of the family. |
| `hf8_band_placement_v4` | Clustering 2 carriers into the empirically best-SNR region of the passband, 8PSK at 600 baud each. | Clustering did not help: about 3,140 bps, below single-carrier and only roughly matching hf6's wide-spread layout. |
| `hf9_ofdm49_v5` | 49 contiguous subcarriers spanning 300-2700 Hz edge to edge, true OFDM, about 4,014 bps: statistically matching the single-carrier record and clearly beating hf6, hf7 and hf8. | Shipped: the 49-carrier PHY is `whale/phy/ofdm49.py`. This is the experiment that established the geometry everything after it uses. |
| `hf10_ofdm49_v6` | 16-QAM plus a rate-3/4 802.11n QC-LDPC code on the hf9 49-bin PHY. Plain 16-QAM reproduced the bench's known real-hardware 16-QAM fragility (0/3); LDPC rescued it at about 4,332 bps qualified. | Shipped: folded into `whale/phy/ofdm49.py`. Its `hardware_test.main` was the shared runner that hf11, hf18 and hf19 re-exported, so all four retire together. |
| `hf11_ofdm49_v6_duration` | Pushing hf10's frame duration 2x-3x, from 176 B/0.325 s to 352 B/0.575 s and 528 B/0.850 s, for about 4,969 bps. | The samples were too small to separate the arms (about 71-75% delivery either way), so it never became a qualification result and no mode adopted the longer frames. |
| `hf12_sc_fec_v7` | Adding LDPC (rates 1/2, 2/3, 3/4) to the hf5 single-carrier PHY to try to beat its uncoded 4,050 bps. | On this SNR-rich, bandwidth-limited channel rate-3/4 is exactly the break-even point for 16-QAM over 8PSK, not a margin. Beating hf5 needs a rate-7/8 code that does not exist here, so the OFDM line was judged the better lever. |
| `hf13_fast_sync_v1` | A fused-FFT replacement for the 41-iteration frequency-offset sync search in hf5's `demodulate()`; cut demodulate CPU 4.6-4.8x with identical throughput and reliability across two hardware sessions. | Shipped: `whale/phy/sc_fast.py`, with the fused search in `whale/phy/fast_sync.py`. |
| `hf15_lowsnr_ofdm` | A path sounder, not a modem: a periodic OFDM burst measuring per-subcarrier channel response, effective noise, SNR and inter-radio CFO across 300-2700 Hz, plus a coherent-integration analysis of how long a symbol may be. | This directory contained no markdown, so nothing survives it here. It was measurement infrastructure for a low-SNR OFDM design that was never built; the record states no reason for stopping. |
| `hf17_diagnostics` | Retained-capture receiver diagnosis aimed at 7 kbit/s over IC-7300 to IC-705, comparing comb-tracking modes on nine captured frames. | Nine frames, zero full-frame successes; confidence-weighted tracking showed only a modest replay benefit. Explicitly a smoke campaign, not qualification. Its capture artifacts live under `logs/mode_qualification/hf-ssb/hf17_ofdm64/` and were not touched. |
| `hf18_ofdm49_vara` | 49 carriers at 50 Hz spacing, VARA HF's top-speed geometry: 7,586 bps on air at matched 5 s airtime, delivering as often as the wider-spaced layout. | Not adopted as a rung: occupied bandwidth is about 2,450 Hz, above the 2,300 Hz ceiling `SPEED_LADDERS.md` sets, and the result is one direction, one session, one day's channel. Explicitly provisional. Its `RESULTS.md` is still cited from `whale/mode_qualification.py`. |
| `hf19_ofdm49_8psk` | HF7's 49-carrier plan at 8PSK instead of 32-QAM: moved the 90%-delivery AWGN boundary from 20 dB to 12 dB and bought the family's first measured fading envelope. | Shipped in substance as HF8 (`whale/modes/hf8_mode.py`), the robust rung between HF3 and HF4. The directory is the supporting evidence. |

## HC control-family experiments

| Directory | What it was | Why it is retired |
| --- | --- | --- |
| `hc2` | Candidate fast rung above HC1: HC1's exact OFDM geometry with differential 8-PSK and a rate-1/2 K=9 inner code. | A documented dead end - see `RESULTS.md`'s "Why it failed: the aggressive-order trap". The K=9 code could not buy back 8-PSK's loss. Its K=9 polynomials survive as `whale/dsp/fec.K9`. |
| `hc2b` | The cheap follow-up question left by hc2: how much rate does HC1 gain purely by amortizing its fixed header over longer QPSK payloads (34, 58, 90 and 130 payload symbols)? | Longer frames are useful only as a quiet-channel high-rate mode; they do not fix HC1's moderate/disturbed fading weakness, and the 2.055 s frame did not meet the reliability and confidence gate. |
| `hc2c` | hc2b's selected 1.521 s frame with eight evenly spaced known full-band payload pilots, measured against exactly paired channel realizations. | Pilots won at every moderate and disturbed point but did not solve HC1's underlying frequency-selective-fade failure, and quiet-channel results were mixed. No mode adopted the piloted frame. |
| `hc2_32qam` | 49-carrier coherent 32-QAM rate proof with an oracle-aligned receiver, plus an EVM health metric and a fading boundary campaign. | A milestone-1 rate proof, explicitly not registered, negotiable or qualified. Its first AWGN sweep was superseded mid-experiment by a receiver fix, and an estimated 2.4 dB hard-decision demapping penalty remained unaddressed. |

## HR control-family experiments

| Directory | What it was | Why it is retired |
| --- | --- | --- |
| `hr0-local-backup` | A local snapshot of the HR0 oracle-start viability prototype: 53 real BPSK OFDM carriers over about 1,988 Hz, an 8.04 s frame and 52.75 bit/s useful rate, decoding from the exact first sample with zero CFO assumed. | A backup copy. Superseded by `experiments/hr0_fast_control/` (kept, still imported by the tests) and by the shipped `whale/modes/hr0.py`. |
| `hr1` | A multi-revision design campaign for the most robust HF Level 0 rung: guarded sequential 16-MFSK over GF(16) with RS, revisions A, B and C, documented across BASELINE, DESIGN, DESIGN_C, IMPLEMENTATION, PLAN, REDESIGN and SCREEN. | Stopped at "redesign before the full campaign". HR1-B's tiny-ACK class failed the fixed 20 bit/s Level 0 session gate, and its 19.54 s full frame exceeded the production 10 s RX buffer and the 8 s HF useful-frame policy. Never registered. All seven design documents are kept. |

## Shared DSP, leads and probes

| Directory | What it was | Why it is retired |
| --- | --- | --- |
| `ofdm` | The original FM-bench OFDM experiment: maximum payload inside a single 3 s keying, no FEC and no compression, on the IC-705 / Wouxun HT FM path. It also produced the per-carrier LLR weighting result. | The optimistic wide-band QPSK hypothesis did not survive the bench. Per-carrier LLR weighting was worth 21.1% and stopped at 500-2400 Hz; going wider needs a transmitter-limiter amplitude correction that was never implemented. Its LDPC code is an ancestor of `whale/dsp/ldpc.py`, and `results/measurements/bandwidth.json` is kept because `whale/fm_channel.py` reads it. |
| `qpsk29` | 29-carrier QPSK OFDM frame built from a handed-down specification, exercised on the IC-7300 to IC-705 HF path at up to 1,406 bit/s. | Explicitly not added to the `hf-ssb` ladder: it occupies 2,625-2,719 Hz against a 2,300 Hz target and keys for 5.200 s against a 3.0 s cap, and its decode time still needs profiling on a representative low-end target. Its LDPC implementation is the direct ancestor of `whale/dsp/ldpc.py`. |
| `mfsk` | An M-ary FSK frame mode for the FM bench sized to the 3.0 s keying cap; best profile `4fsk_650bd_x0.833` at 650 baud on four Gray-coded tones. | The passband ceiling capped the ladder: nothing wider than a 0.833 spacing ratio fit, so only one operating point was ever reachable, and the nearest miss failed on adjacent-tone leakage that no FEC was in place to fix. The four follow-ups its `RESULTS.md` lists were never run. |
| `lead` | A "musical lead": one head that burns through squelch and ALC, labels the frame and times it, replacing the throwaway repeated-block head every mode sends. | Superseded within days by `lead2`, which does the same job in a quarter of the airtime. Its 0.68 s minimum cost 20% of HC0's keying and 98% of HC1's frame, a trade never measured at link level. Simulation only; no over-the-air run. |
| `lead2` | The same idea in one figure at a quarter of the airtime: 5.0% overhead on HC0's keying and 24.6% on HC1's frame. | Superseded by the `signature128` line, which is what `whale/modes/hf_lead.py` implements. Its own caveats stand: no over-the-air run, no interference or sample-clock screen, and the link-level value of the trade was never measured. |
| `signature128` | The qualification experiment that selected the common HF lead: the shortest repeated HC0-grid MFSK signature no less robust than HC0, measured as the paired probability that the signature misses given that the HC0 frame following it decodes. | Shipped: the chosen fixed sequences and the receiver are `whale/modes/hf_lead.py`, plus the `LeadFormat` architecture its `ARCHITECTURE.md` describes. The retained `RESULTS.md` is a pilot that sizes an experiment; it does not itself qualify the wire format. |
| `path_probe` | Raw channel characterization before designing anything: a Newman-phase sum-of-sinusoids across 300-2700 Hz, IC-7300 to IC-705, reporting per-tone amplitude, phase and SNR plus the noise floor with the transmitter silent. | One-off measurement tooling with no markdown record. It produced no design artifact of its own and states no reason for being set aside. |
