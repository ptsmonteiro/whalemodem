# Radio, audio, and PTT setup

This document covers real-radio operation. Start with the software-only tests
in [TESTING.md](TESTING.md) before keying a transmitter.

## Application configuration

Copy `config.example.toml`, edit it for the local station, and pass it with
`--config PATH`, or set `WHALE_CONFIG`. The top-level station settings and
channel defaults precede the radio tables. Each radio names an audio input device, an audio
output device, the `--channel` values it may be selected for, and one PTT
backend with its backend-specific settings.

```toml
callsign = "STA1"
ssid = 2
default_fm_radio = "station-a"
default_hf_radio = "station-a"
cmd_port = 8300
data_port = 8301
log_file = "whale.log"

[radios.station-a]
name = "Icom controlled over CI-V"
audio.input = "IC-705"
audio.output = "IC-705"
channels = ["fm", "hf"]
ptt.backend = "icom-civ"
ptt.usb_id = "0C26:0036"
ptt.address = 0xA4
```

`channels` is a non-empty list drawn from the same names `--channel` takes
(`fm`, `hf`, see [Starting a station](#starting-a-station)) -- a
radio may support one or both. `--radio` (below) is refused at startup if
the named radio's `channels` doesn't include the selected `--channel`.

The server's optional `--radio` value is the radio key (`station-a` above),
not an audio-device name. Without it, `default_fm_radio` or
`default_hf_radio` is selected according to `--channel`. `--config` and
`WHALE_CONFIG` default to `config.toml` in the current directory.
The API ports default to 8300 and 8301 when omitted. Logging defaults to
stderr; set `log_file` to write logs to a file. The corresponding command-line
options remain one-run overrides.

### `whale-configure`

```console
whale-configure --config config.toml
```

A curses terminal UI for the application configuration: edit the station
callsign, optional SSID, API ports, and log file; choose FM and HF defaults;
and add, edit, or delete radios. Point it at `--config PATH` or set `WHALE_CONFIG`; with
neither, it defaults to `config.toml` in the current directory. Pointing it
at a path that doesn't exist yet starts from an empty configuration (nothing is written until you save); pointing it at an
existing-but-malformed file exits with an error rather than overwriting it.

Main-screen keys: `↑`/`↓` or `j`/`k` to move, `a` to add a radio, `Enter` to edit
the selected setting or radio, `l` to tune the selected radio's transmit
level. The optional second selection chooses another radio to receive the
over-air test signal; choosing `None` keeps the selected radio's normal local
input meter, while probe-quality analysis remains unavailable. Use `x` (or Delete) to remove
the selected radio (with a `y`/`n` confirmation), `s` to save, and `q` to quit (prompting to save
first if there are unsaved changes). A level applied by the embedded tuner
updates the selected transmitter's working configuration and remains unsaved
until `s` is pressed on the main screen.

Add/edit form: `↑`/`↓` or `j`/`k` to move between fields, `Enter` to start
editing a text field or cycle a selector/toggle field, `Space` also cycles
a selector/toggle field, `Esc` to cancel (a mid-edit `Esc` reverts just that
field; from the form itself it discards the whole add/edit and returns to
the list). Channel: fm and Channel: hf are independent toggles; at
least one must be on and `Save` refuses to proceed otherwise. The Audio input and Audio output fields are picker-only -- there
is no manual typing for these two, `Enter` or `p` always opens a list of the
currently connected input/output devices to choose from. If the stored
device for either field is not among the currently connected devices (radio
powered off, USB unplugged, etc), the field shows a `[not found]` marker
next to it; this is informational only and never blocks `Save`. The
port, USB ID, and model fields underneath whichever PTT backend is selected
are picker-only too whenever `p` (or `Enter`) can find at least one serial
port or hamlib model to offer -- no manual typing in that case either.
Manual typing is only available as a fallback when the picker comes up
empty or unavailable (no serial port currently connected, or no libhamlib
on this machine), e.g. when pre-filling an inventory for a station you
aren't physically sitting at. Select `Save` to validate and write the entry
back into the in-memory inventory (still not on disk until `s` on the list
view), or
`Cancel` to discard the form.

### `whale-levels`

From the dev tree, run `python -m whale.level_tui --radio ht` (or add
`--config PATH`) with the modem server stopped. Only the configured
Digirig and HT are needed. Open squelch on an unused channel and slowly raise
the HT volume while watching the live two-second input peak/RMS meter. Keep
clipped samples at zero and leave headroom below 0 dBFS. Open-squelch noise
is a rough input check; a received data signal can have a different level.
If available, adjust the PC's recording gain only if it acts before the
sound-card ADC. Digital attenuation cannot repair an already clipped input.

When no TX attenuation is configured, the tuner starts at -24 dB rather than
full PC output. Press `+`/`-` to change it by 1 dB. Press `t` to key a
repeatable 500--3000 Hz multicarrier probe on a 50 Hz grid for at most 60
seconds, and `t` again to stop. Interleaved grid carriers are empty. The
displayed TX peak is the PC's digital output level, not the level or
distortion at the radio mic port. Add `--receive-radio NAME` to take input
from another configured transceiver. While the probe is received, the tuner
shows occupied-carrier flatness, empty-carrier leakage, and EVM after removing
clock error and smooth linear passband response. These describe the complete
receiver, link, and sound-card path. The measurement starts
enabled with `--receive-radio`, starts disabled otherwise, and `d` toggles it.
Press `s` to store the chosen
`audio.tx_level_db` (between -60 and 0 dB) in `config.toml`; regular modem
transmissions then use that attenuation. Saving rewrites the inventory using
the same writer as `whale-configure`. `q` exits and unkeys any active test.
Verify the final setting with an actual fast-data exchange when possible.

## Audio backend

Radios' USB sound cards are opened through whichever PortAudio host API sits
closest to the hardware on the running OS, rather than a higher-level
shared-mixer API that adds latency and jitter:

| OS | Host API used | Avoided |
| --- | --- | --- |
| Windows | WASAPI | MME, DirectSound |
| macOS | Core Audio | -- |
| Linux | ALSA | PulseAudio, JACK |

`audio.input`/`audio.output` in the inventory are each matched against
device names *within* that host API, so they must be a substring PortAudio
reports for the card under that API specifically (check with
`python -m sounddevice`, or use `whale-configure`'s picker, which
lists them for you and writes the exact name back).

Set `WHALE_AUDIO_HOST_API` to override the default -- for example, a Linux
station that must go through PulseAudio or JACK instead of raw ALSA (a USB
card already claimed by another process, or a station mixing radio audio
with other sources), or a Windows card that will not open under WASAPI and
needs MME or DirectSound instead. The value is matched as a
case-insensitive substring against `python -m sounddevice`'s host API names.

Linux additionally needs PortAudio's own shared library installed
(`apt install libportaudio2` or equivalent); the `sounddevice` wheel bundles
it on Windows and macOS but not on Linux.

## PTT backends

| Backend | Use |
| --- | --- |
| `icom-civ` | Icom CI-V control |
| `serial-line` | RTS or DTR on a serial interface |
| `hamlib` | Hamlib-supported rig control |
| `vox` | Audio-triggered transmit control |

External packages can register GPIO, CAT, USB-interface, or other backends
through the `whale.ptt_backends` Python entry-point group. A backend
implements `PttBackend` from `whale.hw.ptt_backends`; embedded applications
may also call `register_backend()` directly.

### `hamlib`

Binds directly to `libhamlib` via ctypes (`whale/hw/hamlib.py`) and keeps one
`RIG*` handle open for the life of the backend, rather than shelling out to
`rigctl` per PTT toggle -- a process spawn plus a fresh rig handshake on every
key() only adds avoidable dead air.

**No separate hamlib install is needed.** Prebuilt libhamlib (+ libusb)
binaries are vendored under `whale/hw/_vendor/hamlib/` for macOS
(arm64/x86_64), Linux (x86_64/aarch64/armv7), and Windows (x86_64); the
loader picks the right one for the running platform automatically. Sources,
exact versions, and license texts are in
`whale/hw/_vendor/hamlib/SOURCES.md`; `scripts/vendor_hamlib.py` refreshes
them for a hamlib version bump. On an unlisted platform (or if the bundled
copy fails to load), it falls back to a system install
(`brew install hamlib` / `apt install libhamlib4`). Set
`WHALE_SYSTEM_HAMLIB=1` to force the system search even where a bundled
copy exists -- e.g. to pick up a rig model added to hamlib after the
vendored version.

```toml
[radios.rigctl]
audio.input = "USB Audio CODEC"
audio.output = "USB Audio CODEC"
ptt.backend = "hamlib"
ptt.model = 3073        # rig model number; see `rigctl -l` (or hamlib.list_rig_models())
ptt.device = "/dev/ttyUSB0"
ptt.baud = 115200
# ptt.civaddr = 148      # Icom rigs only
# ptt.timeout = 2.0      # seconds; also bounds hamlib's internal retry loop
# ptt.retry = 3
# ptt.conf = { ptt_type = "RTS" }   # any other rig_token_lookup() token
```

`ptt.conf` is a passthrough to `rig_set_conf()`, the same mechanism behind
`rigctl -C`; run `rigctl -m <model> -L` to see every token a given rig
supports (port options, `ptt_type` for radios keyed via a control line
instead of CAT, etc).

## Standalone builds

For an end user who doesn't want to set up Python, a venv, or `pip install`
at all, Whale can be frozen into a standalone, no-Python-required
onedir bundle with PyInstaller -- a folder containing the
`whale-server` and `whale-configure` executables plus their shared Python
runtime, numpy/scipy, and (vendored the same way `hamlib` is vendored
above) hamlib and, on Linux, PortAudio. It is a folder you download and
run directly, not yet an installer, system package, or service -- nothing
registers it to start on boot, and there is no upgrade mechanism beyond
replacing the folder. The same command-line flags shown under
[Starting a station](#starting-a-station) apply, just against the frozen
executable instead of `python -m whale.vara_server`:

```console
whale/whale-server --config config.toml --channel fm \
  --cmd-port 8300 --data-port 8301
```

`whale/whale-configure` is the frozen configuration TUI, in the same folder.

Building one is covered in `packaging/pyinstaller/README.md`; that
procedure, not this section, is the source of truth for the actual build
steps. In short: install the build-only `pyinstaller` dependency, then run
`pyinstaller packaging/pyinstaller/whale.spec` from the repo root. The
build must run natively on each target OS/architecture -- no
cross-compilation -- since the spec bundles the build host's own vendored
hamlib (and, on Linux, PortAudio) binaries.

**Residual dependency on Linux.** Freezing removes the Python interpreter,
pip, and the need for a system hamlib/PortAudio install, but it does not
make a Linux bundle dependency-free: the vendored `libportaudio.so.2`
(Debian's own build) itself dynamically loads ALSA's `libasound.so.2` and
JACK's `libjack.so.0` at runtime, confirmed via `readelf -d` and
`apt-cache depends libportaudio2` against the vendored package. A
standalone Linux build therefore still needs `libasound2` (`libasound2t64`
on Debian trixie and current Raspberry Pi OS) and `libjack-jackd2-0` (or
equivalent) present on the target system -- these are not vendored and are
not eliminated by freezing. Both packages ship by default on essentially
any Linux with a working audio stack, including Raspberry Pi OS, but this
is a genuine gap against "no dependencies at all," not a cosmetic one, and
should be checked for on a bare or minimal target rather than assumed.
macOS and Windows builds have no equivalent gap: the `sounddevice` wheel
already bundles PortAudio itself on those platforms.

**Validation status.** linux-x86_64 (inside Docker) and windows-x86_64
(native, on a real Windows host) have actually been built and exercised so
far, without real audio or rig hardware attached: `whale-server --help` and
`whale-configure --help` (`.exe` on Windows) both running to completion,
plus native import/load checks for `whale.hw.hamlib` and
`whale.hw.audio_io`, not a full radio session. The other four platforms
(linux-aarch64, linux-armv7,
macos-arm64, macos-x86_64) are built by the
`.github/workflows/standalone-builds.yml` CI matrix but have not yet run
on real GitHub Actions or on real hardware. As with every mode covered by
this project's [current status](../README.md#current-status),
"builds and imports cleanly" is not the same claim as "verified" --
standalone builds for any platform other than the two smoke-tested here
should be treated as unvalidated until they have actually run on that
target OS/architecture, and every platform (Windows included) still needs
a real audio-plus-rig hardware pass before it is used for anything beyond
a bench trial.

## Starting a station

For FM:

```console
python -m whale.vara_server --config config.toml \
  --cmd-port 8300 --data-port 8301 --channel fm
```

For HF, both peers must select the HF policy:

```console
python -m whale.vara_server --config config.toml \
  --cmd-port 8300 --data-port 8301 --channel hf
```

The channel selects local timeouts, retry policy, useful-keying budget, and
the offered waveform ladder. It is not an on-air field. Mode IDs are
negotiated, but operators should still configure both ends for the actual
channel.

`--mode-level` defaults to `default`. The `optional` and `experimental`
levels are for deliberate qualification runs; see
[MODES.md](MODES.md).

## Hardware checks

With both radios connected and tuned to the same frequency, proceed from the
smallest test to the full link:

```console
python scripts/hw_smoke_single_frame.py
python scripts/hw_smoke_link.py
python scripts/sweep_modes.py --channel fm
```

For the original HF bench:

```console
python scripts/hw_hf_frames.py --mode hc0
python scripts/sweep_modes.py --channel hf
python scripts/run_acceptance_test.py --channel hf \
  --a-radio ic7300 --b-radio ic705 --size 1024
```

The shared sweep method bypasses link ARQ and performs direct
modulate → transmit → capture → demodulate trials. Characterization normally
probes both directions. Qualification retains one declared direction per
radio pair; the better usable direction may be selected before the
promotion-sized run. Bidirectional behavior is still required from the full
hardware link/ARQ/recovery session. `scripts/bench.py` contains the common
radio-pair and trial machinery.

## Capture diagnostics

Set `WHALE_CAPTURE_DIR` to save the audio behind near-miss decodes. These
captures are intended for offline replay and decoder diagnosis; do not commit
large or station-specific recordings without deciding that they are stable
regression fixtures.

## Safety

- Use a dummy load or an authorized frequency where appropriate.
- Confirm callsign, band, mode, power, duty cycle, and local regulations
  before automated transmissions.
- Verify that PTT releases on normal exit and failure before long sweeps.
- Begin at low power and conservative audio levels.
- Treat less-qualified modes as qualification work, not an assurance that
  they are appropriate for an unattended station.

The PTT safety behavior is covered by `tests/test_ptt_safety.py`; it does not
replace station-level fail-safe testing.
