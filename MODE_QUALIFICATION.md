# Waveform mode qualification

This document defines the evidence required to place a waveform in an
experimental, optional, or default Whalemodem mode registry. It applies per
channel policy: qualification on `vhf-fm` does not qualify a mode for
`hf-ssb`. The objective is a reproducible decision about verified application
delivery, acquisition, adaptation, and resource cost, not merely a successful
codec loopback or a nominal bit rate.

Qualification is scoped to a mode's rung role and declared operating
envelope, per [GOALS.md](GOALS.md). A level-0 control/fallback mode is
qualified against the worst conditions its policy claims to serve; a top
rung is qualified against peak useful throughput on a good channel and is
expected to fail outside a narrow envelope. Every gate below that references
channel conditions applies **inside the mode's declared envelope only**.
Failing outside it is expected behaviour and never a gate failure, provided
a lower rung covers those conditions and the link demonstrably falls back.

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
  for the ladder to advertise without operator action. For a level-0 rung
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

## Rung roles and operating envelopes

Each mode declares, as part of qualification, a **rung role** and an
**operating envelope**.

The rung role is one of:

- **level-0** -- the policy's control and fallback mode (HC0 on `hf-ssb`,
  the lowest CPFSK rung on `vhf-fm`). Optimized for maximum robustness.
- **intermediate** -- covers the gap between its neighbours.
- **top** -- the policy's fastest rung. Optimized for maximum useful
  application throughput on a good channel.

The operating envelope is the explicit set of channel conditions the mode
claims to deliver on: for `vhf-fm`, the named preset(s) and the minimum RF
C/N; for `hf-ssb`, the named Watterson preset(s) and the minimum waveform
SNR in each. It is retained with the mode's qualification artifacts and is
the scope against which every channel-conditioned gate below is evaluated.

An envelope is a design statement, not a wish. Narrowing it in response to a
measured result is a legitimate, expected outcome for an intermediate or top
rung, and is preferred over trading away peak rate for margin. Narrowing a
**level-0** envelope is not: a level-0 rung that cannot cover its policy's
worst claimed conditions is a genuine failure, because nothing below it
provides coverage. Widening an envelope requires new evidence at the added
points.

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
instance. The point grid must bracket the edge of the declared envelope: at
least two points well inside it, two near its boundary, and two outside it.
The outside points are there to locate the boundary, not to be passed. Use:

- `fm` with the policy's measured radio preset for VHF FM;
- `watterson` with the policy-relevant presets spanned by the mode's
  envelope, with explicit waveform SNR;
- `awgn` as a diagnostic baseline, not as the sole qualification channel.

A **level-0** mode's envelope must span the worst conditions its policy
claims to serve, and its grid must therefore include the disturbed HF preset
(`hf-ssb`) or the conservative measured preset at the policy's minimum RF
C/N (`vhf-fm`). Intermediate and top rungs sweep only the presets their own
envelopes claim; a top rung is not required to sweep disturbed HF at all,
beyond enough points to show where its boundary lies.

At every point **inside the declared envelope** the 95% Wilson upper bound on
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
limits across its declared envelope. A mode that misses the limits inside
its envelope may either be fixed or have its envelope narrowed to the
measured boundary and re-run -- except a level-0 mode, whose envelope is
fixed by its policy and which must therefore be fixed.

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
declared envelope. A run in which the bench was misconfigured -- a dummy load
in place of an antenna, a muted or mis-levelled audio path, the wrong
filter -- did not place the mode inside its envelope and is an **invalid
run**, recorded as `unmeasured` with the diagnosis retained, not as a
failure. Such a diagnosis must be supported by the captured metrics and, once
this document's configuration metadata is retained, by the recorded setup.
A known failed direction on a valid run is a failed gate for a level-0
mode unconditionally. For an intermediate or top rung it is a failed gate
unless the failure is shown to be a property of that radio pair's conditions
rather than of the direction -- in which case the envelope is narrowed to
exclude those conditions and the ladder must be shown to fall back on that
leg. An unexplained directional asymmetry is always a failure.

### 6. Useful throughput and adjacent-rung overlap

