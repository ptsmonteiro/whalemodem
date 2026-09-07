# HR1: simulation-first plan for the most robust HF rung

## Purpose and status

HR1 is a working name for an experimental HF Level 0/control waveform whose
first priority is verified delivery across the fixed Level 0 envelope in
[`SPEED_LADDERS.md`](../../SPEED_LADDERS.md). Rate is secondary, but it must
still reach the 20 bit/s useful-application floor. The experiment also tests
whether spending approximately the airtime budget of VARA HF's published
lowest row can buy materially more margin than Whalemodem's current HC0.

The external reference is limited to the vendor-published VARA HF standard-mode
row: 23 symbols/s, 32 carriers, FSK, and 18 bit/s net. This repository has no
VARA waveform, decoder, channel capture, or measured VARA threshold. The row is
therefore a **rate and geometry reference, not robustness evidence**. No result
from this experiment may say that HR1 beats, matches, or works below VARA until
both implementations are exercised through the same calibrated audio/channel
boundary or through controlled radios.

This file defines the experiment before choosing a waveform. It deliberately
adds no encoder, decoder, mode ID, registry entry, or production behavior.

## What is already known

- HC0 is the current HF Level 0/control mode: non-coherent constant-envelope
  16-FSK at 93.75 symbols/s, 54 DATA bytes per 3.423-second keying, or about
  126.2 useful DATA bit/s before ACKs, retries, turnaround, and setup.
- The retained 2026-08-30 Monte Carlo campaign delivered 100/100 HC0 frames at
  every tested point from -5 through +20 dB waveform SNR in quiet, moderate,
  and disturbed mid-latitude Watterson presets. This establishes a pass at
  -5 dB; it does **not** locate HC0's fading/SNR floor.
- HC0's implementation notes and an AWGN pilot put its clean-AWGN edge near
  -16 dB. A smaller signature experiment found HC0 nearly unusable at -16 dB
  in moderate Watterson fading and about 91--95% decoded at -12 dB; its
  disturbed -12 dB samples decoded roughly 88%. Those are useful boundary
  hypotheses, not promotion evidence.
- Current canonical Watterson qualification constructs a fresh independently
  seeded channel per frame, then adds AWGN whose power is calculated from that
  frame's post-Watterson mean signal power. This is reproducible and valid for
  comparison with retained results, but per-frame noise normalization partly
  removes absolute slow-fade depth. HR1 must retain that canonical curve and
  add a fixed-noise-density, continuous-fading campaign before claiming
  resistance to deep fades.

At equal information efficiency, reducing HC0's 126.2 useful frame bit/s to
18 bit/s buys `10 log10(126.2 / 18) = 8.46 dB` of energy per useful bit. That
suggests an AWGN edge near -24 dB is a plausible design hypothesis, not a
guarantee. Fading, synchronization, interference, finite interleaving, and
session overhead can consume that budget.

## Measurement conventions

### Primary SNR

All simulated curves use `SnrSpec(kind="waveform")` and spell out
`waveform_snr_db`. Signal power is mean square over the complete half-open
keying interval `[0, tx_samples)`, including acquisition, checked framing,
FEC, intentional guards, and tail silence. Noise power is real AWGN over the
complete 0 Hz to 24 kHz Nyquist band at the 48 kHz audio boundary. Stage order
and the exact reference sample bounds are part of every artifact.

This whole-keying reference makes transmitted energy and silence honest and
allows a useful-bit energy conversion. It is not the decoder's `tone_snr_db`
or another receiver quality estimate; those remain separate diagnostics.

For a useful bit rate `R_u = useful_application_bits / keyed_seconds`, the
corresponding analytical conversion is

```text
Eb/N0(useful), dB = waveform SNR, dB + 10 log10(24000 / R_u)
```

provided signal and noise use the same complete-keying interval. At 18 useful
bit/s the offset is +31.25 dB: -24 dB waveform SNR is approximately +7.25 dB
useful-bit Eb/N0. If quoting a conventional 3 kHz in-band SNR under white
noise, add `10 log10(24000/3000) = 9.03 dB` to the repository's waveform SNR;
thus -24 dB waveform SNR is approximately -15 dB SNR in 3 kHz. Every such
conversion must name the 3 kHz bandwidth and is analytical until measured.

