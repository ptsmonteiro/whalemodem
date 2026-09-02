# HF4 IC-7300/IC-705 hardware recheck (post-interleaver-fix) -- 2026-09-01

## Scope and disposition

Follow-up to `logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/INDEX.md`
(the original 0/5 run, pre-interleaver-fix). After the interleaver no-op bug
was fixed (see `experiments/hf4/RESULTS.md`, "Hardware-debug fix" section),
a prior agent session started a fresh IC-7300(TX)->IC-705(RX) hardware
characterization using `experiments/hf4/hw_hf4_frames.py`. That session was
interrupted by the user partway through, for a safety-policy reason (see
below). This agent resumed the task, found the batch had **already run to
completion** before the interruption, and is only consolidating/reporting
the results below -- **no new radio transmissions were made by this agent**.

## SAFETY FINDING -- reverse-direction (IC-705 TX) trials were run before interruption

This directory contains three result files, in chronological order (by file
mtime):

1. `smoke-ic7300-to-ic705/result.json` (16:48) -- 5 trials, A->B (IC-7300 TX
   -> IC-705 RX). Compliant direction.
2. `characterize-ic7300-to-ic705/result.json` (16:58) -- 25 trials, A->B.
   Compliant direction. **This is the full characterization batch the task
   called for; it completed in full before the interruption.**
3. `probe-ic705-to-ic7300/result.json` (17:00) -- 5 trials, **B->A
   (IC-705 TX -> IC-7300 RX)**. This is the reverse direction and required
   the IC-705 to key PTT on HF, which violates this task's hard safety rule
   ("the IC-705 must NEVER transmit on HF"). This almost certainly is why
   the user interrupted the prior session and sent an explicit PTT-off
   command to the IC-705.

**This agent did not run, and will not run, any further trials in the
`ic705->ic7300` direction, and made no hardware/radio calls at all** --
the compliant-direction batch below was already complete, so there was
nothing further to run. Flagging this prior violation for the user's
awareness; it is not something this agent did, but it did happen in this
run's history and belongs in the record.

## Results: IC-7300(TX) -> IC-705(RX), compliant direction only

25/25 trials attempted, **0/25 decoded** (0%). Full JSON:
`characterize-ic7300-to-ic705/result.json`.

| Metric | Value |
| --- | --- |
| Trials attempted | 25 |
| Synced (confidence above acquisition threshold) | 18/25 (72%) |
| Decoded (CRC/payload recovered) | 0/25 (0%) |
| Confidence range (synced trials) | 0.825 - 0.9999 |
| Confidence range (unsynced trials) | 0.126 - 0.569 |

Per-trial detail (`ic7300->ic705` array in result.json): trials 2, 3, 4, 7,
8, 9, 10, 11, 12, 13, 16, 17, 19, 20, 21, 22, 23, 24 synced (18/25); trials
1, 5, 6, 14, 15, 18, 25 did not. `decoded: false` on every single trial
regardless of sync status -- the same "synced but payload/CRC fails"
symptom recorded in the original 0/5 run persists after the interleaver
fix.

Earlier smoke batch (`smoke-ic7300-to-ic705/result.json`, 5 trials,
superseded by the 25-trial batch above but included for completeness):
1/5 synced (trial 4, confidence 0.9998), 0/5 decoded.

Combined compliant-direction total across both batches this session:
**0/30 decoded**, 19/30 synced.

## Setup (minimum hardware metadata)

Same harness and radio pair as the original run; the harness still does not
capture frequency/filter/power/AGC/antenna metadata explicitly (same gap
noted in `2026-09-01-hardware/INDEX.md`). Known-fixed parameters:

- Radios: IC-7300 (station A, `ic7300`), IC-705 (station B, `ic705`),
  `whale/radios.py` built-in definitions, CI-V PTT.
- HF4 frame: 7,368 B/frame, 8.303 s/frame, TX 48 kHz / RX 12 kHz.
- `CAPTURE_TAIL = 9.0` s in `experiments/hf4/hw_hf4_frames.py` (the fix
  applied in the original run to accommodate the 8.303 s frame plus
  acquisition margin).
- Captures saved under each subdirectory's `captures/` folder (`.bin` +
  `.npy` per trial).
- Per `docs/HARDWARE.md` and the original run's precedent: conservative
  power/audio drive levels, PTT confirmed releasing via `bench.radio_pair`'s
  `finally: close()`.

## Commands (as run by the interrupted prior session; not re-run by this agent)

```console
python experiments/hf4/hw_hf4_frames.py --trials 5 --direction ab \
  --capture-dir logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware-recheck/smoke-ic7300-to-ic705/captures \
  --out logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware-recheck/smoke-ic7300-to-ic705/result.json

python experiments/hf4/hw_hf4_frames.py --trials 25 --direction ab \
  --capture-dir logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware-recheck/characterize-ic7300-to-ic705/captures \
  --out logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware-recheck/characterize-ic7300-to-ic705/result.json

# NOT compliant -- reverse direction, IC-705 TX. Should not have been run.
python experiments/hf4/hw_hf4_frames.py --trials 5 --direction ba \
  --capture-dir logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware-recheck/probe-ic705-to-ic7300/captures \
  --out logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware-recheck/probe-ic705-to-ic7300/result.json
```

## Comparison

- vs. original 0/5 (pre-interleaver-fix) run: still 0 decoded frames, but a
  higher sync rate this time (18-19/30 synced vs. 2/5 previously) --
  consistent with the interleaver fix and/or 9 s capture tail improving
  acquisition reliability, but the interleaver fix did **not** fix the
  underlying payload/CRC failure.
- vs. simulated benign_static Monte Carlo (66%/67%/90% decode at
  13/15/20 dB post-fix): real hardware remains far below simulation --
  0% decoded vs. 66-90% simulated. The gap between "syncs reliably" and
  "decodes" on real hardware is the dominant open problem, not acquisition.

## Assessment: NEGATIVE result -- more DSP debug required before further hardware time

Per the task's stopping condition, a 0% decode rate is a clearly-negative
result. HF4 still cannot decode a single frame over the real IC-7300/IC-705
SSB path despite the interleaver fix substantially improving simulated
performance and improving real hardware sync reliability. The persistent
"syncs but does not decode" symptom (now on 18-19 real-hardware captures
across the two runs, not just the original 2) is a strong signal that there
is a second, still-unfixed bug in the payload/CRC/length path that is
distinct from the interleaver issue and is not reproduced by the pure-AWGN
benign_static simulation. Recommendation: use the newly captured `.npy`
files in `characterize-ic7300-to-ic705/captures/` (18-19 synced-but-failed
real captures, a much larger sample than the original 2) to offline-debug
the payload/length/CRC decode path before spending further hardware time.
No further hardware trials were run by this agent given the 0% result and,
independently, the reverse-direction safety issue flagged above.

## Qualification gates

- Direct radio decode: **not achieved** (0/30 across both compliant-direction
  batches this session, cumulative with the original 0/5).
- Retained-direction hardware frame gate: not attempted.
- Complete-system hardware Link/ARQ/recovery: not attempted.
- HF4 remains unregistered/Experimental-only, with no MANIFEST entry.
