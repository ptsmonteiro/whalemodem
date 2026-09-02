# HF4 IC-7300/IC-705 hardware exploratory test -- 2026-09-01

## Scope and disposition

This is an **exploratory characterization run**, not a `MODE_QUALIFICATION.md`
promotion campaign and not formal retained-direction bookkeeping. HF4 has no
`whale.mode_qualification.MANIFEST` entry and is not reachable through
`scripts/sweep_modes.py`'s registry-based mode selection (see
`whale/modes/hf4_mode.py`). A bench-only hardware harness,
`experiments/hf4/hw_hf4_frames.py`, was written for this run: it drives
`experiments.hf4.hf4.modulate`/`demodulate` directly and reuses
`scripts/bench.py`'s `radio_pair` open/warm-up/close dance, mirroring
`scripts/hw_hf_frames.py`'s method (the same "no MANIFEST" spirit as
`experiments/hf4/benchmark_hf4.py`'s mode_id=244 simulation-only adapter).
It does not modify `hf4.py`'s DSP.

**Result: HF4 did not decode any frame over real radios in this run (0/5
attempts across both directions).** Acquisition/sync succeeded on some
attempts once the capture window was sized correctly, but payload/header
recovery failed every time it synced. See Diagnosis below.

## Setup (minimum hardware metadata)

- Radios: IC-7300 (station A, `ic7300` legacy inventory entry) and IC-705
  (station B, `ic705` legacy inventory entry) -- the source tree's built-in
  audio-device/CI-V definitions in `whale/radios.py`; no `radios.toml` exists
  in this checkout (only `radios.example.toml`), same as the HF2/HF3
  hardware campaigns.
