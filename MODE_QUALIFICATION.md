# Waveform mode qualification

This document defines the evidence required to place a waveform in an
experimental, optional, or default Whalemodem mode registry. It applies per
channel policy: qualification on `vhf-fm` does not qualify a mode for
`hf-ssb`. The objective is a reproducible decision about verified application
delivery, acquisition, adaptation, and resource cost, not merely a successful
codec loopback or a nominal bit rate.

Qualification is scoped to one of the fixed rung contracts in
[SPEED_LADDERS.md](SPEED_LADDERS.md). A mode fills a rung only when it meets
both that rung's useful-throughput floor and its complete required channel
envelope. Every gate below that references channel conditions applies across
that target envelope. Failing outside it is expected behaviour and never a
gate failure, provided a lower rung covers those conditions.

The requirements below are cross-checked against the repository as of
2026-08-29. Items labelled **gap** are requirements for which Whalemodem does
not yet provide complete automation. They must be measured manually and
retained as an artifact; absence of that artifact is `unmeasured`, never a
pass. See [LOGS.md](LOGS.md) for where that evidence is retained and what a
qualifying artifact directory must contain -- only evidence retained under
`logs/mode_qualification/` may be cited here.

## Registry levels and status words

- **Experimental** means available only by explicit developer construction.
  It must not be advertised by a normal station policy.
- **Optional** means explicitly enabled by an operator for a named channel
  policy. It may be negotiated only when both peers advertise it.
- **Default** means included by the policy's ordinary `mode_ladder` and safe
  for the ladder to advertise without operator action. For a Level 0 rung
  this means safe to key on any channel the policy serves; for a faster rung
  it means safe for the ladder to *negotiate* when conditions warrant, not
  that the mode itself works everywhere.

`whale.mode_qualification.MANIFEST` maps `(channel policy, mode ID)` to
`experimental`, `optional`, or `default`. Registry levels are cumulative:
optional includes default modes and experimental includes both lower levels.
`whale.mode_qualification.registry()` performs the filtering; compatibility
builders remain in `whale.modes`. A normal station selects `default`, while
the `--mode-level optional` and `--mode-level experimental` server switches
are explicit operator/developer opt-ins. Tests enforce cumulative subsets,
complete declarations, and globally unique mode IDs.

The manifest initially preserves the historically shipped, provisional
default ladders recorded in the assessment below. A `default` entry is a
product-availability disposition, not proof that the evidence gates passed;
promotion evidence retains the separate status words below.

## Rung levels and operating envelopes

A mode seeking to fill a ladder target declares a **rung level** and an
**operating envelope** as part of qualification. A supplemental experimental
mode may instead declare that it fills no target rung.

The rung level is one of:

- **Level 0** -- control and fallback, optimized for maximum coverage;
- **Level 1** -- robust data;
- **Level 2** -- general-purpose data;
- **Level 3** -- fast data; or
- **Level 4** -- maximum speed inside a deliberately narrow envelope.

The minimum envelope and useful-throughput floor for each policy and level
come from `SPEED_LADDERS.md`. A qualification artifact expands that contract
into the exact named preset(s), directions, and boundary points used by the
campaign. For `vhf-fm` this includes the measured preset and RF C/N; for
`hf-ssb` it includes the named Watterson or bounded benign/static channel and
SNR in the standard 3 kHz reference bandwidth.

The target envelopes are fixed project requirements, not values inferred from
a candidate's results. A mode that misses any required point does not fill
that rung. It may be redesigned, assigned to a lower rung whose complete
contract it meets, or retained as an experimental supplemental mode with its
narrower measured envelope. Extra coverage is welcome but does not compensate
for missing the throughput floor; extra throughput does not compensate for
missing a required channel point. Widening a mode's claimed envelope beyond
the target requires evidence at every added point.

VF6 (VHF FM mode ID `6`) is the fastest declared experimental VHF mode. Its
codec, clean audio path, and bounded synthetic `flat_nbfm` channel point have
focused conformance tests. A retained 20-trial-per-point smoke sweep passes
20/20 full-capacity frames at 35 and 40 dB RF C/N and fails 20/20 at 30 dB;
this is provisional because it is below the Monte Carlo trial count and is a
synthetic recipe rather than the required measured preset. Session,
interoperability, hardware, and resource gates remain unmeasured; its manifest
entry must not be read as a promotion or application-throughput claim.

Evidence in the assessment table uses four words:

- `passed`: a retained result meets the gate exactly;
- `failed`: a retained result was run and missed the gate;
- `unmeasured`: no qualifying retained result exists;
- `provisional`: useful older or smaller evidence exists, but it does not meet
  the gate or lacks required metadata. A currently shipped mode may remain
  provisionally accepted while its evidence is brought up to this process.

