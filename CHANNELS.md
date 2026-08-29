# Simulated channels and trial records

This document defines the common boundary used to qualify waveforms and later
to impair full modem sessions. It complements [`FRAMING.md`](FRAMING.md): a
waveform owns modulation and decoding, while a channel owns everything that
happens to its transmitted audio before it reaches the peer.

## Channel contract

`whale.channel.AudioChannel` consumes a finite mono waveform at its declared
sample rate and returns `ChannelResult(audio, measurements)`. A channel may
change the sample count and retain state between calls. Consequently each
direction has its own instance; A-to-B and B-to-A never share fading state or
a random generator.

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
- `ChannelChain`, which applies stages in their listed order and namespaces
  their measurements by stage.

Stage order is part of the channel definition. In particular, noise before a
receive filter is filtered with the signal, while noise after it is not;
clipping before filtering produces a different spectrum from clipping after
filtering. `describe()` retains that order. The frequency and filter stages
retain state across calls and return to their initial state on `reset()`.
The sample-clock implementation uses a documented rational approximation to
the requested ratio and reports both values.

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

The radio-free end-to-end harness in `tests/test_audio_e2e.py` applies this
boundary at the 48 kHz capture rate. Its station-A transport owns the A-to-B
channel and its station-B transport owns the B-to-A channel. Channel output is
then passed through the same anti-aliased 48-to-12 kHz receive conversion used
by the modem. With no channel supplied, each direction gets a distinct
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

`whale.trials.TrialRun` is schema version 1. The run records channel
configuration, master seed, creation time, and caller metadata. Each
`TrialResult` records:

- trial number, direction, global mode ID, and mode name;
- payload size, TX/RX sample counts, sample rate, and keyed duration;
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