- Frequency, filter bandwidth/mode, TX power, ALC/AGC state, antenna vs.
  dummy-load path, and cabling were **not captured by the harness**, the
  same gap HF2's and HF3's 2026-09-01 hardware INDEX.md files record for
  this identical radio pair. Per those records and `docs/HARDWARE.md`'s
  safety section, the run was conducted starting from low/conservative
  power and audio drive levels with PTT confirmed to release cleanly after
  every trial (`bench.radio_pair`'s `finally: close()` on both transports).
  These operator-side settings should be captured explicitly in any future,
  promotion-grade HF4 hardware campaign.
- PTT method: CI-V (`icom-civ` backend), same as HF2/HF3.
- Direct modulate -> TX -> capture -> demodulate trials, bypassing
  `whale.link`'s ARQ, per `docs/HARDWARE.md`.
- HF4 frame: 7,368 B/frame, 8.303 s/frame, TX 48 kHz / RX 12 kHz.
- `WHALE_CAPTURE_DIR` was not set (env var); captures were instead saved
  explicitly via `--capture-dir` into the `captures/` subdirectories below
  for every trial (not just failures), since every trial in this run failed.

## Commands and artifacts

Initial smoke (capture_tail=2s, insufficient -- see Diagnosis):

```console
python experiments/hf4/hw_hf4_frames.py --trials 1 --direction ab \
  --capture-dir logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/smoke-ic7300-to-ic705/captures \
  --out logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/smoke-ic7300-to-ic705/result.json
```

Repeated after raising `CAPTURE_TAIL` to 9.0 s in the harness (frame is
8.303 s and the sync search located the frame anywhere from ~1.2 s to
~8.3 s into the post-warmup capture, so a short tail truncates the payload
before the decoder can even attempt it):

```console
python experiments/hf4/hw_hf4_frames.py --trials 1 --direction ab \
  --capture-dir logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/smoke-ic7300-to-ic705/captures \
  --out logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/smoke-ic7300-to-ic705/result.json
```

Confirmatory batch, A->B (3 trials):

```console
python experiments/hf4/hw_hf4_frames.py --trials 3 --direction ab \
  --capture-dir logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/confirm-ic7300-to-ic705/captures \
  --out logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/confirm-ic7300-to-ic705/result.json
```

Single reverse-direction probe, B->A (1 trial):

```console
python experiments/hf4/hw_hf4_frames.py --trials 1 --direction ba \
  --capture-dir logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/confirm-ic7300-to-ic705/captures \
  --out logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/confirm-ic7300-to-ic705/result_ba.json
```

Git commit for all runs: `14c20147b55ebce804281fc1f4468c71b938c840`; the tree
was dirty (several untracked experiment/hf4 files, consistent with HF4 not
yet being committed).

## Results

| Trial | Direction | Confidence | Synced | Freq offset | Carriers present | Decoded length | Result |
| --- | --- | ---: | --- | ---: | ---: | ---: | --- |
| Smoke (2 s tail) | A->B | 0.940 | No | -- | -- | -- | sync found late in a too-short capture; buffer truncated before header/payload window -- inconclusive, not a DSP failure |
| Smoke (9 s tail) | A->B | 0.281 | No | -- | -- | -- | below acquisition threshold (0.60); no confident sync |
| Confirm 1 | A->B | 0.593 | No | -- | -- | -- | below acquisition threshold; no confident sync |
| Confirm 2 | A->B | 0.999 | Yes | +5.49 Hz | 117/149 | 35242 | synced, but CRC failed; payload not decoded |
| Confirm 3 | A->B | 0.992 | Yes | +6.30 Hz | 98/149 | 35242 | synced, but CRC failed; payload not decoded |
| BA probe 1 | B->A | 0.758 | No | -- | -- | -- | sync found late; capture window (9 s tail) still cut the frame short in this direction |

Attempted: 5 frames (4 x A->B, 1 x B->A). Decoded exactly: **0/5**.

## Diagnosis

1. **Capture-window sizing was the first-order problem, and it is a harness
   issue, not a waveform issue.** HF4's frame is 8.303 s and the sync
   search sometimes locates the true frame start anywhere from ~1.2 s to
   ~8.3 s into the post-`tx.send()` capture (radio audio-path latency,
   USB buffering, and PTT lead/trail vary trial to trial). A short
   `CAPTURE_TAIL` (2 s, matched to HC0/HC1/HF2/HF3's much shorter frames)
   truncates the header/payload window before the decoder can even attempt
   a CRC check, and reads as a plain sync failure. Raising `CAPTURE_TAIL`
   to 9 s let two of five trials reach a full header+payload decode
   attempt. A production hardware harness for HF4 should size its capture
   window to `hf4.FRAME_SECONDS` plus several seconds of margin on both
   sides, not reuse the shorter-frame defaults.
2. **Once the decoder did reach the payload stage (2/5 trials), it
   consistently reported the same wrong length field: `decoded_length =
   35242` both times**, despite different channel realizations (different
   frequency offsets: +5.49 Hz vs. +6.30 Hz; different carrier-presence
   counts: 117/149 vs. 98/149; visibly different per-carrier SNR shapes).
   Two independent noisy decodes landing on the exact same wrong 16-bit
   length value is not consistent with the length field simply being
   corrupted by random noise (that would need the same coincidence twice,
   independently, at roughly 1-in-65536 odds) -- it points at a systematic
   decode issue on the real hardware path (e.g. a residual channel-estimate
   or phase artifact from the SSB filter/AGC/ALC chain that the
   +13 dB-benign/static header-only equalizer in `hf4.py` does not track,
   as opposed to ordinary AWGN). This is consistent with, and arguably
   worse than, the 70.33% simulated decode rate already recorded in
   `experiments/hf4/RESULTS.md` -- the real hardware path did not decode a
   single frame in this run, though the sample size (2 synced attempts) is
   far too small to treat as a rate.
3. Per-trial confidence and sync outcome were noisy across nominally
   identical trials at what should be a stable channel (0.281, 0.593, 0.999,
   0.992 confidence across four A->B attempts within the same short
   session), suggesting the acquisition/header search is itself sensitive
   to something in this real audio path (timing jitter, ALC/AGC pumping, or
   USB audio buffering) beyond what the synthetic benign/static channel
   model exercises.
4. Per `docs/HARDWARE.md`'s guidance and the task's instructions, this run
   did not chase the header/length decode bug further (e.g. replaying
   captures against a modified decoder, or sweeping capture offsets) --
   that is DSP debugging work for the next design iteration, not this
   hardware smoke/characterization pass.

## Recommendation

The larger 20-40 frame characterization batch was **not run**: the smoke
test did not decode successfully (0/5), so per the task's own escalation
criterion there was nothing to characterize at scale yet. The real-hardware
result is roughly consistent with -- if anything slightly worse than -- the
70.33% simulated decode rate, and it surfaces a second, distinct failure
mode (a repeatable wrong length field on synced frames) that the pure-AWGN
simulated `benign_static` model may not be reproducing. Recommendation:
before investing further in hardware campaigns, (a) fix the hardware
harness's capture-window sizing (done here, but should be folded into any
reusable script) and (b) use the two saved synced-but-failed captures in
`confirm-ic7300-to-ic705/captures/` to offline-debug the header/length
decode path -- that is a more productive next step than either more
hardware trials or more benign/static Monte Carlo runs at this point.

## Qualification gates

- Direct radio decode: **not achieved** (0/5).
- Retained-direction hardware frame gate: not attempted (no successful
  frames to build on).
- Complete-system hardware Link/ARQ/recovery: not attempted.
- Optional/default promotion: not supported by this campaign.
- HF4 remains unregistered/Experimental-only, with no MANIFEST entry.