## Reproducible test matrix

### 1. Unit and malformed-input tests

Every mode must have deterministic tests for:

1. zero-, representative-, and maximum-length payload round trips, plus
   refusal of an oversize payload without truncation;
2. encoded duration, sample rate, payload capacity, mode ID, registry order,
   control-mode choice, and adjacent `ModeRegistry.step()` behavior;
3. acquisition with valid leading/trailing audio and the mode's supported
   timing, frequency, clock, filter, noise, fading, clipping, and interference
   ranges;
4. rejection without an exception or unbounded work of silence, bounded white
   noise, a bare carrier, truncated audio, corrupt header/length, corrupt
   payload/CRC, impossible declared length, and non-finite or wrong-shaped
   audio where the public decoder accepts arbitrary arrays;
5. exact payload delivery only after integrity verification, and stable
   acquisition/CRC/quality diagnostic fields expected by
   `whale.qualification` and the link.

Existing mode, framing, DSP, capture-replay, and link tests support most of
this. CPFSK explicitly exercises hostile length fields and partial frames
(`test_afsk_loopback.py`); VF3, HC0, and HC1 each exercise oversize,
truncated-frame, corruption, noise, and tone rejection in their own mode
test files. Corrupt header/length and impossible declared length are
CPFSK-specific hazards -- VF3/HC0/HC1 frames are fixed length with no
declared-length field to corrupt, so those two cases do not apply to them.

`tests/test_mode_conformance.py` now runs one parameterized contract test
against every registry mode's public `decode()` for the cases that were not
already covered per mode: silence, bounded white noise, a bare carrier, and
non-finite or wrong-shaped (including empty) audio. Adding it surfaced a
real gap it closed: `afsk.demodulate` raised `ValueError` on empty or
non-1-D audio instead of reporting a clean non-decode; it now returns
`{"synced": False, "payload": None}` for both, matching VF3/HC0/HC1's
existing behavior.

**Gate:** all applicable tests pass, with no unexplained `xfail`. An expected
failure may document future performance work but cannot satisfy a promotion
gate.

### 2. Bounded CI channel regression

`tests/test_channel_regressions.py` is the bounded, fixed-seed smoke matrix.
Each registry mode must have at least one policy-appropriate point. Keep CI to
at most two full-capacity frames per `(mode, point)` and do not interpret this
as a reliability estimate. VHF uses `ComplexFmChannel` with an explicitly
named measured preset and RF C/N; HF uses an explicitly named Watterson preset
plus waveform-referenced AWGN. Any threshold relaxation requires a written
reason in the test.

**Gate:** the mode meets its checked-in deterministic minimum on every CI run.
A failure blocks all promotions until understood; passing only establishes a
regression anchor.

### 3. Frame Monte Carlo sweeps

Use `scripts/benchmark_simulated_channels.py`, full-capacity deterministic
random payloads, and a fresh output file for each model/preset. Use at least
100 independent trials per `(mode, point)` for initial qualification and at
least 300 at the two points used to claim a promotion boundary. Seeds are
derived by the tool and each direction/model must use an independent channel
instance. The point grid must bracket the edge of the target envelope: at
least two points well inside it, two near its boundary, and two outside it.
The outside points are there to locate the boundary, not to be passed. Use:

- `fm` with the policy's measured radio preset for VHF FM;
- `watterson` with the policy-relevant presets spanned by the mode's
  envelope, with explicit SNR/3 kHz;
- `awgn` as a diagnostic baseline, not as the sole qualification channel.

A **Level 0** mode's grid must include the disturbed HF preset (`hf-ssb`) or
the conservative measured preset at 5 dB RF C/N (`vhf-fm`). Levels 1 through
4 sweep every class and boundary named by their target contracts. An HF Level
4 mode is not required to sweep disturbed Watterson fading because that
condition is not in its target envelope. Its benign/static qualification
must, however, use the bounded impairment definition in `SPEED_LADDERS.md`;
an identity channel or AWGN alone remains diagnostic rather than qualifying.

At every point **inside the target envelope** the 95% Wilson upper bound on
FER must be at most 10%, the lower bound on acquisition probability at least
90%, and there must be no `error` outcomes. Results at points outside the
envelope are recorded to locate the boundary and do not affect the gate,
except that an `error` outcome -- an exception or unbounded work -- is a
failure anywhere, envelope or not.

Two asymmetric requirements make the ladder's shape explicit, one in each
direction:

- A lower rung must have a point at which it meets those limits while the
  next faster rung does not, demonstrating that it adds coverage rather than
  only overhead.
- A faster rung must have a point inside both envelopes at which it meets
  those limits and beats the rung below on useful throughput by the margin
  in section 6, demonstrating that it adds speed rather than only risk.

**Gate:** both requirements hold, and the mode meets the FER/acquisition
limits across the target rung's complete envelope. A miss at any required
point fails that rung; the target envelope is not narrowed to fit the result.

The script reports acquisition probability, FER, payload delivery, confidence
intervals, channel/decoder measurements, seeds, expanded channel descriptions,
Git state, and JSON trial records. BER is present only when a decoder exposes
bit evidence. It currently runs one logical direction per frame; symmetric
simulation is acceptable for waveform qualification, while asymmetry is
covered at session and hardware levels.

### 4. Full-stack sessions, ARQ, adaptation, and recovery

Use `scripts/benchmark_sessions.py` for connection, simultaneous
bidirectional verified transfer, ARQ, per-direction adaptation, and clean
disconnect through the selected policy. Run at least 20 trials per smoke
point and 100 per promotion point, with at least 10,000 application bytes in
each direction so that every adjacent mode transition can occur. Include:

- clean and transition points in both directions;
- an asymmetric run using `--reverse-model`/`--reverse-points`;
- lost DATA, lost ACK, corrupted DATA, and a transient transport failure;
- fallback from every faster rung and a later climb after recovery;
- connection and disconnect loss/retry cases.

The benchmark currently records connection, directional delivery, ARQ-facing
link metrics, adaptation effects, useful bytes per simulated second, channel
measurements, seeds, and Git state. Tests in `test_link_recovery.py` exercise
the individual drop/corruption cases. **Gap:** the command exposes neither
its existing frame-drop/transport-fault hooks nor a scripted recovery schedule
on the CLI, and it does not summarize time spent in each mode. The smallest
implementation is repeatable `--fault direction:phase:index:action` arguments
and per-direction mode-history fields in its JSON.

**Gate:** 100% verified bidirectional delivery and clean disconnect, with the
95% Wilson lower bound at least 95% at promotion points; every injected fault
must be observed, recovered without duplicate application bytes, and followed
by the expected fallback/re-climb. No deadlock or uncaught exception is
allowed.

### 5. Bidirectional hardware

Use `scripts/sweep_modes.py` for direct full-capacity frames through two named
radios, audio devices, PTT backends, settings, and cables. Test both directions
with at least 40 frames per direction for optional promotion and 100 per
direction on two materially different radio/audio pairs for default promotion.
Retain all failed captures and at least one successful capture per direction.
Then run the full hardware link with `scripts/run_acceptance_test.py` (and the
focused hardware recovery scripts where applicable): connect, transfer at
least 10,000 verified bytes each way, exercise ARQ and mode changes, recover
from one induced lost frame and one lost ACK, and disconnect. Do not infer the
reverse direction from one successful leg.

The frame sweep already emits the versioned trial schema, Wilson intervals,
throughput, levels, decoder metrics, radio names, registry IDs, Git commit,
and failed captures. Hardware smoke/recovery scripts exist, but their result
formats and fault controls are not unified. **Gap:** extend the hardware
acceptance runner to emit a session artifact equivalent to
`benchmark_sessions.py`, including radio/audio/PTT configuration and fault
events.

**Gate:** frame delivery satisfies the same FER/acquisition bounds as the
Monte Carlo gate in each direction, and every hardware session completes
exactly and recovers, with the hardware pair's conditions inside the mode's
target envelope. A run in which the bench was misconfigured -- a dummy load
in place of an antenna, a muted or mis-levelled audio path, the wrong
filter -- did not place the mode inside its envelope and is an **invalid
run**, recorded as `unmeasured` with the diagnosis retained, not as a
failure. Such a diagnosis must be supported by the captured metrics and, once
this document's configuration metadata is retained, by the recorded setup.
A known failed direction on a valid run is a failed gate. If the failure is
shown to be a property of conditions outside the target envelope, the run is
not evidence for or against the target; otherwise the rung fails. An
unexplained directional asymmetry is always a failure.

### 6. Useful throughput and adjacent-rung overlap

Measure one-direction bulk-transfer throughput from the first application
DATA byte becoming available until the final DATA byte is acknowledged. Use
verified application bits divided by elapsed simulated or measured channel
time. The interval includes DATA and control frames, ACKs, retries, PTT
leads/tails, and radio turnaround. Connection setup and disconnect are
reported separately and are not included in this primary throughput number.
Nominal codec rate, physical-layer payload per keyed second, and a clean frame
loopback are not application-throughput evidence.

