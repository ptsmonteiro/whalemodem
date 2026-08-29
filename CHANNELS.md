# Simulated channels and trial records

This document defines the common boundary used to qualify waveforms and later
to impair full modem sessions. It complements [`FRAMING.md`](FRAMING.md): a
waveform owns modulation and decoding, while a channel owns everything that
happens to its transmitted audio before it reaches the peer.

The promotion gates, minimum trial counts, required hardware and resource
evidence, and current production-mode assessment are defined in
[`MODE_QUALIFICATION.md`](MODE_QUALIFICATION.md). The tools below produce
evidence for that process; their existence alone does not qualify a mode.

## Channel contract

`whale.channel.AudioChannel` consumes a finite mono waveform at its declared
sample rate and returns `ChannelResult(audio, measurements)`. A channel may
change the sample count and retain state between calls. Consequently each
direction has its own instance; A-to-B and B-to-A never share fading state or
a random generator.

One-shot callers finish a finite keying with `drain(final_input=None)`. The
optional mono `final_input` is continuation audio emitted by an earlier stage,
not a new keying. The result contains that continuation after this stage plus
the stage's finite retained response. Draining never resets the channel.
Ordinary `process()` remains the streaming operation: state carries between
calls unchanged, and no tail is emitted unless requested. `ChannelChain`
folds drain continuation through every stage in order, so an early filter tail
passes through all later filters, clock conversion, and random stages without
repeating a per-keying delay or radio mute.

Identity, gain, clipping, frequency conversion, AWGN, impulse/noise,
interference, and sample-clock stages have no intrinsic tail; they transform
only supplied continuation. A delay already emits its complete per-keying
delay in `process()` and does not add it again during drain. Watterson's finite
block path delay is likewise already present in its ordinary result. FIR
responses drain their remaining finite support. Stable IIR responses use the
zero-input length needed for the largest pole magnitude to fall below `1e-6`
of its initial envelope, capped at one second of input. Combined models add
finite FIR and path delay and use the same cap. This is a bounded engineering
criterion, not a claim that an IIR has an exact finite response. Seeded random
generators advance only for finite samples actually processed, and `reset()`
replays ordinary and drained output deterministically.

Random channels own an explicitly seeded generator. `reset()` restores the
initial seeded realization, and `describe()` supplies JSON-compatible
configuration including the seed. This makes a failed trial replayable from
its result document. Channel measurements describe known injected or measured
conditions. Decoder estimates belong in the separate decoder-metrics field.

The common stages in `whale.channel` are:

- `IdentityChannel`, the lossless reference;
- `AwgnChannel`, seeded real white noise using waveform-referenced SNR;
- `FrequencyOffsetChannel`, an audio-frequency shift with optional linear
  drift in Hz/s and phase/time continuity across calls;
- `DelayChannel`, a fixed integer-sample delay prepended to each keying;
- `FilterChannel`, a stateful Butterworth low-, high-, or band-pass response;
- `ClippingChannel`, symmetric hard clipping at an absolute amplitude;
- `SampleClockChannel`, receiver-clock error in ppm, where positive error
  produces more captured samples for the same physical waveform;
- `GainChannel`, an explicit linear or dB voltage gain with measured input and
  output power;
- `ImpulseNoiseChannel`, seeded Poisson events with fixed, uniform, or normal
  amplitudes and rectangular, Hann, or exponential burst envelopes;
- `NarrowbandInterferenceChannel`, one or more seeded tones or narrow Gaussian
  noise bands with absolute or waveform-relative power, linear drift, and a
  periodic duty cycle;
- `NotchChannel`, a finite-depth notch with configurable center, width, and
  optional linear drift;
- `ChannelChain`, which applies stages in their listed order and namespaces
  their measurements by stage.

Stage order is part of the channel definition. In particular, noise before a
receive filter is filtered with the signal, while noise after it is not;
clipping before filtering produces a different spectrum from clipping after
filtering. `describe()` retains that order. The frequency and filter stages
retain state across calls and return to their initial state on `reset()`.
The sample-clock implementation uses a documented rational approximation to
the requested ratio and reports both values.

