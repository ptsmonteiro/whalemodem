# HC1 versus HF2 paired comparison, 2026-09-07

## Verdict

HC1 and HF2 do **not** form a clean pair of distinct default ladder rungs.
HF2 has the wider useful fading envelope and higher estimated stop-and-wait
goodput; HC1 has higher data-frame-only net rate but loses that advantage as
soon as one current minimum HR0 ACK is charged. In moderate fading HF2 first
passes the confidence-qualified gate at 15 dB SNR/3 kHz, while HC1 does not
pass until 24 dB. Neither works in disturbed fading even at 36 dB.

Do not change the registry from this direct-frame campaign. In performance
terms a direct HC0 -> HF2-like step is preferable to retaining HC1 as a
supposedly more robust rung. Deployment cannot make that switch today because
HF2 still measures about 4.21 kHz occupied bandwidth against the 2.3 kHz HF
ceiling, and the pair lacks session-level adaptation evidence. Retain the
current product state temporarily, develop or repair a compliant intermediate
waveform, and keep HF4 as the maximum-speed mode.

## Method

The harness calls the production `HC1` and `HF2` `WaveformMode` adapters with
full link-capacity DATA payloads: 74 physical bytes / 64 application bytes for
HC1 and 117 / 107 for HF2. Encoder output includes the 128 ms common minimum
lead and 20 ms tail. Every encoded frame is scalar-normalized to 0.128 RMS at
the front of the channel; observed gain was 0.9887-1.0153 for HC1 and
0.9669-1.0170 for HF2. The 0.95 clip stage clipped zero samples.

Each path then applies, in order: sixth-order 250-3,100 Hz TX filter; 0.95
hard-clip limit; +8 Hz offset with 0.05 Hz/s drift; two equal-power Watterson
paths; AWGN; sixth-order 250-3,100 Hz RX filter; and +20 ppm receive-clock
error. Conditions differ only in Watterson delay and 2-sigma Doppler spread:

| Condition | Differential delay | Doppler spread |
| --- | ---: | ---: |
| static/benign | 0.1 ms | 0.005 Hz |
| quiet | 0.5 ms | 0.1 Hz |
| moderate | 1.0 ms | 0.5 Hz |
| disturbed | 2.0 ms | 1.0 Hz |

Injected SNR is signal power divided by noise power in a 3 kHz reference
bandwidth. No historical `waveform_snr_db` result enters this comparison.
Decoder `carrier_snr_db` remains diagnostic only. A mode-independent
`SeedSequence([master_seed, condition_index, point_index, trial])` supplies the
same initial Watterson oscillator frequencies/phases and AWGN sequence to both
modes. The unequal frame lengths mean HF2 observes a longer suffix of the same
realization. Payload bytes are deterministic from that seed; HC1's payload is
the prefix of HF2's payload.

An exploratory 20-trial sweep covered every requested initial-grid point but
is deliberately not retained here as qualification evidence. These artifacts
contain 300 independent trials at every reported point. Gate: FER 95% Wilson
upper bound <=10%, acquisition 95% Wilson lower bound >=90%, and zero
exceptions. `unresolved` means the interval straddles a gate.

## Reliability results

Cells are `successes/300 [95% Wilson CI]; acquisition/300 [95% Wilson CI]; verdict`.

| Condition | SNR dB/3 kHz | HC1 | HF2 |
| --- | ---: | --- | --- |
| static | 3 | 2 [0.2-2.4%]; 211 [64.9-75.2%]; fail | 0 [0.0-1.3%]; 219 [67.7-77.7%]; fail |
| static | 6 | 263 [83.5-90.9%]; 300 [98.7-100%]; unresolved | 229 [71.2-80.8%]; 300 [98.7-100%]; fail |
| static | 7.5 | 291 [94.4-98.4%]; 300 [98.7-100%]; pass | 287 [92.7-97.5%]; 300 [98.7-100%]; pass |
| static | 9 | 291 [94.4-98.4%]; 300 [98.7-100%]; pass | 297 [97.1-99.7%]; 300 [98.7-100%]; pass |
| quiet | 6 | 191 [58.1-68.9%]; 300 [98.7-100%]; fail | 190 [57.7-68.6%]; 297 [97.1-99.7%]; fail |
| quiet | 9 | 289 [93.6-97.9%]; 300 [98.7-100%]; pass | 293 [95.3-98.9%]; 300 [98.7-100%]; pass |
| quiet | 12 | 298 [97.6-99.8%]; 300 [98.7-100%]; pass | 299 [98.1-99.9%]; 300 [98.7-100%]; pass |
| moderate | 9 | 188 [57.1-68.0%]; 300 [98.7-100%]; fail | 206 [63.2-73.7%]; 293 [95.3-98.9%]; fail |
| moderate | 12 | 244 [76.5-85.3%]; 300 [98.7-100%]; fail | 270 [86.1-92.9%]; 300 [98.7-100%]; unresolved |
| moderate | 15 | 269 [85.7-92.6%]; 300 [98.7-100%]; unresolved | 285 [91.9-97.0%]; 300 [98.7-100%]; pass |
| moderate | 18 | 276 [88.4-94.6%]; 300 [98.7-100%]; unresolved | 296 [96.6-99.5%]; 300 [98.7-100%]; pass |
| moderate | 21 | 279 [89.5-95.4%]; 300 [98.7-100%]; unresolved | 297 [97.1-99.7%]; 300 [98.7-100%]; pass |
| moderate | 24 | 285 [91.9-97.0%]; 300 [98.7-100%]; pass | 297 [97.1-99.7%]; 300 [98.7-100%]; pass |
| moderate | 30 | 290 [94.0-98.2%]; 300 [98.7-100%]; pass | 294 [95.7-99.1%]; 300 [98.7-100%]; pass |
| disturbed | 18 | 140 [41.1-52.3%]; 300 [98.7-100%]; fail | 127 [36.9-48.0%]; 298 [97.6-99.8%]; fail |
| disturbed | 27 | 141 [41.4-52.7%]; 300 [98.7-100%]; fail | 173 [52.0-63.1%]; 299 [98.1-99.9%]; fail |
| disturbed | 36 | 149 [44.1-55.3%]; 300 [98.7-100%]; fail | 175 [52.7-63.8%]; 300 [98.7-100%]; fail |