Report two other rates rather than overloading "net":

1. **Frame useful rate:** verified DATA-body bits divided by keyed seconds.
2. **Session useful rate:** verified application bits divided by total
   simulated time, including connect, common leads, ACKs, retries, fallback,
   turnaround, and disconnect.

Also retain physical payload bytes, DATA-body bytes, total frame airtime, and
nominal coded/raw rates. A short control packet and a full DATA frame are
separate workloads; fixed-length padding is not useful throughput.

### Rate and latency budget

The target is at least 20 bit/s **session useful rate** in long,
one-direction bulk transfer at every point in the fixed Level 0 envelope, not
merely a 20 bit/s codec. Screening should use a frame useful rate of at least
28--34 bit/s to leave room for ARQ overhead. For the standard 54-byte
DATA-body comparison, 20 frame bit/s corresponds to 21.6 seconds of keying;
any longer candidate must justify a smaller payload/session design rather
than claiming the Level 0 rate from nominal arithmetic. Measure 12-byte
control-packet airtime and connection setup separately, because an extremely
robust mode that takes minutes to establish a link is not automatically
usable.

## Falsifiable design targets

These are experiment targets, not current claims:

| Target | Required result |
| --- | --- |
| Baseline | Reproduce HC0 and locate its AWGN and each Watterson boundary with the same harness, payload, seeds policy, and gates used for HR1. |
| Minimum value | HR1's qualified boundary is at least 3 dB below HC0 in `mid_latitude_disturbed`, with a point where HR1 passes and HC0 fails. |
| Primary robustness stretch | Pass at -24 dB canonical AWGN and -20 dB fixed-N0 `mid_latitude_disturbed`, while maintaining at least 20 bit/s long-transfer session throughput. This is additional coverage beyond the fixed Level 0 envelope. |
| Geographic coverage | Declare and pass an explicit boundary for every F.1487 preset, including `mid_latitude_disturbed_nvis` and `high_latitude_disturbed`; do not substitute the three mid-latitude presets for the complete registry. |
| Stretch | Pass -24 dB fixed-N0 `mid_latitude_disturbed` and -20 dB fixed-N0 `high_latitude_disturbed`, or document the measured physical/algorithmic limit. |
| Acquisition | Acquisition is no worse than checked-frame delivery at the boundary; a lead miss or false label cannot suppress a body that would pass integrity checks. |
| Integrity | Zero undetected payload substitutions, false checked frames, exceptions, hangs, or unbounded candidate searches. |

Failure of the stretch numbers is useful if the campaign locates a stable
boundary and attributes the loss. Do not redefine the fixed Level 0 target
after seeing the data. HR1 cannot fill Level 0 unless it spans the complete
target envelope, meets the throughput floor, and adds the distinct robust
point required by `MODE_QUALIFICATION.md`.

## Test matrix

Run the matrix in the stages below. Do not take the full Cartesian product of
all impairments: screen one factor at a time, promote the boundary cases, then
run specified combined stresses and full sessions.

### 1. Baseline and analytical feasibility

- Freeze workloads: empty/12-byte control, 16-byte short DATA, and 54-byte
  full DATA body; record the extra 10-byte checked air header separately.
- Measure exact energy per useful bit, occupied 99% power bandwidth, peak,
  RMS, crest factor, keyed time, decoder work, and memory for HC0 and every
  HR1 candidate.
- Use an oracle-aligned demodulation/coding curve only to reject bad ideas.
  Then add real acquisition, timing, CFO, clock, integrity, and bounded search
  before any candidate can win.
- Re-run HC0 on canonical AWGN at `-10, -12, -14, -15, -16, -17, -18, -20`
  dB and on each Watterson preset at a coarse `-5` to `-24` dB grid. Refine
  in 1 dB steps around every transition. This baseline must precede an HR1
  superiority claim because retained HC0 evidence does not locate its floor.

### 2. AWGN floor and acquisition separation