Interference power is mean-square audio power. An absolute `power_db` uses
`10 ** (power_db / 10)` directly; a relative value multiplies that ratio by
the input waveform's mean-square power for the current call. Tone amplitude
is chosen to produce that power while active. Narrow noise is generated as
seeded low-pass Gaussian noise translated to its configured center. Duty
cycle uses a one-second period by default and remains continuous across calls.
Measurements record the realized injected power, active fraction, and actual
start/stop frequencies. Impulse events likewise continue across call
boundaries and report event counts, active samples, realized power, and peak
amplitude. The drifting notch updates its coefficients in short blocks and
reports the actual center-frequency interval used.

## Watterson HF channel

`WattersonChannel` implements the stationary Gaussian-scatter model described
by Recommendation ITU-R F.1487 for narrowband HF modem testing. Each
`WattersonPath` is a delayed copy of the analytic audio multiplied by an
independent, zero-mean complex Gaussian fading process. Its Doppler power
spectrum is Gaussian, may have a Doppler shift, and uses F.1487's frequency
spread convention: the configured spread is **2 sigma**, not sigma. Path
outputs are power-weighted, summed, and normalized so their expected total
power is the input power.

The fading process is synthesized as a deterministic sum of independently
phased sinusoids whose frequencies are drawn from the requested Gaussian
spectrum. Gains are evaluated on a low-rate control grid and interpolated at
the audio samples. This preserves fade time and phase between keyings without
performing hundreds of oscillators at 48 kHz. The seed, oscillator count,
control rate, paths, and spread convention are included by `describe()`.

`WATTERSON_PRESETS` contains the two-independent-path, equal-power cases from
F.1487 Annex 3: quiet, moderate, and disturbed low-, mid-, and high-latitude
conditions, plus disturbed mid-latitude NVIS. They intentionally retain the
geographic names and exact delay/spread values from the Recommendation. The
older labels “good”, “moderate”, and “poor” are not aliases because they hide
which parameter combination was actually tested.

This model represents multiplicative ionospheric distortion. AWGN and radio
audio responses remain separate stages in a `ChannelChain`, making their
ordering and SNR reference explicit. F.1487 describes the model as validated
for 3 kHz channels and potentially applicable up to 12 kHz; it should not be
presented as a general wideband-HF propagation model.

Statistical tests validate zero mean, circular quadrature variance, unit mean
power, the Rayleigh-envelope fourth moment, independent paths, Gaussian
Doppler centroid and 2-sigma width, delay, and seeded replay. These validate
the simulator process itself; modem performance sweeps still require test
durations appropriate to the selected Doppler spread.

## Complete scenario presets

`whale.scenario` provides a composition layer above individual channel stages.
For HF, `HfSsbScenario` builds the complete ordered path:

```text
TX response -> clipping -> frequency offset/drift -> Watterson propagation
            -> narrowband interference -> AWGN -> RX response
```

```python
from whale.channel import SnrSpec
from whale.scenario import HfSsbScenario

scenario = HfSsbScenario.from_preset(
    "moderate", sample_rate=48_000, snr=SnrSpec(8.0), seed=1234)
channel = scenario.build()
```

The `quiet`, `moderate`, and `disturbed` recipes expand to explicit filter,
clip, oscillator, interference, noise, and propagation parameters in
`scenario.describe()`. They currently select the corresponding mid-latitude
F.1487 cases. They do not replace or alias `WATTERSON_PRESETS`: callers that
need a particular F.1487 geography continue to use its full geographic name.
The scenario values are reproducible project test recipes, not additional ITU
recommendations.

`channel.describe()` returns that same expanded scenario description, so a
trial can retain a replayable recipe after the scenario builder goes out of
scope.

`FmScenario` (also exported as `VhfFmScenario`) supplies the same three
scenario names around the complex-IQ narrow-FM model. Its defaults use the
`vhf_bench_conservative` measured response, while `radio_preset=` can select a
directional measured leg and `carrier_to_noise_db=` can override the recipe's
RF C/N. The expanded description includes
`profile_authority="whalemodem_project_simulation"` and
`is_propagation_standard=false`. These are convenient stress profiles based
on this project's particular bench plus explicit exploratory RF assumptions;
they are not universal FM propagation conditions or radio specifications.
The geographically named F.1487 and measured `FM_RADIO_PRESETS` registries
remain the authoritative underlying component presets.

## Hardware mode sweeps

