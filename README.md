# Whale

**Whale is an open-source, VARA API-compatible data modem for amateur radio,
working on HF and FM. It is fast, with modes up to 7.5 kbit/s, and uses its
own waveforms.**

Existing VARA-capable applications can use Whale through the same local TCP
interface. See [docs/MODES.md](docs/MODES.md) for the shipped modes and rates.

## Quick start

**Install.**

Linux:

```console
curl -fsSL https://raw.githubusercontent.com/ptsmonteiro/whalemodem/main/install.sh | bash
```

macOS:

```console
curl -fsSL https://raw.githubusercontent.com/ptsmonteiro/whalemodem/main/install.sh | bash
```

Windows (PowerShell):

```console
irm https://raw.githubusercontent.com/ptsmonteiro/whalemodem/main/install.ps1 | iex
```

Each installer verifies the release checksum, installs the latest release,
and leaves an existing configuration unchanged. Run it again to upgrade. A
new terminal may be needed before `whale-server`, `whale-configure` and
`whale-test` are on `PATH`. Python is not required.

**Configure Whale.** The first time you install Whale, run its terminal
configuration tool to set up the station:

```console
whale-configure
```

It has a station info screen -- callsign, optional SSID, ports -- and a list
of radios. At a minimum, add a radio (`a`), fill in its audio devices and PTT
backend, and use the `Test PTT` action in the form to key it for one second
and confirm it un-keys. Save (`s`) when done. See
[radio and PTT setup](docs/HARDWARE.md) for the radio fields and PTT
backends, and [radio audio setup](docs/HARDWARE.md#audio-level-tuning) for
tuning the transmit level (`l` on a radio from the main screen).

**Run.** Start one server per radio, then point your VARA-capable
application at its command and data ports:

```console
whale-server
```

Read the [hardware and safety guide](docs/HARDWARE.md) before transmitting.

## Help us test it on the air

`whale-test` runs a connect, a verified 4 KB transfer in each direction, and
a disconnect against another Whale station, without either operator starting
a server or picking a port. The answering station runs the first command,
the calling station the second:

```console
whale-test
whale-test CALLSIGN
```

Each side writes a `whale-report-...txt` file and prints its path -- please
share it if you hit a problem. Ctrl-C stops a run, says goodbye to the far
end and unkeys the radio; press it again to stop without the goodbye. See
[docs/TESTING.md](docs/TESTING.md) for more.

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
python -m whale.vara_server
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