Screen HR1 at `-30, -27, -24, -22, -20, -18, -16, -12` dB, then refine the
10--90% transition in 1 dB steps. At every point report separately:

- acquisition probability (candidate found above its frozen threshold);
- payload/CRC failure conditional on acquisition;
- total FER and exact verified payload delivery;
- false candidates tried, winning candidate rank, timing/CFO/clock error;
- decoder work for success, acquisition failure, and payload failure.

Thresholds and candidate-ranking limits are selected on training seeds, then
frozen before held-out tests. Add at least 100,000 signal-absent windows of
silence, AWGN, a bare carrier, wrong-mode frames, and impulsive/NBI noise.
There must be no checked false payload; report false acquisition separately
so CRC is not used to conceal an unusable search load.

### 3. Watterson coverage and fade duration

Exercise every exact `WATTERSON_PRESETS` name:

| Family | Presets | Initial SNR grid |
| --- | --- | --- |
| Mid-latitude anchor | quiet, moderate, disturbed | `-24, -22, -20, -18, -16, -14, -12, -10, -5` dB |
| Delay stress | disturbed NVIS | `-22, -20, -18, -16, -14, -12, -10, -5, 0` dB |
| Geographic extremes | low quiet/moderate/disturbed; high quiet/moderate/disturbed | `-20, -16, -12, -8, -4, 0, 5` dB, extended until each edge is bracketed |

For repository comparability, first run the canonical independent-frame
`Watterson -> AwgnChannel` chain. Then run a second campaign in which AWGN N0
is calibrated once from the unfaded transmitted reference and held fixed
while Watterson gain evolves continuously. That second runner is a known
harness addition, not something to emulate by relabeling canonical SNR.

Use independent seeds for statistical trials and never share a channel or RNG
between directions. In addition, use at least 20 continuous realizations per
preset, each with total keyed-plus-gap dwell of
`max(300 seconds, 20 / frequency_spread_hz)`; this gives the 0.1 Hz quiet
preset at least 300 seconds and prevents many short, reset frames from being
mistaken for long-duration fade evidence. Record delivery in time blocks and
compare early/late distributions. Interleavers and frame duration must span a
stated fraction of the slowest fade time; if they do not, session ARQ must be
shown to survive the resulting outage bursts.

### 4. Synchronization and radio-boundary impairments

Use clean, near-boundary AWGN, `mid_latitude_disturbed`, disturbed NVIS, and
`high_latitude_disturbed` anchors. Test both signs and random values within
each declared range.

| Impairment | Screen points |
| --- | --- |
| Static audio CFO | 0, +/-25, +/-50, +/-100, +/-150 Hz |
| Linear frequency drift | 0, +/-0.1, +/-0.5, +/-1, +/-5 Hz/s, combined with nonzero CFO |
| Sample clock | 0, +/-10, +/-25, +/-50, +/-100 ppm |
| Leading blackout | 0, 50, 100, 250, 500 ms of zeroed post-channel audio |
| Mid/trailing blanking | one and multiple 10, 50, 100, 250 ms zeroed spans at seeded positions |
| Hard clipping | thresholds at 1.0, 0.75, 0.5, and 0.3 times candidate peak; record input/output power and spectral growth |
| SSB response | reproducible TX/RX band-pass combinations with edges 300/500/700 Hz and 2200/2400/2700 Hz, with noise placement explicit |

The declared envelope is the largest symmetric range that passes; a
one-sided anomaly must be investigated. Include near-limit CFO plus opposite
clock error and clipping plus leading blackout as pairwise combined stresses.
Acquisition/body fallback must remain bounded even when the common lead is
erased, mislabeled, or produces multiple candidate boundaries.

### 5. Interference, impulses, notches, and non-Gaussian noise

Define signal-to-interference ratio using the same complete-keying signal
power. Preserve exact stage order and seeds.

- Single CW interferer at every signaling tone/carrier, halfway between
  adjacent tones, and swept across 300--2700 Hz, at SIR `+10, 0, -10, -20`
  dB. Repeat with two independent tones.
- Narrow Gaussian interferers of 10, 50, and 200 Hz bandwidth, stationary and
  drifting, with 25%, 50%, and 100% duty cycles.