`scripts/sweep_modes.py` performs direct frame qualification over two radios.
`--channel vhf-fm` or `--channel hf-ssb` selects the corresponding
`ChannelPolicy` and obtains its complete ordered registry; mode names and IDs
are never duplicated in the tool. Every selected mode sends deterministic
random packets at its full link capacity (air header plus DATA chunk) in both
directions unless narrowed explicitly.

The sweep bypasses negotiation and ARQ so each result describes one physical
frame. It records acquisition, verified-payload, and exception outcomes;
keyed duration; separate TX/RX rates and sample counts; receive levels; and
common decoder diagnostics. Failed captures are saved by default as `.npz`
files containing both audio and expected payload. `result.json` uses the
versioned `TrialRun` schema and includes per-mode/direction success rates,
95% Wilson intervals, useful decoded throughput, radio names, registry IDs,
seed, and Git commit.

Examples:

```powershell
python scripts/sweep_modes.py --channel vhf-fm
python scripts/sweep_modes.py --channel hf-ssb --trials 10
python scripts/sweep_modes.py --channel hf-ssb --modes hc0 --direction ab
```

Results default to a timestamped directory under `logs/mode_sweeps`. A sweep
returns failure unless every mode/direction reaches `--required-rate`, which
defaults to 100%. A small trial count is a smoke test rather than strong
reliability evidence; the confidence interval in the output makes that
uncertainty visible.

## Complex-baseband VHF FM channel

`whale.fm_channel.ComplexFmChannel` retains the real 48 kHz audio boundary
used by the modem but simulates the radio path internally as complex IQ:

```text
TX filter -> pre-emphasis -> TX limiter -> FM modulation
          -> time-varying RF multipath/flutter
          -> RF noise and narrowband/co-channel interference
          -> IF filter/limiter/discriminator -> de-emphasis -> RX filter
          -> squelch state machine -> sample-clock mismatch
```

This is materially different from adding noise to recovered audio. Complex
RF noise passes through the limiter and discriminator, so output quality
falls nonlinearly once carrier-to-noise approaches the FM threshold. Carrier
frequency error interacts with the receiver IF filter. Delayed RF paths can
have independent amplitude flutter, phase drift, and phase flutter before
detection. Tests pin both threshold behavior and loss
near the IF-filter edge.

`carrier_to_noise_db` is carrier power divided by complex white-noise power
across the simulator's complete IQ Nyquist band. It is deliberately named
C/N rather than SNR and is not comparable to waveform-referenced audio AWGN
without a stated bandwidth conversion. `deviation_hz`, `full_scale_audio`,
IF bandwidth, frequency error, RF paths, RF interference, and clipping are
explicit inputs. TX and RX audio bands are separate. Pre-emphasis and
de-emphasis use independently configurable time constants, and the TX limiter
is explicitly before modulation. The squelch has open/close thresholds,
hysteresis, attack, hang, and closing-ramp controls. Leading and trailing
radio clipping are separate from that state machine. `describe()` records the
ordered stage names and every parameter needed to replay the path.
The defaults of 2.5 kHz deviation at 0.6 audio amplitude and 7.5 kHz IF
bandwidth are initial narrow-FM simulation assumptions, not bench
measurements.

### Measured VHF bench presets

`FM_RADIO_PRESETS` initially contains:

- `ic705_to_kg_uv9d`;
- `kg_uv9d_to_ic705`;
- `vhf_bench_conservative`, taking the more adverse measured value from
  each direction.

The directional audio magnitude anchors come from the middle 150 ms block of
five trials in
`experiments/ofdm/results/measurements/bandwidth.json`. They are represented
by a minimum-phase filter passing through the measured -6 and -10 dB band
edges; the measurement did not identify phase, so no claim is made that this
reconstructs the radios' group delay. The directional sample-clock values are
the -3.7/+3.1 ppm results from `scripts/measure_clock_offset.py`. The 110 ms
mute on the IC-705-to-handheld leg is the Wouxun squelch-opening blackout
measured and described in `experiments/ofdm/screen_ofdm.py`. The measured
0.505/0.815 ms delay-spread values are
retained as preset metadata but are not converted into invented discrete RF
echoes; callers can provide measured or exploratory `FmRfPath` values.

```python
from whale.fm_channel import ComplexFmChannel

channel_ab = ComplexFmChannel.from_preset(
    48_000, "ic705_to_kg_uv9d", carrier_to_noise_db=15, seed=1)
channel_ba = ComplexFmChannel.from_preset(
    48_000, "kg_uv9d_to_ic705", carrier_to_noise_db=15, seed=2)
```