For every HF rung, measure 99%-power occupied bandwidth over representative
and maximum-length frames, including acquisition and framing. The upper
confidence bound of the measured occupied bandwidth must not exceed 2,300 Hz;
a nominal carrier span alone is not bandwidth evidence.

At every channel point claimed for a rung, its median full-session useful
application throughput must meet the floor in `SPEED_LADDERS.md` without
reducing the section 4 completion reliability. Test at least 10,000
application bytes per direction; increase the transfer size when necessary
to keep fixed setup effects below 5% of the measured transfer interval.

For every adjacent pair, retain at least two channel points where both modes
satisfy the frame reliability gate. At each point, enabling the faster rung
must improve median full-session useful application throughput by at least
25%. The lower rung must also retain a point where it passes and the faster
rung fails, as required by section 3. A mode that is faster but misses its
level's absolute throughput floor does not fill that rung.

`benchmark_simulated_channels.py`, `benchmark_sessions.py`, and
`sweep_modes.py` calculate the needed useful-throughput quantities. **Gap:**
none produces an automatic adjacent-rung comparison report. The smallest
addition is a summary function that joins mode/point rows, checks confidence
bounds, and emits ratios and pass/fail fields.

### 7. CPU and memory

Measure on both a named development machine and the documented minimum
low-end target. Record OS, Python/NumPy/SciPy versions, CPU model/core count,
RAM, power/performance governor, sample rate, commit, command, and whether the
tree was dirty. Measure idle receive search for every default mode, worst-case
successful decode, failed acquisition, a 30-minute bidirectional session with
retries and adaptation, peak resident set size, final resident set size, and
audio overruns/dropouts.

`scripts/benchmark_rx.py` provides deterministic bounded idle-noise timing,
and the link records per-decode thread CPU. It prints text only and does not
measure memory, sustained real-time load, or dropouts. **Gap:** add JSON output
and environment metadata to `benchmark_rx.py`, plus a small `psutil`-based
session sampler (or platform RSS fallback) that records process CPU, RSS peak
and end, and transport overruns. Avoid adding a heavyweight profiler to the
runtime path.

**Gate:** on the minimum target, every decoder's p95 work for one receive
window is below the window's real-time duration, the sustained session has
zero audio overruns, peak RSS stays within 256 MiB, and end RSS grows by no
more than 16 MiB or 5% (whichever is larger) from the post-warm-up baseline.
These are project qualification limits, not currently measured guarantees.

## Required artifacts and metadata

Store promotion evidence under `logs/mode_qualification/<policy>/<mode>/<date>/`
or a reviewed `experiments/<mode>/results/` directory. A qualification index
must list every gate and link its immutable artifact. JSON artifacts must use
the existing `TrialRun` schema where applicable and otherwise a documented,
versioned schema. Retain:

- the mode's target rung level, or an explicit supplemental-mode declaration;
- the fixed target envelope, the exact campaign expansion of it, and any
  additional measured coverage claimed beyond it;
- exact command, UTC start/end, trial counts, master and derived seeds;
- mode name/ID, registry order/control ID, policy and payload size;
- Git commit and dirty state (default promotion requires a clean tree);
- Python, dependency, OS, architecture, and hardware metadata;
- complete expanded channel description and SNR reference/measurement window;
- per-trial outcome separated into acquisition, payload, and error, decoder
  metrics separate from injected channel measurements, durations and useful
  bytes;
- Wilson intervals and explicit gate calculations;
- radio, audio device, PTT backend, levels, filters, frequencies, firmware,
  direction, and operator notes for hardware runs;
- failed captures with expected payload and replay command, plus representative
  successful captures; and
- CPU/RSS samples, overruns, and warm-up definition for resource runs.

The frame tools already provide most waveform/channel fields. Dependency and
host metadata, successful hardware captures, uniform hardware session JSON,
and resource JSON are the principal missing pieces.

## Promotion decision

Promotion is monotonic in evidence, not in implementation maturity:

| Destination | Mandatory gates |
| --- | --- |
| Experimental | Unit/malformed-input suite and a bounded clean loopback pass; unique stable mode ID; declared target rung or explicit supplemental status; provisional measured envelope; decoder resource use is bounded by construction/test. |
| Optional | All experimental gates; bounded CI; frame Monte Carlo across the claimed envelope; one bidirectional radio pair; full simulated sessions and recovery; adjacent-rung value/overlap when claiming a target rung; development-host CPU/RSS. |
| Default | All optional gates; the complete target envelope and throughput floor confirmed by promotion-sized runs; 100-trial promotion session points; two bidirectional hardware pairs and hardware recovery; minimum-target CPU/RSS and no dropouts; complete clean-commit artifacts and documentation. |