- Notches 50, 100, and 300 Hz wide at 10, 20, and 40 dB depth, stationary and
  drifting through the occupied band.
- Poisson impulses at 0.1, 1, and 5 events/s; 2, 10, and 50 ms envelopes; and
  peak levels 10, 20, and 30 dB above signal RMS.
- Seeded burst blanking/noise occupying 1%, 5%, 10%, and 20% of frame time,
  both randomly distributed and contiguous.

After one-factor screens, promote the weakest passing and first failing point
from each class into combined Watterson + fixed N0 + CFO/clock + interference
tests. At least one promotion point must combine disturbed fading, target SNR,
50 Hz CFO, 50 ppm clock error, a 100 ms leading blackout, and a 0 dB-SIR CW
interferer. The result may define a lower combined-stress SNR floor than the
single-factor target, but it must be reported rather than averaged away.

### 6. Statistical gates

Use deterministic derived seeds and fresh artifacts per model/preset.

- Exploration: 30 independent trials per point; never call this a gate.
- Boundary shaping: at least 100 independent trials per `(candidate, point)`.
- Claimed boundary: at least 300 trials at the last passing and first failing
  SNR for every preset, matching `MODE_QUALIFICATION.md`.
- Final primary/combined-stress points: 1,000 trials to narrow uncertainty and
  detect rare search or integrity behavior.
- Continuous-fade sessions: at least 20 independent long realizations in each
  direction at smoke points and 100 per promotion point.

Inside the declared envelope, the 95% Wilson upper bound on FER is at most
10%, the 95% Wilson lower bound on acquisition probability is at least 90%,
and there are zero `error` outcomes. With 100 trials this requires at least 96
acquisitions and no more than 4 frame errors; with 300 it requires at least
281 acquisitions and no more than 19 frame errors. Preserve
`acquisition_failed`, `payload_failed`, and `error` as distinct outcomes.

The pass boundary is the lowest SNR whose gate passes and for which a second
independent campaign also passes; the next lower tested point must fail or the
grid must extend. Report Wilson intervals, not only point estimates. Use a
predeclared bootstrap confidence interval for median session throughput and
retain per-realization outage lengths, retransmissions, and completion time.

### 7. Asymmetric full-link and session behavior

After a waveform survives frame tests, install it only in an experimental
registry and exercise the ordinary link/session boundary. Each promotion
trial transfers at least 10,000 verified application bytes in both directions,
connects, negotiates, transfers, yields the floor, adapts independently,
recovers, and disconnects.

Required directional pairs include:

- target disturbed/fixed-N0 A-to-B and a clean B-to-A return path;
- different Watterson preset, SNR, CFO sign, drift, and sample-clock error in
  each direction;
- a weak return path that loses ACKs while DATA is received;
- transient deep fade, lost DATA, lost ACK, corrupted DATA, and transport
  failure, followed by exact recovery without duplicate bytes;
- HR1-only sessions and the complete ladder, proving fallback to HR1 and later
  climb independently in both directions.

The gate is 100% exact bidirectional delivery and clean disconnect, with the
95% Wilson lower bound on session completion at least 95% at promotion points.
No deadlock, infinite retransmission, false disconnect, or cross-direction
mode coupling is allowed. Report connection time, control-frame success,
directional mode history, time in each mode, retransmissions, outage duration,
and session useful rate. Eighteen bit/s must be demonstrated on sufficiently
long bulk transfer; setup time is reported separately and is never amortized
away without saying over how many bytes.

### 8. CPU, memory, and bounded work

The decoder must remain streamable and practical on the documented minimum
Raspberry Pi-class target. Before optional promotion, measure development-host
idle search, successful worst-case decode, failed acquisition, many-candidate
search, and a 30-minute impaired session. Before default promotion, repeat on
the minimum target.

Use the project gates: decoder p95 work below the real-time receive-window
duration, zero audio overruns, peak RSS no more than 256 MiB, and post-warm-up
growth no more than 16 MiB or 5%, whichever is larger. Also set and test hard
limits for receive-buffer duration, candidate count, iterations/list size,
FEC work, and maximum decode latency. Record CPU model, cores, governor, RAM,
OS, Python/NumPy/SciPy versions, sample rate, worker count, commit, and dirty
state. Desktop parallel Monte Carlo speed is not evidence of real-time
single-session feasibility.

