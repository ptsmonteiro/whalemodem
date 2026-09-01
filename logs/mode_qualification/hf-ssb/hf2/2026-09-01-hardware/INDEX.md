# HF2 IC-7300 to IC-705 hardware campaign -- 2026-09-01

## Scope and disposition

This index covers two runs over the same radio pair and direction: a 3-frame
smoke/confirm pair and a subsequent 40-frame retained-direction campaign.
HF2 decoded 43/43 full-capacity physical frames byte-for-byte from the
IC-7300 (station A, transmitter) to the IC-705 (station B, receiver) across
both runs, including 40/40 in the retained-direction campaign proper.
IC-7300 -> IC-705 is the declared retained direction, and the 40-frame run
numerically clears section 3's FER/acquisition Wilson gate for this
direction (see "Qualification gates" below).

**This is not, and cannot be, a promotion pass, regardless of the numbers
below.** HF2's occupied-bandwidth gate is a known, currently-open, and
already-documented failure: HF2's own carrier plan puts its top carrier at
2,343.75 Hz, above the channel's 2,300 Hz ceiling by design, and the
measured 99%-power occupied bandwidth is approximately 4,200 Hz -- nearly
double the ceiling, by a wide, non-marginal margin. See
`logs/mode_qualification/hf-ssb/hf2/2026-09-01-bandwidth/INDEX.md`. This
hardware campaign does not measure occupied bandwidth, does not touch the
waveform's spectral leakage, and cannot cure that failure; it only
characterizes frame delivery over one specific radio pair. **HF2 must
remain Experimental-only, and Optional/Default promotion stays blocked,
until the bandwidth gate's root cause is diagnosed, fixed, and re-measured
-- irrespective of how cleanly this or any other hardware frame campaign
clears the FER/acquisition gate.**

Separately, this campaign's radio pair's actual TX/RX filter passband width
was not captured or verified against the bandwidth gate's failure (see the
setup-record gap below). A clean hardware pass on this pair says nothing
about channel-plan compliance: it may simply mean this particular IC-7300
transmit filter and IC-705 receive filter happened to pass enough of HF2's
too-wide signal to decode, not that the signal fits the channel plan. Any
reader must not treat this campaign as evidence toward, or against, the
bandwidth gate.

The 40-frame run also does not by itself satisfy every condition needed for
promotion consideration even setting bandwidth aside: it is a physical-layer
frame sweep (bypasses Link/ARQ/negotiation/sockets), the setup record is
incomplete (below), and resource evidence remains unmeasured.

The run used the source tree's legacy `ic7300` and `ic705` inventory
entries: their USB audio devices and CI-V PTT backends. Both radios' RF
frequency, mode/filter settings, firmware, transmit power, antenna/dummy-load
path, cabling, and operator audio-level settings were not captured by the
harness and are therefore unrecorded. This minimum setup-record gap is an
additional reason the campaign is not promotion-grade evidence on its own
terms, separate from the bandwidth-gate blocker above.

## Commands and artifacts

Initial smoke:

```powershell
python scripts/sweep_modes.py --channel hf-ssb --mode-level experimental --modes hf2 --direction ab --a ic7300 --b ic705 --trials 1 --capture all --output-dir logs/mode_qualification/hf-ssb/hf2/2026-09-01-hardware/smoke-ic7300-to-ic705
```

Confirmatory pair:

```powershell
python scripts/sweep_modes.py --channel hf-ssb --mode-level experimental --modes hf2 --direction ab --a ic7300 --b ic705 --trials 2 --capture all --output-dir logs/mode_qualification/hf-ssb/hf2/2026-09-01-hardware/confirm-ic7300-to-ic705
```

Retained-direction 40-frame campaign:

```
python scripts/sweep_modes.py --channel hf-ssb --mode-level experimental --modes hf2 --direction ab --a ic7300 --b ic705 --trials 40 --capture all --output-dir logs/mode_qualification/hf-ssb/hf2/2026-09-01-hardware/retained-40-ic7300-to-ic705
```

