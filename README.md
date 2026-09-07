# whalemodem

Whalemodem is an experimental, open amateur-radio data modem for VHF and HF.
It has a VARA-shaped command/data interface, but its on-air protocol is native
and the local interface is not yet fully VARA-compatible. Detailed references
live in the [documentation index](docs/README.md).

## Project goals

The goal is a practical open alternative to VARA FM and VARA HF: competitive
useful throughput and reliability, compatibility with existing applications,
and operation on inexpensive low-power hardware. Application adapters, link
behavior, waveforms, audio transport, and radio control should remain modular
and independently testable, with performance demonstrated by reproducible
simulation and radio measurements. The full criteria are in
[GOALS.md](GOALS.md).

## Setup and run

Python 3.11 or newer is required:

```console
python -m venv .venv
# Activate .venv using the command for your shell.
python -m pip install -e ".[test]"
```

Run the full-stack software test or the complete automated suite without
radios:

```console
python -m pytest tests/test_audio_e2e.py -q
python -m pytest -q
```

For radio operation, copy `radios.example.toml` to `radios.toml`, configure
both stations, and start one server per radio:

```console
python -m whale.vara_server --radio-config radios.toml --radio station-a --mycall STA1 --cmd-port 8300 --data-port 8301
python -m whale.vara_server --radio-config radios.toml --radio station-b --mycall STA2 --cmd-port 8310 --data-port 8311
```

Exercise both directions with:

```console
python acceptance_test.py --a-cmd 8300 --a-data 8301 --b-cmd 8310 --b-data 8311 --a-call STA1 --b-call STA2
```

Read the [hardware and safety guide](docs/HARDWARE.md) before transmitting;
the [testing guide](docs/TESTING.md) covers the other test workflows. For a
station that shouldn't need a Python setup at all, see
[Standalone builds](docs/HARDWARE.md#standalone-builds).

## Current status

- The end-to-end connect, bidirectional byte transfer, verification, and
  disconnect path is implemented. Software full-stack tests cover VHF FM and
  HF SSB; retained captures and bench runs cover parts of both radio paths.
- Every shipped mode remains provisionally qualified. Waveform evidence gaps
  include retained-direction frame campaigns, per-frame net-throughput,
  and resource measurements. Ladder overlap/adaptation and complete-system
  hardware recovery are tracked separately.
- The local API is only the subset needed by the acceptance scenario, ARQ is
  stop-and-wait, and HF clear-channel assessment is missing. HF operation is
  therefore for controlled bench tests, not unattended use on a shared band.

See [MODE_QUALIFICATION.md](MODE_QUALIFICATION.md) for the evidence audit.

## Next steps

- Close waveform-qualification gaps with retained-direction hardware frames
  and per-frame net-throughput measurements; qualify ladder adaptation and
  complete-system hardware sessions independently.
- Add HF clear-channel assessment.
- Measure CPU, memory, latency, and audio dropouts on a low-end target.
- Expand and test VARA API compatibility, then improve throughput and radio
  turnaround against the [speed-ladder targets](SPEED_LADDERS.md).