## Reproducibility and artifacts

Development artifacts live under `experiments/hr1/results/`; eventual
promotion artifacts belong under
`logs/mode_qualification/hf-ssb/hr1/<date>/`. Each retained run must include:

- immutable schema version, exact command, UTC times, master/derived seeds,
  trial count, worker count, Git commit and dirty paths;
- candidate revision/hash and frozen constants, payload/workload, mode role,
  provisional envelope, sample rates, exact keyed/reference sample bounds;
- expanded ordered channel description, including Watterson oscillator count,
  control rate, 2-sigma spread convention, fixed-N0 calibration, filters,
  clipping, interference, blanking, CFO/drift, and clock error;
- per-trial outcome, useful/physical bytes, energy, airtime, decoder metrics,
  channel measurements, candidate count/rank, CPU time, wall time, and error;
- Wilson intervals and machine-readable pass/fail calculations;
- failed captures with expected payload and one-command deterministic replay,
  plus representative successful and threshold-adjacent captures;
- environment and CPU/RSS/overrun metadata; and
- a human index linking every target and qualification gate to its artifact.

Training, tuning, and held-out seed ranges must be disjoint and recorded.
Default-promotion evidence requires a clean tree. A rerun that changes a
threshold, code, interleaver, frame layout, or receiver search policy starts a
new candidate revision and cannot be merged with earlier trials.

## Sequential execution and promotion stops

Execute each point only after its predecessor leaves a written handoff:

1. **Freeze this protocol and baseline HC0.** Implement only missing benchmark
   support (not HR1), locate HC0's true AWGN/Watterson edges, and record the
   canonical-versus-fixed-N0 distinction.
2. **Screen waveform/FEC/interleaver families with an oracle.** Keep only
   candidates analytically capable of the rate/energy target and surviving
   all Watterson delay/Doppler families. No robustness claim is allowed here.
3. **Build bounded real acquisition and decode for the best candidate.** Add
   integrity and hostile-input tests; freeze training/held-out seeds and
   thresholds.
4. **Run AWGN and all-preset Watterson boundary campaigns.** Stop or redesign
   unless the candidate adds at least 3 dB over HC0 in disturbed fading.
5. **Run synchronization, radio-boundary, interference, and combined-stress
   matrices.** Revise the declared envelope only from retained evidence.
6. **Integrate experimentally and run asymmetric full sessions/recovery.**
   Confirm at least 20 bit/s session useful rate and Level 0 behavior.
7. **Measure resources, then wait for radios.** Development-host resource
   evidence may proceed; optional/default promotion remains blocked until the
   bidirectional radio and minimum-target gates in `MODE_QUALIFICATION.md` are
   satisfied.

Each handoff states hypotheses tested, constants frozen, exact artifacts,
failed seeds/replay commands, open discrepancies, and a go/redesign/stop
decision. Do not assign a stable on-air mode ID or modify the default HF
ladder until the experimental gates pass.

## Claim boundary while no radios or VARA implementation exist

Simulation can establish reproducible performance of HC0 and HR1 under the
repository's precisely described channel models, including a measured SNR
boundary, useful-bit Eb/N0 calculation, impairment envelope, relative HC0
gain, session behavior, and development-host resource cost.

Simulation cannot establish over-the-air sensitivity, immunity to real AGC,
ALC, SSB filter phase, oscillator phase noise, sound-card/radio nonlinearity,
external interference statistics, antenna/path behavior, or bidirectional
hardware reliability. It also cannot establish VARA parity from the
published 18 bit/s row, infer VARA's Watterson threshold, or compare latency,
FER, acquisition, occupied spectrum, and CPU without running VARA under the
same conditions. Until those measurements exist, the strongest honest result
is: "HR1 passes the stated Whalemodem simulation gate at X dB and gains Y dB
over HC0 at Z useful rate." Hardware and VARA-comparative claims remain
unmeasured.
