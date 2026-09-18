# Whale

**Whale is an open-source, VARA API-compatible data modem for amateur radio,
working on HF and FM. It is fast, with modes up to 7.5 kbit/s, and uses its
own waveforms.**

Existing VARA-capable applications can use Whale through the same local TCP
interface. See [docs/MODES.md](docs/MODES.md) for the shipped modes and rates.

## Quick start

**Install for station use (Linux or macOS).**

```console
curl -fsSL https://raw.githubusercontent.com/ptsmonteiro/whalemodem/main/install.sh | bash
```

On Windows, run `irm https://raw.githubusercontent.com/ptsmonteiro/whalemodem/main/install.ps1 | iex`
in PowerShell. The installer verifies the release checksum, installs the latest
release, and leaves existing `config.toml` files unchanged. Run it
again to upgrade. A new terminal may be needed before `whale-server` and
`whale-configure` are on `PATH`. Python is not required.

**Configure Whale.** The terminal configuration tool writes `config.toml`,
including the station callsign, channel defaults, and radios. It browses the
audio devices, serial ports, and hamlib models it finds:

```console
whale-configure
```

Highlight a transmitting radio in `whale-configure` and press `l` to measure
its input and tune its transmit level. `None` keeps the single-radio level
meter; another radio can receive the optional over-air test signal. See
[radio audio setup](docs/HARDWARE.md#audio-level-tuning).

**Run.** Start one server per radio, then point your VARA-capable
application at its command and data ports:

```console
whale-server --channel fm
```

Read the [hardware and safety guide](docs/HARDWARE.md) before transmitting.

## Current status

Whale is in early development, but the full path already works: two stations
connect, transfer data in both directions with verification, and disconnect,
on HF and FM. The on-air protocol is native, so Whale talks to Whale.

## Contributing

From a Python 3.11-or-newer checkout, install the runtime and test
dependencies:

```console
python -m venv .venv
# Activate .venv using the command for your shell.
python -m pip install -r requirements.txt pytest
```

Run Whale directly from the checkout:

```console
python -m whale.vara_server --channel fm --cmd-port 8300 --data-port 8301
python -m whale.config_tui
```

See [GOALS.md](GOALS.md) for what the project is aiming at,
[docs/TESTING.md](docs/TESTING.md) for the test workflows, and the
[documentation index](docs/README.md) for everything else.

## License

Copyright © 2026 Pedro Monteiro.

Whale is free software licensed under the GNU General Public License,
version 3 or later. See [LICENSE](LICENSE). Third-party components retain
their respective licenses and copyright notices.
