# Project goals

## Vision

Whalemodem aims to become a high-performance, open amateur-radio data modem
for both VHF and HF. It should combine competitive over-the-air performance
with a modular architecture, broad hardware support, and continuous
end-to-end verification.

The end goal is a modem that can serve as a practical alternative to VARA FM
and VARA HF while remaining easy to inspect, test, extend, and integrate.

## Success criteria

### Competitive performance

Whalemodem should be competitive with VARA FM and VARA HF in both speed and
robustness under comparable channel conditions.

Performance work should consider more than nominal bit rate. Measurements
should include:

- Useful application throughput
- Connection setup time
- Round-trip time
- Frame and message delivery rate
- Recovery from corruption, fading, and lost acknowledgements
- Performance at different signal-to-noise ratios
- Behavior with asymmetric transmit and receive paths

Claims of competitiveness should ultimately be supported by reproducible
benchmarks over simulated audio channels and representative radio hardware.

### Adaptive radio timing

The modem should learn or measure the timing characteristics of each transmit
and receive chain and use them to minimize dead air without sacrificing
reliability.

This includes adapting to:

- PTT assertion and release delays
- Transmitter ramp-up and receiver recovery time
- Audio-device startup and buffering latency
- Leading and trailing audio clipping
- Radio turnaround time
- Direction-dependent behavior between two stations

The objective is to reduce total frame airtime and end-to-end round-trip time,
rather than relying indefinitely on conservative fixed delays.

### Compatible and extensible application interfaces

Whalemodem should expose a VARA-compatible API so existing applications can
use it without modem-specific integration work. Compatibility should be
defined and tested against the commands, events, connection behavior, and
data-stream semantics expected by real VARA clients.

Protocol operation must remain separate from application-facing adapters. The
architecture should make it straightforward to add other interfaces without
changing the physical or link layers, including:

- KISS TNC
- AGWPE
- Native library or socket APIs
- Future amateur-radio application interfaces

Each adapter should translate between its external contract and a shared,
transport-independent modem service API.

### Extensible modulation architecture

The physical layer should support multiple modulation and coding schemes
through explicit, stable interfaces. Adding a new waveform should not require
rewriting connection management, ARQ, application adapters, or radio control.

A modulation implementation should be able to define its own:

- Modulator and demodulator
- Symbol rate and occupied bandwidth
- Framing and synchronization requirements
- Error detection and forward-error correction
- Channel-quality measurements
- Payload and timing limits
- Capability identifiers used during negotiation

The link should be able to negotiate suitable modes and change modes as
channel conditions evolve.

### End-to-end testing from the beginning

End-to-end behavior should remain testable at every stage of development.
Tests should exercise the same public boundaries used in production rather
than validating only isolated algorithms.

The test strategy should include:

- Audio-to-audio tests through deterministic simulated channels
- Impairment tests for noise, fading, frequency error, clipping, latency, and
  dropped or corrupted frames
- Bidirectional connection, transfer, floor-control, and disconnect scenarios
- Hardware-in-the-loop tests using real audio devices, radios, and PTT
- Regression tests for every hardware or protocol failure that can be
  reproduced in software
- Repeatable performance benchmarks with recorded results

A complete release candidate should be able to connect, transfer verified
data in both directions, recover from expected channel faults, and disconnect
cleanly in both simulated and hardware test environments.

### Efficient operation on low-end hardware

Whalemodem should be practical to run on inexpensive, low-power computers,
including older Raspberry Pi-class systems and similar mini computers. A
dedicated modern desktop must not be required for normal modem operation.

The implementation should therefore aim for:

- Bounded CPU and memory use during long-running sessions
- Real-time audio processing without dropouts on the documented minimum
  hardware target
- Efficient streaming algorithms that avoid repeatedly processing unbounded
  audio buffers
- Predictable latency under sustained bidirectional traffic
- Minimal background services and deployment dependencies
- Headless operation and straightforward startup at boot
- Useful diagnostics that do not impose a large cost when disabled

Performance changes should be benchmarked on both development machines and a
representative low-end target. Tests should include long-running receive,
worst-case demodulation work, retransmissions, mode changes, and simultaneous
application and radio I/O. Supported hardware targets should be documented
with measured CPU load, memory use, and any profile or sample-rate limits.

### Broad PTT and radio-control support

Radio control should be isolated behind a common interface so the modem can
command the major PTT mechanisms used by amateur-radio stations.

The intended scope includes:

- Hamlib-controlled radios
- Serial-port control lines such as RTS and DTR
- CAT and vendor-specific serial protocols
- GPIO
- USB audio-interface PTT devices
- VOX where explicit PTT is unavailable
- Additional platform- or device-specific backends

Backends should expose their timing and capabilities to the modem where
possible, allowing the timing adaptation system to optimize each setup.

## Architectural principles

The project should preserve clear boundaries between:

```text
Application adapters
        ↓
Connection and stream service
        ↓
Link protocol, negotiation, and reliability
        ↓
Modulation, coding, and framing
        ↓
Audio transport, radio control, and PTT backends
```

These boundaries should allow each layer to be tested independently while
also supporting full end-to-end tests across all layers.

Design decisions should favor:

- Measured behavior over unexplained constants
- Negotiated capabilities over assumptions about the peer
- Per-direction adaptation where radio paths are asymmetric
- Reproducible evidence for performance and robustness changes
- Replaceable interfaces rather than dependencies on one waveform, radio, or
  application API
- Resource efficiency and predictable real-time behavior on low-end hardware
- Correctness and observability before optimization

## Definition of the end state

The project has reached its intended end state when a user can connect common
VHF or HF radio hardware, select an appropriate control backend, and use an
existing VARA-compatible application to communicate reliably without custom
integration. The modem should automatically select and adapt its operating
mode and radio timing, achieve performance competitive with the corresponding
VARA product, and demonstrate those results through repeatable audio-channel
and hardware end-to-end tests. It should do so on a documented low-end
mini-computer target, such as an older Raspberry Pi, without audio dropouts or
unbounded resource growth.

At that point, adding a new waveform, PTT backend, or application protocol
should be an extension through a defined interface rather than a redesign of
the modem.
