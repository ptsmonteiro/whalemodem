# Documentation

The repository README is the project entry point. The documents below hold
the detailed contracts, operating procedures, evidence, and engineering
history.

## Direction and architecture

- [Project goals](../GOALS.md) defines the end state, success criteria, and
  architectural principles.
- [Link protocol](../LINK.md) specifies connection management, ARQ, mode
  negotiation, disconnect behavior, and the local TCP interface.
- [Modulation, coding, and framing](../FRAMING.md) specifies waveform
  contracts and every currently implemented mode.
- [Adaptive head timing](../ADAPTIVE_TIMING.md) specifies radio-turnaround
  calibration and leading-audio protection.

## Running and testing

- [Radio, audio, and PTT setup](HARDWARE.md) covers hardware inventory,
  station startup, PTT backends, diagnostics, and safety.
- [Testing and qualification](TESTING.md) covers the automated suite,
  simulated channels, capture replay, hardware progression, and acceptance
  scenario.
- [Simulated channels and trial records](../CHANNELS.md) defines channel
  models, presets, SNR conventions, and retained-result schemas.
- [Waveform mode qualification](../MODE_QUALIFICATION.md) defines evidence
  gates and audits the current modes.

## Measurements and experiments

- [Performance and engineering history](PERFORMANCE.md) retains benchmark
  results, unsuccessful approaches, design lessons, and open performance
  work.
- [`experiments/`](../experiments/) contains candidate waveforms and their
  local `README.md` and `RESULTS.md` evidence records.

Implementation lives in `whale/`, automated checks in `tests/`, and
measurement and hardware utilities in `scripts/`.