The three `result.json` files contain per-trial outcomes, keyed time, decoder
metrics, seed, registry IDs, Git state, and paths to compressed audio/payload
captures. Git commit was `9376a0c9583763091fcb2cf2a5bfae0adc1005b4` for all
three runs; the tree was dirty in each case.

## Results

### Smoke and confirm (3 frames)

| Trial | Payload | Confidence | CFO | Clock estimate | Carriers | Carrier SNR min/mean | Keyed | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Smoke 1 | 117 B | 0.980 | -8.155 Hz | 53.4 ppm | 19/19 | 14.46/17.94 dB | 1.757 s | decoded, CRC valid |
| Confirm 1 | 117 B | 0.979 | -8.248 Hz | 47.2 ppm | 19/19 | 14.50/17.71 dB | 1.753 s | decoded, CRC valid |
| Confirm 2 | 117 B | 0.980 | -8.114 Hz | 14.3 ppm | 19/19 | 14.82/18.15 dB | 1.755 s | decoded, CRC valid |

### Retained-direction campaign (40 frames)

All 40 trials decoded byte-for-byte with valid CRC and 19/19 present
carriers; zero `error` and zero `acquisition_failed`/`payload_failed`
outcomes. Summary statistics across the 40 trials:

| Metric | Range |
| --- | ---: |
| Confidence | 0.978-0.982 |
| CFO | -9.404 to -7.652 Hz |
| Clock offset estimate | -113.3 to 141.2 ppm |
| Carrier SNR, per-trial minimum | 5.873-17.262 dB |
| Carrier SNR, per-trial mean | 11.579-19.432 dB |
| Keyed time | 1.750-1.776 s |
| Capture peak | 0.189-0.199 |

Sweep summary line: `hf2 A:ic7300->B:ic705: 40/40 (100.0%), 95% CI
91.2-100.0%, 488 useful bit/s`. Total keyed time across the campaign was
approximately 70.1 s. Capture peaks stayed well below full scale (max
0.199), with no indication of clipping.

As with the smoke pair, the clock-offset estimate varied substantially
trial to trial (-113 to +141 ppm) while timing tracked every frame
successfully; this spread is consistent with estimator variation rather
than a real sample-clock fault, but is not distinguished from one by this
campaign. The per-trial minimum carrier SNR also varied more widely (5.9-
17.3 dB) than in the 3-frame smoke pair, which is expected variation over a
longer real-air campaign, not an anomaly -- no trial failed to decode
despite the lower end of that range.

The summaries' 487-488 bit/s use the production 107-byte DATA chunk and
measured keyed time. This is a direct-frame diagnostic, not useful
application throughput: it excludes ACKs, retries, turnaround, connection,
and disconnect. The 117-byte physical waveform payload must not be used as
an application-throughput claim.

## Qualification gates

Computed with the repository's Wilson-interval helper
(`scripts/sweep_modes.wilson_interval`, the same formula
`whale/qualification.py`'s campaigns use) against the 40-trial retained
direction result:

- Decoded: 40/40. 95% Wilson interval on the decode rate: [0.9124, 1.0000].
  95% Wilson-UB FER = 1 - 0.9124 = **0.0876**, clears the <=0.10 ceiling.
- Acquired (not `acquisition_failed`): 40/40. 95% Wilson-LB acquisition =
  **0.9124**, clears the >=0.90 floor.
- `error` outcomes: 0/40, clears the zero-error requirement.
- **The retained-direction FER/acquisition gate numerically clears at these
  40 trials.** This does **not** promote HF2. The occupied-bandwidth gate
  remains a real, open failure (see above) that independently blocks
  Optional/Default promotion, and this campaign's radio pair's filter
  passband was not verified against that gate, so a clean pass here says
  nothing about channel-plan compliance.
- Complete-system hardware Link/ARQ/recovery: unmeasured, but not a
  waveform promotion gate.
- HF2 remains Experimental only.