Confirmed/pass brackets are therefore:

- static: HF2 `(6, 7.5]` dB; HC1 first passes at 7.5 dB, but its 6 dB point
  is unresolved, so only the wider confirmed-fail/pass bracket `(3, 7.5]`
  can be claimed;
- quiet: both `(6, 9]` dB;
- moderate: HF2 `(9, 15]` dB with 12 dB unresolved; HC1 `(12, 24]` dB with
  15, 18, and 21 dB unresolved;
- disturbed: no boundary through 36 dB. Both have a confirmed error floor.

## Throughput and failure mechanism

Nominal full-frame rates from actual encoder sample counts are 660.9 bit/s
for HC1 (512 bits / 0.774667 s) and 534.6 bit/s for HF2 (856 bits / 1.601333
s). The current minimum 12-byte HR0 DATA_ACK is 1.812 s. With perfect DATA
delivery, one ACK after each DATA frame gives 197.9 bit/s HC1 versus 250.8
bit/s HF2.

Per-point entries below are `delivered DATA-frame goodput / one-ACK
stop-and-wait estimate`, in bit/s:

| Condition | SNR | HC1 | HF2 |
| --- | ---: | ---: | ---: |
| static | 3 | 4.4 / 4.3 | 0.0 / 0.0 |
| static | 6 | 579.4 / 189.9 | 408.0 / 218.9 |
| static | 7.5 | 641.1 / 196.1 | 511.4 / 245.6 |
| static | 9 | 641.1 / 196.1 | 529.2 / 249.6 |
| quiet | 6 | 420.8 / 169.0 | 338.6 / 197.2 |
| quiet | 9 | 636.7 / 195.7 | 522.1 / 248.0 |
| quiet | 12 | 656.5 / 197.5 | 532.8 / 250.4 |
| moderate | 9 | 414.2 / 168.0 | 367.1 / 206.6 |
| moderate | 12 | 537.6 / 185.2 | 481.1 / 238.4 |
| moderate | 15 | 592.6 / 191.3 | 507.8 / 244.7 |
| moderate | 18 | 608.1 / 192.9 | 527.4 / 249.2 |
| moderate | 21 | 614.7 / 193.6 | 529.2 / 249.6 |
| moderate | 24 | 627.9 / 194.9 | 529.2 / 249.6 |
| moderate | 30 | 638.9 / 195.9 | 523.9 / 248.4 |
| disturbed | 18 | 308.4 / 147.5 | 226.3 / 153.0 |
| disturbed | 27 | 310.6 / 148.0 | 308.3 / 186.5 |
| disturbed | 36 | 328.3 / 151.9 | 311.8 / 187.8 |

Delivered-frame goodput charges DATA airtime only and HC1 wins every sampled
nonzero point because of its higher nominal rate. The stop-and-wait estimate
adds one minimum HR0 ACK only after a successful frame; it excludes radio
turnaround, propagation, retries, ACK airtime after failed DATA, and ACK
losses. HF2 wins that metric at every meaningful sampled point because 107
bytes amortize the fixed ACK much better than HC1's 64 bytes.

Across 5,100 trials per mode, HC1 had 3,886 decodes, 89 acquisition failures,
994 CRC failures, 131 other checked-payload failures, zero FEC-tail failures,
zero incomplete captures, and zero exceptions. HF2 had 4,015 decodes, 94
acquisition failures, 828 CRC failures, 163 other checked-payload failures,
zero FEC-tail failures, zero incomplete captures, and zero exceptions.
Near and above transitions acquisition was normally 300/300; disturbed
failure remained roughly flat while SNR rose 18 dB. This is a persistent
frequency-selective-notch floor, not thermal noise.