Those are separate directional instances suitable for the paired audio
transport. The presets approximate this particular cabled audio/radio bench;
they are not generic models of every IC-705 or KG-UV9D installation.

For controlled experiments, `FM_SYNTHETIC_PROFILES` contains `flat_nbfm` and
`handheld_nbfm`. These profiles use explicit TX/RX filters, 75 microsecond
pre/de-emphasis, a pre-modulation limiter, and synthetic squelch parameters;
they are project recipes rather than measurements:

```python
channel = ComplexFmChannel.from_profile(
    48_000, "handheld_nbfm", carrier_to_noise_db=15, seed=3,
    rf_paths=(FmRfPath(), FmRfPath(
        delay_seconds=0.0005, amplitude=0.35,
        gain_flutter_depth=0.2, gain_flutter_hz=6.0)),
    rf_interference=(FmRfInterference(
        kind="cochannel", offset_hz=1200, power_db_relative=-18),))
```

Measured presets intentionally continue to apply their historical combined
post-discriminator magnitude response; it cannot be split honestly into TX
and RX components from the available end-to-end measurement.

## Regression tests and Monte Carlo benchmarks

Channel qualification is deliberately split by purpose:

- `tests/test_channel_regressions.py` is the bounded CI matrix. It uses fixed,
  independently derived seeds and one or two full-capacity frames per point.
  The selected points cover the VHF ladder through the conservative measured
  FM preset and both HF modes through moderate mid-latitude Watterson fading
  plus AWGN. These tests catch deterministic performance regressions; their
  tiny sample counts are not reliability estimates.
- `scripts/benchmark_simulated_channels.py` is the explicit Monte Carlo tool.
  It sweeps waveform SNR for AWGN/Watterson or RF C/N for complex FM, defaults
  to 100 trials per mode and point, and reports acquisition probability, FER,
  payload delivery rate, and 95% Wilson intervals. BER is present only when a
  waveform decoder supplies bit-error evidence. Injected-channel measurements
  and decoder estimates remain separate in every trial. The tool writes all
  trials and configuration through `TrialRun` schema version 2 and is never
  collected implicitly by pytest. By default it transmits each mode's full
  DATA chunk. `--payload-bytes` instead selects a non-negative DATA-body size
  that must fit every selected mode. Artifacts record the requested DATA size,
  the per-mode DATA size actually used, and the complete encoded payload size,
  which is 10 bytes larger because it includes the shared air header.
- `scripts/benchmark_sessions.py` is the separate full-stack Monte Carlo tool.
  It reuses the exact channel factories above, then drives connection,
  bidirectional ARQ transfer, adaptation, and disconnect through the radio-free
  TCP/session harness. It reports setup, transfer, and teardown in simulated
  keyed-audio seconds; connection/transfer/disconnect outcomes; retransmissions,
  ACK timeouts, duplicate DATA evidence of a lost ACK, mode changes, and useful
  bytes per simulated second. `--reverse-model` and `--reverse-points` create an
  explicitly asymmetric path and results retain distinct A-to-B and B-to-A
  records. Host wall-clock time is deliberately not a performance metric.

Examples:

```powershell
python scripts/benchmark_simulated_channels.py --model fm --policy vhf-fm `
    --points 5 10 15 20 25 30 --trials 100
python scripts/benchmark_simulated_channels.py --model fm --policy vhf-fm `
    --fm-preset ic705_to_kg_uv9d --points 10 20 30 --trials 20 `
    --modes 2 --payload-bytes 193 --out logs/mode2_diagnostic.json
python scripts/benchmark_simulated_channels.py --model watterson `
    --policy hf-ssb --watterson-preset mid_latitude_moderate `
    --points -5 0 5 10 15 --trials 100
python scripts/benchmark_sessions.py --model fm --policy vhf-fm `
    --points 15 20 25 --trials 20 --bytes 4000
python -m pytest -m channel_regression -q
```

The Monte Carlo seed for each frame is derived from the master seed, global
mode ID, point index, and trial number. Selecting fewer modes therefore does
not silently change the realizations of the modes that remain. A benchmark
result should be retained whenever its performance claim is retained.