Use decoded application bytes divided by total keyed/channel time, including
control frames, ACKs, retries, turnaround, and adaptive head/tail. Nominal
codec rate is not evidence. For every adjacent pair, retain at least two
channel points where both modes satisfy the frame reliability gate. At each
of those points the faster mode's frame useful throughput must exceed the
lower mode's by 10% or more. In full sessions, enabling the faster rung must
improve median useful application throughput by at least 5% without reducing
completion reliability. The lower rung must also retain the distinct robust
point required above.

These margins are a floor for a rung's existence, not a target. The **top**
rung of each policy carries an additional speed gate: on a clean channel
inside its envelope it must reach at least 60% of its policy's GOALS.md
reference peak -- 7,050 bit/s useful application throughput in a 2300 Hz HF
channel, 12,750 bit/s in VARA FM Narrow -- measured as useful application
bytes per keyed second in a full session. A top rung that clears every
reliability gate but does not approach the reference rate has not met its
purpose, and closing that gap takes priority over widening its envelope.
Where no existing rung reaches the threshold, the fastest one holds the top
role provisionally and the shortfall is recorded as a `failed` speed row
rather than being redefined away.

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

- the mode's declared rung role and operating envelope, and any change to
  that envelope with the measurement that motivated it;
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
| Experimental | Unit/malformed-input suite and a bounded clean loopback pass; unique stable mode ID; declared rung role and provisional envelope; decoder resource use is bounded by construction/test. |
| Optional | All experimental gates; bounded CI; frame Monte Carlo across the declared envelope; one bidirectional radio pair; full simulated sessions and recovery; adjacent-rung value/overlap; development-host CPU/RSS. |
| Default | All optional gates; envelope confirmed by the promotion-sized runs (level-0 spanning its policy's worst claimed conditions; top rung meeting the section 6 speed gate); 100-trial promotion session points; two bidirectional hardware pairs and hardware recovery; minimum-target CPU/RSS and no dropouts; complete clean-commit artifacts and documentation. |

Every mandatory row must be `passed`. `failed`, `unmeasured`, or `provisional`
blocks a new promotion. A regression in a default mode removes it from the
default registry unless a documented compatibility/security reason requires
a time-bounded exception. Mode IDs are never reused after on-air publication.

## Assessment of the current modes

This is a documentary audit of checked-in tests, capture files, and result
notes, not a new statistical or real-radio run. “CPFSK” covers the 300, 600,
and 1200 baud VHF rungs; where the evidence differs, the weakest rung controls
the group status.

| Requirement | CPFSK | VF3 | HC0 | HC1 |
| --- | --- | --- | --- | --- | --- |
| Rung role | level-0 (300 baud) through top (1200 baud) VHF | top VHF | level-0 HF control | top HF |
| Declared operating envelope | `vhf_bench_conservative`, 5 dB RF C/N and above (all three rungs) | `vhf_bench_conservative`, 10 dB RF C/N and above | all three mid-latitude Watterson presets including disturbed, across the tested SNR range | Watterson quiet above -5 dB and moderate at 10 dB and above; disturbed excluded |
| Unit, framing, malformed input | passed | passed | provisional | provisional |
| Bounded policy channel CI | passed | passed | passed | passed |
| Qualified frame Monte Carlo | passed (2026-08-29 clean-tree rerun after the channel-drain fix; 300 and 600 baud delivered 600/600 and 598/600, 1200 baud delivered 591/600, all within the FER/acquisition gate)  | passed within the declared envelope (2026-08-30 clean-tree run; 99-100/100 at every point from 10 dB RF C/N up). 0/100 at 5 dB is outside the envelope and root-caused to a genuine carrier-SNR floor, not an acquisition or threshold bug; the 300/600 baud rungs cover that region -- see campaign notes below | passed (2026-08-30 clean-tree run; 100/100 at every SNR point across quiet, moderate, and disturbed Watterson presets) | passed within the declared envelope (2026-08-30 clean-tree run; clears quiet above -5 dB and moderate at 10 dB+). The disturbed preset is outside the envelope by design and is covered by HC0, which passed the identical trials 100/100 |
| Full-stack connection and bidirectional ARQ | passed | passed | passed | passed |
| Scripted adaptation/fault recovery artifact | provisional | provisional | provisional | provisional |
| Bidirectional hardware frame gate | unmeasured | provisional (6/6, 3 each way) | provisional (2026-08-28 captures, 11/11 and 5/5 each way, below trial minimum) | unmeasured (2026-08-28 captures, 17/17 one direction, 0/3 the other; the weak leg is attributed to the IC-705 transmitting into a dummy load, so the session does not measure HC1's envelope and the 0/3 is an invalid run rather than a failed direction -- see `logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-28-hardware/INDEX.md`. Re-run required with both stations on antennas and the transmit configuration recorded) |
| Full hardware link/recovery gate | provisional | unmeasured | unmeasured | unmeasured |
| Useful throughput and adjacent overlap | provisional | provisional | provisional | unmeasured (no qualifying overlap sweep; the top-rung speed gate against the 7,050 bit/s reference is also unmeasured) |
| Development CPU and RSS | unmeasured | unmeasured | unmeasured | unmeasured |
| Minimum-target CPU, RSS, and dropouts | unmeasured | unmeasured | unmeasured | unmeasured |
| Complete promotion artifact/metadata | unmeasured | provisional | provisional | provisional |
| Top-rung speed gate (>=60% of the GOALS reference peak) | n/a below the top rung; unmeasured for 1200 baud | unmeasured | n/a (level-0) | unmeasured |
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
rung with a real operating-range boundary that a more robust mode (CPFSK
here) is expected to cover. VF3's declared envelope therefore starts at
10 dB RF C/N under `vhf_bench_conservative`. This is the intended shape for
a top rung: the correct response is to keep VF3's rate and let the CPFSK
rungs own the region below, not to add carrier-SNR margin at the cost of
speed. A future waveform or coding change may widen the envelope only if it
does not reduce peak useful throughput.

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
100/100. HC1's fast-rung disposition already assumes HC0 is the fallback for
conditions it cannot cover; this campaign is evidence that disturbed HF
conditions are one of them, and the disturbed preset is accordingly excluded
from HC1's declared envelope rather than treated as a defect. Adding
frequency-selective-fading margin to HC1 -- wider carrier spacing, heavier
coding -- would cost the peak rate that is HC1's reason to exist, and is not
indicated. The open work on HC1 is speed against the 7,050 bit/s reference
and a valid bidirectional hardware run, not disturbed-channel robustness.

No qualifying adjacent-rung overlap report, top-rung speed measurement, or
CPU/RSS artifact has yet been retained for any production mode. CPFSK and
HC0 pass the Monte Carlo gate outright; VF3 and HC1 pass it within the
declared envelopes recorded above. Existing default placement therefore
records historical/provisional acceptance rather than retroactively
declaring the new gates passed.

`experiments/hc2_32qam/` is not a declared mode and therefore has no manifest
entry or mode ID. It now has a deterministic clean-channel oracle round trip,
a real acquisition/CFO/equalization/phase-tracking receiver, and a
2026-08-30 AWGN frame sweep (`experiments/hc2_32qam/RESULTS.md`): 7,800
full-capacity trials, waveform-referenced SNR, Wilson intervals, and per-frame
EVM. None of that is qualification evidence. The AWGN sweep is a diagnostic
baseline, which section 3 explicitly does not accept as a qualification
channel; the artifacts are scratch, produced from a dirty tree with the
experiment package untracked, and no `logs/mode_qualification/` campaign
directory exists for HC2. Watterson trials, hardware evidence, negotiation,
and application-throughput work all remain prerequisites before it can enter
this process. As a candidate HF top rung it would be evaluated against the
section 6 speed gate on a good channel, with a narrow declared envelope,
rather than against disturbed-preset robustness. The AWGN result is
consistent with that shape -- delivery collapses below about 12 dB waveform
SNR and realized payload exceeds the 7,050 bit/s reference row from 12.5 dB
up -- but a declared envelope requires the fading evidence it does not yet
have.
