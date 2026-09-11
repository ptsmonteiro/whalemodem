# Whale

**An open HF and VHF data modem for amateur radio, up to 7.5 kbit/s — a
fresh alternative to VARA HF and VARA FM, driven through the same VARA TCP
interface your applications already speak.**

Whale is a small, readable Python implementation of a complete HF and
FM data modem: waveforms, FEC, ARQ link layer, PTT/CAT control, and a
VARA-shaped local TCP command/data interface.

## Why another modem?

VARA set the bar for amateur HF data, but it is closed source, paid, and
Windows-centric. Whale aims at the same job with different properties:

- **Open and inspectable.** Every waveform, FEC choice, and timing decision
  is in the repo, commented, and covered by tests.
- **Drop-in for existing apps.** Whale listens on the same two TCP ports and
  speaks the same line protocol, so a VARA-capable client can drive it.
- **Cross-platform and cheap.** Pure Python plus numpy/scipy, a sound card,
  and hamlib or a serial line for PTT. Runs on a Raspberry Pi.

Whale negotiates a mode at connect time and adapts speed mid-session as the
channel changes. Every shipped mode, with its rate and its pure-SNR,
Watterson-fading, and on-air pass points, is listed in
[docs/MODES.md](docs/MODES.md).

## Quick start

**Install.** Python 3.11 or newer:

```console
python -m venv .venv
# Activate .venv using the command for your shell.
python -m pip install -e ".[test]"
```

**Set up your radio.** The terminal configuration tool writes `radios.toml`,
browsing the audio devices, serial ports, and hamlib models it finds:

```console
whale-configure
```

**Run.** Start one server per radio, then point your VARA-capable
application at its command and data ports:

```console
whale-server --radio-config radios.toml --radio station-a \
    --mycall STA1 --cmd-port 8300 --data-port 8301
```

Read the [hardware and safety guide](docs/HARDWARE.md) before transmitting.
A [standalone build](docs/HARDWARE.md#standalone-builds) needs no Python
install on the station machine.

## Current status

Whale is in early development, but the full path already works: two stations
connect, transfer data in both directions with verification, and disconnect,
on HF and FM. The on-air protocol is native, so Whale talks to Whale.

## Contributing

Good places to start: extending VARA API coverage against fresh captures,
clear-channel assessment, new waveforms, and radio testing on paths and rigs
we do not have. Waveform work is measurable — add a mode, run the simulated
channel tests, and the numbers speak for themselves.

See [GOALS.md](GOALS.md) for what the project is aiming at,
[docs/TESTING.md](docs/TESTING.md) for the test workflows, and the
[documentation index](docs/README.md) for everything else.