The 2026-08-29 mode-2 diagnostic matrix under
`logs/mode_qualification/vhf-fm/cpfsk/2026-08-29/diagnostics` is investigation
evidence, not promotion evidence: it has only 20 trials per point. Its 1,080
trials exposed a finite-buffer boundary in the direct-frame FM benchmark. The
benchmark passes exactly the modulated frame to the stateful FM channel and
adds receive-filter delay padding only after channel processing. The measured
minimum-phase response returns one output block of the same length, so its
response to the last input samples is not present unless later audio is also
processed. Every one of the 229 failures acquired and differed in exactly the
last body-CRC bit; a targeted replay of a known failure for each preset decoded
when 10 ms of post-frame audio was processed through the channel. Do not read
that campaign as evidence of RF C/N threshold behavior, a conservative-preset
penalty, or a 1200-baud payload-length ceiling. The reusable drain contract now
fixes that boundary: the runner processes the frame, drains the same channel,
concatenates both outputs, and only then adds receive-decimator padding.
Fixed-seed replays of a previously failing frame decode exactly for all three
FM presets. The retained matrix remains diagnostic evidence; a promotion-sized
campaign still must be run and retained before the Monte Carlo gate can pass.

The repository-internal radio-free harness in `tests/support/audio_link.py`
applies this boundary at the 48 kHz capture rate. `DirectionalAudioLink` owns
distinct A-to-B and B-to-A channels, captures every `ChannelResult`, records
simulated keyed airtime, and supports deterministic reset and replay. Optional
frame-drop and pre-channel transport-fault hooks support full-session recovery
tests without adding simulation behavior to production `RadioTransport`.
Channel output passes through the modem's normal anti-aliased 48-to-12 kHz
receive conversion. With no channel supplied, each direction gets a distinct
`IdentityChannel`, preserving the lossless acceptance-test behavior.

## SNR conventions

An unqualified `snr_db` is forbidden in channel configuration and result
metadata. Use a `SnrSpec` kind and a key that states the reference.

- `waveform`: the canonical simulated-channel SNR. It is mean-square signal
  power over the explicitly recorded half-open reference sample interval,
  divided by mean-square real AWGN over the complete 0 Hz to Nyquist band. If
  the interval is omitted, it is the complete waveform passed to the channel.
- `in_band`: signal power divided by noise power integrated over the recorded
  audio-frequency band. This is appropriate for off-air measurements such as
  `scripts/measure_snr.py`; the band edges must accompany the number.
- `eb_n0`: energy per information bit divided by noise spectral density. The
  information bit rate must accompany it. This is for analytical comparisons,
  not the default channel control.

The reference interval matters because silence, PTT head padding, and waveform
tails change whole-buffer RMS. A sweep must use the same interval rule for all
points in a curve and record its bounds. It must also state whether radio/audio
filtering occurs before or after noise injection.

Decoder outputs currently named `snr_db`, `tone_snr_db`, and
`carrier_snr_db` remain waveform-specific quality estimates. They are useful
diagnostics, but are not measurements of injected channel SNR and must not be
used as its replacement. Trial documents therefore store them under
`decoder_metrics`, separately from `channel_measurements`.

## Trial result schema

`whale.trials.TrialRun` is schema version 2. The run records channel
configuration, master seed, creation time, and caller metadata. Each
`TrialResult` records:

- trial number, direction, global mode ID, and mode name;
- payload size, separate TX/RX sample counts and rates, and keyed duration;
- one of `decoded`, `acquisition_failed`, `payload_failed`, or `error`;
- channel measurements and decoder metrics in separate namespaces;
- optional capture path and error text.

`TrialRun.to_dict()` produces a JSON-compatible document and adds the total
and passed counts. Tools may add metadata, but must not rename or reinterpret
versioned fields. A future incompatible change increments `schema_version`.
`classify_decode()` applies the common boundary: an exact payload match is
decoded; otherwise missing or below-threshold confidence is an acquisition
failure; otherwise acquisition succeeded and the payload failed. Exceptions
raised by the encoder, channel, transport, or decoder are recorded as errors.
Non-finite unavailable diagnostics are serialized as JSON `null`.
For CPFSK qualification trials, bounded hard-decision evidence adds BER, total
and missing bit counts, and at most the first 128 error positions. Ordinary
decoder calls do not retain the underlying hard-bit vector.