HF2's coherent 16-QAM shows at most a small benign-channel cost: both first
pass at 7.5 dB, while HF2 alone is a confirmed failure at 6 dB. No distinct
multi-dB coherent penalty is established. Its 2-3-way frequency diversity is
material under 1 ms/0.5 Hz moderate fading: HF2 passes at 15 dB, nine dB
below HC1's first confirmed pass. Diversity improves but does not cure the
2 ms/1 Hz disturbed floor.

## Ladder recommendation and provisional adaptation

The present HC1 -> HF2 ordering is not monotone under either project metric:
HC1 is faster by the official per-DATA-frame definition, while HF2 is more
robust in moderate fading and faster in the link's ACK-dominated stop-and-wait
operation. Keeping both as adjacent generic rungs therefore lacks a clear
adaptation objective.

Provisional waveform-level policy, pending session validation:

- do not use either mode in disturbed conditions;
- use HF2 at >=15 dB in moderate fading; do not select HC1 below its first
  confirmed pass at 24 dB;
- at >=24 dB moderate, prefer HF2 for ordinary stop-and-wait transfers and
  HC1 only for a future batched/no-ACK-per-frame service that can realize its
  data-only rate;
- in quiet/static conditions, both pass by 9/7.5 dB respectively; prefer HF2
  for ordinary transfers, with HC1 offering no demonstrated robustness rung.

These are not session-qualified switch thresholds. Before changing the
default ladder, run bidirectional sessions across the boundary regions with
turnaround, propagation, retries, ACK loss, adaptation hysteresis, unequal
paths, and message-size distributions; qualify a spectrally compliant HF2 or
replacement intermediate; and retain hardware/channel-plan evidence. HF4
remains the maximum-speed rung and was not changed or compared here.

## Commands and artifacts

Commit recorded in every JSON: `0e5e3a5b656a97aa2a7c1a3b57457da07b11cb68`.
The tree was dirty before this task; each artifact retains the full porcelain
listing and mode-source hashes. No historical artifact was overwritten.

```text
/tmp/whalemodem-hc1-hf2-venv/bin/python -m scripts.compare_hc1_hf2 --condition static --points 3 6 9 --trials 300 --workers 4 --seed 20260907 --out logs/mode_qualification/hf-ssb/hc1-hf2/2026-09-07/static.json
/tmp/whalemodem-hc1-hf2-venv/bin/python -m scripts.compare_hc1_hf2 --condition quiet --points 6 9 12 --trials 300 --workers 4 --seed 20260907 --out logs/mode_qualification/hf-ssb/hc1-hf2/2026-09-07/quiet.json
/tmp/whalemodem-hc1-hf2-venv/bin/python -m scripts.compare_hc1_hf2 --condition moderate --points 12 15 18 24 30 --trials 300 --workers 4 --seed 20260907 --out logs/mode_qualification/hf-ssb/hc1-hf2/2026-09-07/moderate.json
/tmp/whalemodem-hc1-hf2-venv/bin/python -m scripts.compare_hc1_hf2 --condition disturbed --points 18 27 36 --trials 300 --workers 4 --seed 20260907 --out logs/mode_qualification/hf-ssb/hc1-hf2/2026-09-07/disturbed.json
/tmp/whalemodem-hc1-hf2-venv/bin/python -m scripts.compare_hc1_hf2 --condition static --points 7.5 --trials 300 --workers 6 --seed 20260908 --out logs/mode_qualification/hf-ssb/hc1-hf2/2026-09-07/static-refine.json
/tmp/whalemodem-hc1-hf2-venv/bin/python -m scripts.compare_hc1_hf2 --condition moderate --points 9 21 --trials 300 --workers 6 --seed 20260908 --out logs/mode_qualification/hf-ssb/hc1-hf2/2026-09-07/moderate-refine.json
```

Raw artifacts: `static.json`, `static-refine.json`, `quiet.json`,
`moderate.json`, `moderate-refine.json`, and `disturbed.json` in this directory.

Verification:

```text
/tmp/whalemodem-hc1-hf2-venv/bin/python -m pytest -q tests/test_hc1_hf2_comparison.py tests/test_hc1_mode.py experiments/hf2/test_hf2.py tests/test_channel_regressions.py tests/test_watterson_channel.py tests/test_channel_contract.py tests/test_mode_qualification.py
# 95 passed.

/tmp/whalemodem-hc1-hf2-venv/bin/python -m pytest -q tests/test_audio_e2e.py
# Run outside the localhost socket sandbox: 8 passed.

git diff --check
/tmp/whalemodem-hc1-hf2-venv/bin/python -m compileall -q scripts/compare_hc1_hf2.py whale/trials.py whale/modes/hf2_mode.py
```

Artifact audit: 10,200 trials, only 74-byte HC1 and 117-byte HF2 physical
payloads, only `snr_3khz_db` injected-SNR labels, target RMS 0.128 throughout,
zero incomplete captures, zero exceptions, and zero silently overwritten
historical files.