Every mandatory row must be `passed`. `failed`, `unmeasured`, or `provisional`
blocks a new promotion. A regression in a default mode removes it from the
default registry unless a documented compatibility/security reason requires
a time-bounded exception. Mode IDs are never reused after on-air publication.

## Assessment of the current modes

This is a documentary audit of checked-in tests, capture files, and result
notes, not a new statistical or real-radio run. “CPFSK” covers the 300, 600,
and 1200 baud VHF rungs; where the evidence differs, the weakest rung controls
the group status. The per-mode envelope rows below record historical/current
measurements. They do not override the fixed targets, and a `passed` result in
those rows does not claim that the mode fills a target rung; that separate
status is explicitly `unmeasured` below.

| Requirement | CPFSK | VF3 | HC0 | HC1 |
| --- | --- | --- | --- | --- | --- |
| Current registry position (not target-rung qualification) | three ordered VHF modes | fastest ordinary VHF mode | HF control mode | fastest ordinary HF mode |
| Declared operating envelope | `vhf_bench_conservative`, 5 dB RF C/N and above (all three rungs) | `vhf_bench_conservative`, 10 dB RF C/N and above | all three mid-latitude Watterson presets including disturbed, across the tested SNR range | Watterson quiet above -5 dB and moderate at 10 dB and above; disturbed excluded |
| Unit, framing, malformed input | passed | passed | provisional | provisional |
| Bounded policy channel CI | passed | passed | passed | passed |
| Historical frame Monte Carlo at measured points | passed (2026-08-29 clean-tree rerun after the channel-drain fix; 300 and 600 baud delivered 600/600 and 598/600, 1200 baud delivered 591/600, all within the FER/acquisition gate) | passed within the measured envelope (2026-08-30 clean-tree run; 99-100/100 at every point from 10 dB RF C/N up). 0/100 at 5 dB was root-caused to a genuine carrier-SNR floor -- see campaign notes below | passed (2026-08-30 clean-tree run; 100/100 at every SNR point across quiet, moderate, and disturbed Watterson presets) | passed within the measured envelope (2026-08-30 clean-tree run; clears quiet above -5 dB and moderate at 10 dB+); fails disturbed at every tested SNR |
| Full-stack connection and bidirectional ARQ | passed | passed | passed | passed |
| Scripted adaptation/fault recovery artifact | provisional | provisional | provisional | provisional |
| Bidirectional hardware frame gate | unmeasured | provisional (6/6, 3 each way) | provisional (2026-08-28 captures, 11/11 and 5/5 each way, below trial minimum) | unmeasured (2026-08-28 captures, 17/17 one direction, 0/3 the other; the weak leg is attributed to the IC-705 transmitting into a dummy load, so the session does not measure HC1's envelope and the 0/3 is an invalid run rather than a failed direction -- see `logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-28-hardware/INDEX.md`. Re-run required with both stations on antennas and the transmit configuration recorded) |
| Full hardware link/recovery gate | provisional | unmeasured | unmeasured | unmeasured |
| Useful throughput and adjacent overlap | provisional | provisional | provisional | unmeasured |
| Development CPU and RSS | unmeasured | unmeasured | unmeasured | unmeasured |
| Minimum-target CPU, RSS, and dropouts | unmeasured | unmeasured | unmeasured | unmeasured |
| Complete promotion artifact/metadata | unmeasured | provisional | provisional | provisional |
| Fixed target-rung throughput and envelope | unmeasured | unmeasured | unmeasured | unmeasured |
| Present registry disposition | provisional default VHF | provisional default VHF | provisional default HF control | provisional default HF fast rung |

The full-stack passes come from `tests/test_audio_e2e.py`, which exercises the
ordinary VHF ladder through VF3 and the HF ladder from HC0 through HC1 with
bidirectional payloads, negotiation, ARQ, adaptation, and disconnect. Recovery
behaviors also have focused tests, but not yet the unified promotion artifact
required above. The bounded CI points are exactly those in
`tests/test_channel_regressions.py` and make no statistical claim.

VF3 has the strongest retained direct-radio result:
`experiments/vf3/RESULTS.md` and
`experiments/vf3/results/final_dqpsk_both_3.json` report six exact
full-capacity frames and offline replay, but six trials do not meet the new
minimum. HC0 and HC1's saved captures are retained at
`logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-28-hardware/` (see that
directory's `INDEX.md`): HC0 has exact saved-capture replay in both
directions (11/11 and 5/5), including the weak HF leg, though its largest
single batch (6 trials) is below the 100-trial minimum. HC1's saved captures
establish one useful direction at 17/17 and its frequency-offset handling,
while the reverse leg failed all 3 attempted trials at acquisition, detecting
no carriers at all. That leg is attributed to the IC-705 transmitting into a
dummy load rather than an antenna: HC0 decoded 11/11 over the same leg in the
same session but with visibly degraded metrics in that direction only
(confidence 0.38-0.42 and nonzero raw BER, against 0.49-0.50 and zero BER the
other way), which is the signature of a large one-directional path loss that
HC0's margin absorbs and HC1 cannot. The attribution rests on operator
recollection because no antenna or power configuration was retained with the
run -- precisely the metadata this document now requires. The session
therefore does not measure HC1's declared envelope, and its hardware frame
gate is `unmeasured` pending a re-run with both stations on antennas, rather
than a failed direction. HC1 remains a provisional fast rung with HC0 as its
control/fallback mode.

The first retained CPFSK campaign is
`logs/mode_qualification/vhf-fm/cpfsk/2026-08-29/fm_frame_monte_carlo.json`.
It used the `vhf_bench_conservative` FM preset, 100 independent trials at
each of 5, 10, 15, 20, 25, and 30 dB RF C/N, and full-capacity payloads.
The 300-baud rung delivered 600/600 frames and the 600-baud rung delivered
598/600. The 1200-baud rung delivered only 454/600 and missed the FER gate at
every point (70--85 deliveries per 100); all captures acquired and there
were no exception outcomes. The non-monotonic failure at high C/N required
investigation rather than a boundary follow-up run; the diagnostics below now
identify a finite channel-buffer tail artifact rather than the formerly
suspected marginal 2200 Hz measured-response placement. The artifact was
produced from commit `3946cbd6f84a34347f379382d011cfbfd0178861` with a
dirty tree containing the qualification-manifest implementation, so it is
retained initial evidence but cannot support default promotion.

That simulated failure does not erase the earlier bench result. Mode 2 at
1200/2200 Hz was selected by real-radio baud/placement sweeps that operated in
both directions, and the historical payload sweep delivered every tested
frame through 255 DATA bytes in both directions. Those runs established useful
operation on this IC-705/KG-UV9D pair, but they predate this qualification
process and do not constitute its minimum-trial hardware artifact. In
particular, the current 402-byte DATA chunk (412 encoded bytes after the air
header) still needs a retained bidirectional hardware qualification run.

### 2026-08-29 mode-2 FM diagnostics

The 18 artifacts under
`logs/mode_qualification/vhf-fm/cpfsk/2026-08-29/diagnostics` used mode 2,
master seed `20260829`, the three requested presets, RF C/N 10/20/30 dB, 20
trials per point, and requested DATA sizes 88, 193, 255, 300, 350, and 402
bytes. The complete encoded sizes were respectively 98, 203, 265, 310, 360,
and 412 bytes. These small runs are diagnostic only and are not promotion
evidence.

Each cell below is exact frame deliveries at 10/20/30 dB RF C/N:

| DATA bytes | IC-705 to KG-UV9D | KG-UV9D to IC-705 | Conservative combined |
| ---: | --- | --- | --- |
| 88 | 14/20, 16/20, 15/20 | 13/20, 18/20, 17/20 | 14/20, 16/20, 15/20 |
| 193 | 16/20, 18/20, 16/20 | 14/20, 18/20, 17/20 | 16/20, 18/20, 16/20 |
| 255 | 14/20, 16/20, 15/20 | 14/20, 17/20, 16/20 | 14/20, 16/20, 15/20 |
| 300 | 14/20, 14/20, 15/20 | 13/20, 15/20, 17/20 | 14/20, 14/20, 15/20 |
| 350 | 18/20, 15/20, 15/20 | 17/20, 17/20, 17/20 | 18/20, 15/20, 15/20 |
| 402 | 17/20, 16/20, 16/20 | 17/20, 18/20, 16/20 | 17/20, 16/20, 16/20 |

All 1,080 frames acquired; 851 delivered and all 229 failures were
payload/CRC failures, with no acquisition or exception outcomes. Increasing
C/N did not produce a reliable monotonic improvement: aggregate delivery was
274/360 at 10 dB, 293/360 at 20 dB, and 284/360 at 30 dB. Failure probability
also did not grow with DATA length: aggregate deliveries by increasing size
were 138, 149, 137, 131, 147, and 149 out of 180. The conservative preset was
trial-for-trial identical in outcome to the IC-705-to-KG-UV9D preset (280/360 for
each) and only modestly below the reverse measured direction (291/360), so it
was not materially worse than both directional models.

The decoder evidence identifies the discrepancy. Every delivered frame had
zero hard-decision errors. Every failed frame had exactly one error, no
missing bits, and that error was the final body-CRC bit (positions 1086, 1926,
2422, 2782, 3182, or 3598 from the sync start as size increased). No failure
occurred when that expected terminal bit was one. `ComplexFmChannel.process()`
returns a block the same length as its input, while its causal measured audio
filter retains state; the direct-frame runner supplied no post-frame audio
through the channel and appended downsampler padding only afterward. A
targeted replay of one known failure for each preset changed all three to an
exact decode when 10 ms of post-frame audio was processed through the FM
channel. This is evidence of a shared simulated-channel/frame-boundary problem,
not a conservative-preset problem, a direction-specific problem, payload
length sensitivity, or random RF-noise weakness in the waveform.

The reusable channel-drain/tail contract is now implemented at the
direct-frame boundary without changing the on-air waveform or decoder. The
runner drains the same channel before receive decimation, and fixed-seed
replays of a previously failing frame decode exactly for all three FM presets.
A promotion-sized rerun of this matrix is still required; these focused
replays do not pass the Monte Carlo gate. A retained bidirectional hardware
run at the current 402-byte DATA
capacity is still required for formal qualification. Mode 2 remains in its
existing registry disposition during this investigation.

Regression replay on 2026-08-29 used master seed `20260829`, point index 0,
trial 1 (derived seed `5957953403229853794`), 10 dB RF C/N, and the 412-byte
physical payload. The command
`python -m pytest tests/test_channel_regressions.py -q` replayed that formerly
failing terminal-zero frame for all three presets, plus terminal-one trial 2;
all six cases decoded exactly. The complete focused file also includes the
ordinary VHF/HF fixed-seed regressions.

A promotion-sized rerun of the same campaign (`fm_frame_monte_carlo_rerun.json`,
also 2026-08-29, commit `c553b94`, clean tree) confirms the fix at scale: all
three CPFSK rungs now clear the Wilson-bound FER/acquisition gate at every RF
C/N point, including the previously failing 1200-baud rung (591/600 overall,
weakest point 91/100 at 5 dB). See
`logs/mode_qualification/vhf-fm/cpfsk/2026-08-29/INDEX.md` for the full
point-by-point breakdown. This clears the "Qualified frame Monte Carlo" gate
for the CPFSK group in the assessment table above; the remaining gaps for
default promotion are the hardware, throughput-overlap, and resource rows.

### 2026-08-30 VF3, HC0, and HC1 frame Monte Carlo campaigns

With the CPFSK campaign complete, the same promotion-sized methodology was
run against the three modes still `unmeasured` on this gate: VF3 over the
`fm` model (`vhf_bench_conservative` preset, matching CPFSK), and HC0/HC1
over the `watterson` model across all three standard mid-latitude presets
(quiet/moderate/disturbed). All three artifacts are from commit `c553b94`
with a clean tree. See
`logs/mode_qualification/vhf-fm/vf3/2026-08-30/INDEX.md` and
`logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-30/INDEX.md` for full
point-by-point results and commands.

VF3 delivered 0/100 at 5 dB RF C/N -- every trial was classified as an
acquisition failure by `ACQUISITION_THRESHOLD` (0.70) -- then cleared the
gate at every point from 10 dB up (99-100/100). This is a hard cliff rather
than a gradual FER slope, and it fixes the lower edge of VF3's operating
envelope at 10 dB RF C/N under `vhf_bench_conservative`.

This was root-caused on 2026-08-30 by replaying trial seeds from the same
campaign directly against `whale/modes/vf3.py` with `ACQUISITION_THRESHOLD`
forced to 0.0. Acquisition itself is not failing: `start_index` lands within
a few samples of the same offset on every trial, and all 58 header carriers
are present after equalization. Every forced-through frame still fails CRC,
so the 0.70 gate is correctly rejecting frames that are not decodable, not
misclassifying good ones. The measured per-carrier SNR at this point averages
about 4.7 dB with a minimum around -1 dB across VF3's 58 subcarriers --
insufficient margin for differential OFDM decode, unlike CPFSK's single
wideband tone, which cleared 91/100 at the same 5 dB RF C/N point in the
2026-08-29 campaign. This is a genuine carrier-SNR floor intrinsic to VF3's
58-carrier design under this preset, not an acquisition bug, a threshold
miscalibration, or a channel-drain artifact; no code change is indicated.
The finding is the same shape as HC1's disturbed-preset result below: a fast
current mode with a real operating-range boundary that a more robust mode (CPFSK
here) is expected to cover. VF3's declared envelope therefore starts at
10 dB RF C/N under `vhf_bench_conservative`. This explains its place in the
current registry, but does not assign it to one of the fixed target rungs in
`SPEED_LADDERS.md`.

HC0 passed cleanly at 100/100 across every SNR point in every preset,
including disturbed, clearing the gate outright.

HC1 passed in quiet conditions above -5 dB and in moderate conditions from
10 dB up, but never cleared the gate under the disturbed preset (CCIR Poor:
2 ms delay spread, 1.0 Hz Doppler spread), topping out at 61/100 even at
20 dB. This was investigated as a possible repeat of the CPFSK channel-drain
artifact and ruled out: every disturbed-preset trial from 10-20 dB acquired
successfully, and all failures were payload/CRC mismatches correlated with
roughly double the sub-0 dB subcarriers (2.6 vs 1.2 of 19) in failed frames
versus decoded ones at the same SNR point. This is frequency-selective fading
against HC1's 93.75 Hz carrier spacing, not a harness or thermal-noise
problem, and it does not respond to added SNR -- consistent with HC0's
`whale/modes/hc0.py` redundancy margin passing the identical trials at
100/100. HC1's current fast-mode disposition assumes HC0 is the fallback for
conditions it cannot cover, and the disturbed preset is accordingly excluded
from HC1's measured envelope. This records current behavior; it does not
qualify HC1 for a fixed target rung.

No qualifying adjacent-rung overlap report, target-rung speed measurement, or
CPU/RSS artifact has yet been retained for any production mode. CPFSK and
HC0 pass the Monte Carlo gate outright; VF3 and HC1 pass it within the
declared envelopes recorded above. Existing default placement therefore
records historical/provisional acceptance rather than retroactively
declaring the new gates passed.

`experiments/hc2_32qam/` is not a declared mode and therefore has no manifest
entry or mode ID. It now has a deterministic clean-channel oracle round trip,
a real acquisition/CFO/equalization/phase-tracking receiver, and two
2026-08-30 AWGN frame sweeps (`experiments/hc2_32qam/RESULTS.md`): 7,800
full-capacity trials against the original receiver and 8,300 against the
current one, both waveform-referenced with Wilson intervals and per-frame EVM.
The first sweep found that every failure above 12.5 dB was one acquisition
defect -- two identical training symbols giving the matched filter a near-tied
second peak -- and the second sweep re-measured the mode after the training
symbols were made distinct. None of that is qualification evidence. The AWGN
sweep is a diagnostic baseline, which section 3 explicitly does not accept as a
qualification channel; the artifacts are scratch, produced from a dirty tree
with the experiment package untracked, and no `logs/mode_qualification/`
campaign directory exists for HC2. Hardware evidence, negotiation, and
application-throughput work all remain prerequisites before it can enter this
process. As a high-rate HF candidate it would be evaluated against the fixed
Level 4 throughput and benign/static envelope, rather than against
disturbed-preset robustness. The AWGN result is
consistent with that shape -- with the fix in place, delivery collapses below
about 20.5 dB SNR/3 kHz (11.5 dB under the retired full-Nyquist convention),
realized payload exceeds the 7,050 bit/s reference
row from 11.5 dB up, and frame error rate reaches 1e-2 by 13 dB (superseding
the 12 dB / 12.5 dB / 16 dB figures the first sweep produced) -- and a
2026-08-30 Watterson sweep now supplies the fading evidence a declared
envelope needs. That evidence is unfavourable and is the more important
result: over 10,400 fading trials HC2 delivered 0 of 300 frames against the
`mid_latitude_quiet` preset at every SNR from 11.5 dB to 40 dB, and
parametrically it requires better than roughly 0.1 ms of differential delay
and 0.005 Hz of frequency spread, with delay binding first well inside the
cyclic prefix. Its measured envelope is thus narrower than the mildest
standard Watterson preset. That is useful boundary evidence, but only the
complete Level 4 contract determines whether it can fill the target rung.

Two further results from that sweep bear on any future promotion. Frame
integrity held completely -- not one corrupt frame passed CRC32 in 10,400
fading trials -- but the EVM health metric the AWGN campaign proposed as a
fallback trigger caught only 45 to 71 percent of failures in the
delay-dominated regime, so a demotion policy built on it would sit at high
frame error rates believing the link was healthy. Section 3's requirement for
fallback from every faster rung should therefore be read as needing decode
outcome, not EVM, as the primary demotion signal.
