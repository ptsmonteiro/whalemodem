# HF3 IC-7300 to IC-705 hardware smoke -- 2026-09-01

## Scope and disposition

HF3 decoded 3/3 full-capacity physical frames byte-for-byte from the IC-7300
(station A, transmitter) to the IC-705 (station B, receiver). This is the
first retained successful HF3 result over real radios and verifies the
waveform on this favorable direction. IC-7300 -> IC-705 is the declared
retained direction. This is **provisional smoke evidence**, not a pass of the
retained-direction hardware frame gate: the >=40-frame minimum was not met.
The reverse direction and a bidirectional Link/ARQ/recovery session are not
requirements of the waveform frame gate; they belong to characterization and
complete-system qualification respectively.

The run used the source tree's legacy `ic7300` and `ic705` inventory entries:
their USB audio devices and CI-V PTT backends. Both radios' RF frequency,
mode/filter settings, firmware, transmit power, antenna/dummy-load path,
cabling, and operator audio-level settings were not captured by the harness
and are therefore unrecorded. This minimum setup-record gap is an additional reason the
campaign is not promotion-grade evidence.

## Commands and artifacts

Initial smoke:

```powershell
python scripts/sweep_modes.py --channel hf-ssb --mode-level experimental --modes hf3 --direction ab --a ic7300 --b ic705 --trials 1 --capture all --output-dir logs/mode_qualification/hf-ssb/hf3/2026-09-01-hardware/smoke-ic7300-to-ic705
```

Confirmatory pair:

```powershell
python scripts/sweep_modes.py --channel hf-ssb --mode-level experimental --modes hf3 --direction ab --a ic7300 --b ic705 --trials 2 --capture all --output-dir logs/mode_qualification/hf-ssb/hf3/2026-09-01-hardware/confirm-ic7300-to-ic705
```

The two `result.json` files contain per-trial outcomes, keyed time, decoder
metrics, seed, registry IDs, Git state, and paths to all three compressed
audio/payload captures. Git commit was
`9376a0c9583763091fcb2cf2a5bfae0adc1005b4`; the tree was dirty.

## Results

| Trial | Payload | Confidence | CFO | Clock estimate | Carriers | Carrier SNR min/mean | Keyed | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Smoke 1 | 803 B | 0.962 | -8.136 Hz | -109.7 ppm | 36/36 | 6.62/11.76 dB | 3.337 s | decoded, CRC valid |
| Confirm 1 | 803 B | 0.974 | -8.203 Hz | -103.0 ppm | 36/36 | 7.77/13.29 dB | 3.323 s | decoded, CRC valid |
| Confirm 2 | 803 B | 0.981 | -8.079 Hz | -6.8 ppm | 36/36 | 13.59/20.10 dB | 3.331 s | decoded, CRC valid |

All retained captures replay through the HF3 decoder byte-for-byte. Capture
peaks were 0.187-0.192, with no indication of full-scale clipping. The clock
estimate varied substantially on the third frame, but timing tracked every
frame successfully; a larger campaign is needed to distinguish a real
sample-clock mismatch from estimator variation.

The summaries' 1,901-1,907 bit/s use the production 793-byte DATA chunk and
measured keyed time. This is a direct-frame diagnostic, not useful application
throughput: it excludes ACKs, retries, turnaround, connection, and disconnect.
The 803-byte physical waveform payload must not be used as an
application-throughput claim.

## Qualification gates

- Direct radio decode: provisional retained-direction smoke success (3/3).
- Retained-direction hardware frame gate: provisional, below 40 frames.
- Complete-system hardware Link/ARQ/recovery: unmeasured, but not a waveform
  promotion gate.
- Optional/default promotion: not supported by this campaign.
- HF3 remains Experimental only.
