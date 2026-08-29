# whalemodem

`whalemodem` is a from-scratch amateur-radio data modem with a
**VARA-API-shaped** TCP interface: one command port and one data port. It
exchanges bytes between two stations over an analog radio link using its own
physical layer and stop-and-wait ARQ link layer.

The project is correctness-first. It does not drive VARA software and is not
wire-compatible with VARA. The familiar connect/data-stream/disconnect shape
is the application interface; everything on air is native to whalemodem.

## Status

The v1 acceptance target is implemented: connect two stations, send 1 KiB in
each direction, and disconnect at either station's request, with the received
payload verified byte-for-byte. The complete path is exercised by
`acceptance_test.py` and by software-only end-to-end tests.

This is experimental amateur-radio software, not a finished VARA replacement.
Its emphasis is a testable modem architecture, measured behavior on real
hardware, and waveform evolution without rewriting the link layer.

## Features

- VARA-shaped local command and byte-stream TCP ports
- Stop-and-wait ARQ with retransmission and session-scoped sequence numbers
- Independent mode negotiation and adaptation in each direction
- Adaptive leading-audio protection for radio turnaround and squelch recovery
- VHF FM and HF SSB channel policies
- Software channel simulation, capture replay, and real-radio test tools
- Extensible waveform and PTT backend interfaces

## Channels and modes

Mode availability is selected by channel policy and qualification level. The
normal `default` level currently preserves the historically shipped ladders.

| Channel | Control mode | Data modes |
| --- | --- | --- |
| `vhf-fm` | CPFSK 300 baud (mode 0) | CPFSK 300/600/1200 baud (0-2), VF3 DQPSK OFDM (3) |
| `hf-ssb` | HC0 non-coherent 16-FSK (5) | HC0 (5), HC1 DQPSK OFDM (4) |

Control traffic always uses the channel's robust control mode. DATA uses the
mode negotiated for that direction and steps down after trouble or up after a
clean streak. See [FRAMING.md](FRAMING.md) for waveform details and
[MODE_QUALIFICATION.md](MODE_QUALIFICATION.md) for the evidence required to
change a mode's availability.

## Requirements and installation

- Python 3.11 or newer
- NumPy, SciPy, sounddevice, and pyserial
- Two radios and suitable PTT/audio interfaces for on-air operation

Install the package and its dependencies in an isolated environment:

```console
python -m venv .venv
# Activate .venv using the command for your shell, then:
python -m pip install -e .
```

No radio hardware is needed to run the software test suite.

## Quick start without radios

Run the full TCP stack over paired in-memory audio transports:

```console
python -m pytest tests/test_audio_e2e.py -q
```

Run the complete automated suite:

```console
python -m pytest -q
```

Channel regressions are marked separately because they run a bounded matrix
of simulated channel points:

```console
python -m pytest -m channel_regression -q
```

See [docs/TESTING.md](docs/TESTING.md) for capture replay, simulation,
benchmarking, and hardware procedures.

## Running two radio stations

Copy `radios.example.toml`, describe each station's audio device and PTT
backend, and start one server per radio:

```console
python -m whale.vara_server --radio-config radios.toml --radio station-a \
  --mycall STA1 --cmd-port 8300 --data-port 8301

python -m whale.vara_server --radio-config radios.toml --radio station-b \
  --mycall STA2 --cmd-port 8310 --data-port 8311
```

Then drive the acceptance scenario:

```console
python acceptance_test.py \
  --a-cmd 8300 --a-data 8301 --b-cmd 8310 --b-data 8311 \
  --a-call STA1 --b-call STA2
```

Use `--channel hf-ssb` at both stations for the HF ladder. Use
`--mode-level optional` or `--mode-level experimental` only for an explicit
qualification run; those levels cumulatively enable less-qualified modes.

Radio configuration, supported PTT backends, safety expectations, and
hardware commands are documented in [docs/HARDWARE.md](docs/HARDWARE.md).

## Local TCP interface

The command port is line-oriented and uses carriage-return-terminated
commands:

```text
MYCALL <call>
LISTEN ON | OFF
CONNECT <mycall> <dstcall>
DISCONNECT | ABORT
```

The server reports `PTT ON`, `PTT OFF`, `CONNECTED`, `CONNECT FAILED`, and
`DISCONNECTED` status lines. Once connected, bytes written to the data port
are transmitted; received bytes are written back to the same TCP stream.

Compression modes, bandwidth selection, and Winlink-specific extensions are
not implemented. [LINK.md](LINK.md) is the complete protocol and local API
reference.

## Repository guide

| Path | Purpose |
| --- | --- |
| `whale/` | Modem, link, transport, channel policy, DSP, modes, and hardware integration |
| `tests/` | Unit, full-stack, simulated-channel, and capture-replay tests |
| `scripts/` | Benchmarks, qualification sweeps, hardware smoke tests, and diagnostics |
| `experiments/` | Candidate waveform implementations, measurements, and retained results |

The main documentation is:

- [GOALS.md](GOALS.md) — vision, success criteria, and architectural direction
- [LINK.md](LINK.md) — on-air link protocol, ARQ, negotiation, and local TCP API
- [FRAMING.md](FRAMING.md) — waveform contracts, modulation, coding, and framing
- [ADAPTIVE_TIMING.md](ADAPTIVE_TIMING.md) — calibrated radio-turnaround protection
- [CHANNELS.md](CHANNELS.md) — simulated channels, trial records, and SNR conventions
- [MODE_QUALIFICATION.md](MODE_QUALIFICATION.md) — mode evidence gates and availability
- [docs/PERFORMANCE.md](docs/PERFORMANCE.md) — measurements, design history, and open performance work
- [docs/TESTING.md](docs/TESTING.md) — test and qualification workflows
- [docs/HARDWARE.md](docs/HARDWARE.md) — radio, audio, and PTT setup

## Known limitations

- Only one frame is in flight at a time; the link is stop-and-wait rather
  than sliding-window.
- Lost DATA costs a complete chunk retransmission.
- An idle connected station sends no keepalive and is eventually timed out.
- HF clear-channel assessment is represented in policy but not implemented.
- Performance and hardware compatibility are still being characterized.

The intended end state and the reasons behind these tradeoffs are recorded in
[GOALS.md](GOALS.md).
