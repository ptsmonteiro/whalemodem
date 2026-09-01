# hf13_fast_sync_v1 -- real-hardware validation of the fused-FFT sync search

## Background

`experiments/hf5_8psk_4k_profiling/fast_sync.py` prototyped a fused-FFT
replacement for the 41-iteration frequency-offset sync search in
`experiments/hf5_8psk_4k/sc.py`'s `demodulate()` (lines ~330-348, plus
`_hilbert_envelope`). The original loop redundantly recomputes a
time-domain correlation and a fresh FFT/IFFT pair per frequency hypothesis;
the fused version computes one FFT of the capture (padded to a fast
composite length) and reuses it across all 41 hypotheses, fusing the
correlation-IFFT and Hilbert-envelope steps into a single per-hypothesis
forward+inverse FFT pair.

That prototype was validated only against synthetic AWGN simulation. This
experiment closes that gap with real captured over-the-air audio.

Both `sc.py` and `fast_sync.py` were used strictly read-only (imported,
never modified).

## Methodology

1. **Capture** (`capture_frames.py`, real hardware): modulated 10 random
   payloads with the unmodified `sc.SingleCarrierMode` at hf5's qualified
   operating point -- 8PSK @ 1500 baud, `packet_bytes=2994`,
   `pilot_interval=150` (RESULTS.md step 28, ~4049 bps, 5/5 on real
   hardware) -- keyed the IC-7300 (TX only), captured 12 kHz audio on the
   IC-705 (RX only, never keyed -- no code path in this experiment calls
   the IC-705 transport's `.send()`), and saved each raw capture plus its
   ground-truth payload to `captures/*.npz`. 10/10 captures succeeded
   (`captures/manifest_*.json`).
2. **Replay** (`compare_sync.py`, offline, no radio touched): for each of
   the 10 real captures, ran BOTH the original
   `sc.SingleCarrierMode.demodulate()` and `fast_sync.PatchedMode`'s
   fused-FFT-sync `demodulate()` on the *exact same* captured samples, and
   compared synced/crc_ok/payload/confidence/freq_offset_hz/channel_snr_db,
   plus wall-clock demodulate() time for both.

## Results

Full per-trial data: `compare_results.json`.

| trial | o_sync | f_sync | o_crc | f_crc | payload match (orig vs fast) | o_conf | f_conf | o_foff (Hz) | f_foff (Hz) | o_snr (dB) | f_snr (dB) | o_ms | f_ms | speedup |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 001 | True | True | True | True | True | 0.725 | 0.725 | -7.71 | -7.71 | 14.4 | 14.4 | 1384.1 | 288.2 | 4.80x |
| 002 | True | True | False | False | True | 0.687 | 0.687 | -7.81 | -7.81 | 14.5 | 14.5 | 1389.2 | 289.8 | 4.79x |
| 003 | True | True | True | True | True | 0.671 | 0.671 | -7.95 | -7.95 | 14.8 | 14.8 | 1390.4 | 292.0 | 4.76x |
| 004 | True | True | True | True | True | 0.668 | 0.668 | -8.01 | -8.01 | 15.0 | 15.0 | 1379.9 | 290.2 | 4.75x |
| 005 | True | True | True | True | True | 0.676 | 0.676 | -8.10 | -8.10 | 14.7 | 14.7 | 1399.7 | 297.0 | 4.71x |
| 006 | True | True | True | True | True | 0.689 | 0.689 | -8.15 | -8.15 | 14.7 | 14.7 | 1377.8 | 300.3 | 4.59x |
| 007 | True | True | True | True | True | 0.687 | 0.687 | -8.16 | -8.16 | 14.9 | 14.9 | 1381.8 | 284.9 | 4.85x |
| 008 | True | True | True | True | True | 0.673 | 0.673 | -8.20 | -8.20 | 14.7 | 14.7 | 1539.0 | 294.2 | 5.23x |
| 009 | True | True | False | False | True | 0.653 | 0.653 | -8.24 | -8.24 | 14.9 | 14.9 | 1377.0 | 289.4 | 4.76x |
| 010 | True | True | True | True | True | 0.743 | 0.743 | -8.24 | -8.24 | 14.8 | 14.8 | 1380.3 | 300.2 | 4.60x |

- **Discrepancies: 0 / 10.** `synced`, `crc_ok`, decoded payload,
  confidence, `freq_offset_hz`, and `channel_snr_db` are bit-for-bit
  identical between the original and fast-sync paths on every real
  capture, including the two trials (002, 009) where the frame failed CRC
  -- both implementations fail identically on the same two frames, which
  is exactly the equivalence being tested (this experiment measures
  sync-search equivalence, not link qualification; 8/10 raw decode on this
  particular capture batch reflects today's channel conditions, not a
  fast-sync defect, since the outcome is identical either way).
- Ground-truth payload match: orig 8/10, fast 8/10, on the *same* 8
  trials.
- **Real measured speedup: 4.60x-5.23x, average 4.79x** (demodulate() wall
  time: original ~1.38-1.54s, fast ~0.28-0.30s per frame), closely
  matching the ~5x speedup measured on synthetic AWGN captures in the
  original profiling.

## Verdict

**Real-hardware equivalence: PASS.** The fused-FFT sync search produces
identical decode outcomes to the original sync loop on every one of 10 real
over-the-air captures, with no edge case (non-flat noise, clipping, real
carrier drift) surfacing a discrepancy the synthetic AWGN test missed. The
optimization is safe to adopt. Proceeding to Stage 2.

---

## Stage 2: `sc_fast.py` qualification batch

`sc_fast.py` packages the fused-FFT sync search as a drop-in
`SingleCarrierMode`-compatible class (`SingleCarrierMode` in
`experiments/hf13_fast_sync_v1/sc_fast.py`) with an identical public API
(`modulate()`, `demodulate()`, `max_payload_bytes`, `frame_seconds()`) to
hf5's `sc.py` -- same PHY, only the sync-search implementation differs.
`hardware_test.py` runs it through the same direct-PHY trial harness as
hf5's, IC-7300(TX) -> IC-705(RX) only.

### Qualification batch (real hardware, IC-7300(TX) -> IC-705(RX))

Run: `python experiments/hf13_fast_sync_v1/hardware_test.py --trials 12`
(same config as the Stage 1 captures: 8PSK @ 1500 baud, `packet_bytes=2994`,
`pilot_interval=150`). Raw log: `logs/mode_qualification/hf-ssb/hf13_fast_sync_v1/20260901T181812Z/result.json`.

Note: an earlier run of this batch reported a spurious ~50% raw BER on
every trial including ones that decoded cleanly (CRC ok, payload matched
ground truth) -- that was a bug in `hardware_test.py`'s own ground-truth
bit computation (`_ground_truth_bits` re-applied the PN whitener a second
time on top of `demodulate()`'s already-dewhitened `raw_bits`), not a
defect in `sc_fast.py`. Confirmed and fixed with a synthetic no-channel
round trip (0/23952 bit errors after the fix, vs 12028/23952 before) before
re-running on real hardware; the table below is the corrected, re-run
result.

| trial | outcome | confidence | channel_snr_db | freq_offset_hz | demod_ms | raw_ber (errors/bits) | post_fec_ber |
|---|---|---|---|---|---|---|---|
| 1 | decoded | 0.741 | 15.4 | -8.06 | 309 | 0.00000 (0/23952) | null |
| 2 | crc_fail | 0.682 | 14.8 | -8.13 | 304 | 0.00004 (1/23952) | null |
| 3 | decoded | 0.686 | 14.9 | -8.18 | 304 | 0.00000 (0/23952) | null |
| 4 | decoded | 0.659 | 15.3 | -8.26 | 314 | 0.00000 (0/23952) | null |
| 5 | decoded | 0.667 | 14.7 | -8.33 | 301 | 0.00000 (0/23952) | null |
| 6 | decoded | 0.683 | 14.8 | -8.32 | 292 | 0.00000 (0/23952) | null |
| 7 | decoded | 0.673 | 14.9 | -8.39 | 292 | 0.00000 (0/23952) | null |
| 8 | decoded | 0.648 | 15.2 | -8.37 | 298 | 0.00000 (0/23952) | null |
| 9 | decoded | 0.669 | 15.0 | -8.38 | 304 | 0.00000 (0/23952) | null |
| 10 | decoded | 0.688 | 15.2 | -8.41 | 310 | 0.00000 (0/23952) | null |
| 11 | decoded | 0.679 | 14.7 | -8.46 | 299 | 0.00000 (0/23952) | null |
| 12 | decoded | 0.662 | 15.5 | -8.43 | 306 | 0.00000 (0/23952) | null |

- **Decode rate: 11/12 (91.7%)**, one near-miss CRC failure at trial 2 with
  a single bit error out of 23,952 (raw BER 4e-5 on that trial) -- a
  channel-margin event, not a sync or implementation defect.
- **Mean raw BER (all 12 trials): ~3.5e-6.** Post-FEC BER: **null on every
  trial** (this mode has no FEC, reported explicitly rather than
  fabricated).
- **Mean net throughput on decoded frames: ~4048.3 bps**, matching hf5's
  qualified ~4049 bps (this is a sync-search CPU optimization, not a PHY
  change, so the throughput figure should and does match).
- **Mean demodulate() time: ~303 ms/frame** on real hardware captures, vs.
  hf5's original ~1.3-1.5 s/frame measured in Stage 1 -- consistent with
  the ~4.8x real speedup already established.

## Final recommendation

The fused-FFT sync search (`sc_fast.py`) is **safe to adopt**: Stage 1
showed zero discrepancies against the original `sc.py` on 10 real captures
(identical sync/CRC/payload/confidence/freq-offset/SNR outcomes, including
matching failure cases), and Stage 2's qualification batch confirms
`sc_fast.py` decodes real hf5-baseline-equivalent frames reliably
(11/12, mean raw BER ~3.5e-6) at hf5's qualified ~4049 bps, while cutting
demodulate() CPU time by ~4.8x on real hardware (~1.4s -> ~0.3s per frame).

Recommend **`sc_fast.py` as the go-to implementation going forward** for
this 8PSK@1500baud/no-FEC operating point -- same PHY, same throughput,
same real-hardware reliability, at roughly a fifth of the CPU cost. It
should supplement (not blindly replace) `hf5_8psk_4k/sc.py`: hf5 remains
the frozen, read-only, independently-qualified reference this experiment
validated against, and any future PHY change (FEC, new modulation, pilot
tuning) should still be evaluated against that reference baseline. But for
runtime/production use where CPU headroom matters, `sc_fast.py` is the
recommended drop-in.
