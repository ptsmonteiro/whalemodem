# Radio, audio, and PTT setup

This document covers real-radio operation. Start with the software-only tests
in [TESTING.md](TESTING.md) before keying a transmitter.

## Radio inventory

Hardware is selected from a TOML inventory. Copy `radios.example.toml`, edit
it for the local station, and pass it with `--radio-config PATH`, or set
`WHALE_RADIO_CONFIG`. Each entry names an audio-device substring and one PTT
backend with its backend-specific settings.

```toml
[radios.station-a]
description = "Icom controlled over CI-V"
audio_name = "IC-705"
ptt.backend = "icom-civ"
ptt.usb_id = "0C26:0036"
ptt.radio_name = "IC-705"
ptt.address = 0xA4
```

The server's `--radio` value is the inventory key (`station-a` above), not an
audio-device name. When no inventory is selected, the legacy `ic705`,
`ic7300`, and `ht` definitions remain available for the original bench.

An optional top-level `default_radio = "station-a"` key, placed before any
`[radios.*]` table, names the radio to use when none is given explicitly. A
file with only one `[radios.*]` table defaults to it implicitly even
without `default_radio`.

### `whalemodem-configure`

```console
whalemodem-configure --radio-config radios.toml
```

A curses terminal UI for the inventory file: list, add, edit, set the
default, and delete radios. Point it at `--radio-config PATH` or set
`WHALE_RADIO_CONFIG`; with neither, it defaults to `radios.toml` in the
current directory. Pointing it at a path that doesn't exist yet starts from
an empty inventory (nothing is written until you save); pointing it at an
existing-but-malformed file exits with an error rather than overwriting it.

List keys: `↑`/`↓` or `j`/`k` to move, `a` to add a radio, `Enter` to edit
the selected one, `d` to set it as default, `x` (or Delete) to remove it
(with a `y`/`n` confirmation), `s` to save, `q` to quit (prompting to save
first if there are unsaved changes).

Add/edit form: `↑`/`↓` or `j`/`k` to move between fields, `Enter` to start
editing a text field or cycle a selector/toggle field, `Space` also cycles
a selector/toggle field, `Esc` to cancel (a mid-edit `Esc` reverts just that
field; from the form itself it discards the whole add/edit and returns to
the list). The Audio name field shows a live match count against connected
devices and, along with the serial-port and hamlib-model fields underneath
whichever PTT backend is selected, supports `p` to pick from the real
hardware/data instead of typing blind -- typing the value by hand always
works too, including when no sound card, serial port, or libhamlib is
available to browse. Select `Save` to validate and write the entry back
into the in-memory inventory (still not on disk until `s` on the list
view), or `Cancel` to discard the form.

## Audio backend

Radios' USB sound cards are opened through whichever PortAudio host API sits
closest to the hardware on the running OS, rather than a higher-level
shared-mixer API that adds latency and jitter:

| OS | Host API used | Avoided |
| --- | --- | --- |
| Windows | WASAPI | MME, DirectSound |
| macOS | Core Audio | -- |
| Linux | ALSA | PulseAudio, JACK |

`audio_name` in the inventory is matched against device names *within* that
host API, so it must be a substring PortAudio reports for the card under
that API specifically (check with `python -m sounddevice`).

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
through the `whalemodem.ptt_backends` Python entry-point group. A backend
implements `PttBackend` from `whale.hw.ptt_backends`; embedded applications
may also call `register_backend()` directly.

### `hamlib`

Binds directly to `libhamlib` via ctypes (`whale/hw/hamlib.py`) and keeps one
`RIG*` handle open for the life of the backend, rather than shelling out to
`rigctl` per PTT toggle -- a process spawn plus a fresh rig handshake on every
key() is dead air this project's adaptive-timing goals are meant to remove.

**No separate hamlib install is needed.** Prebuilt libhamlib (+ libusb)
binaries are vendored under `whale/hw/_vendor/hamlib/` for macOS
(arm64/x86_64), Linux (x86_64/aarch64/armv7), and Windows (x86_64); the
loader picks the right one for the running platform automatically. Sources,
exact versions, and license texts are in
`whale/hw/_vendor/hamlib/SOURCES.md`; `scripts/vendor_hamlib.py` refreshes
them for a hamlib version bump. On an unlisted platform (or if the bundled
copy fails to load), it falls back to a system install
(`brew install hamlib` / `apt install libhamlib4`). Set
`WHALEMODEM_SYSTEM_HAMLIB=1` to force the system search even where a bundled
copy exists -- e.g. to pick up a rig model added to hamlib after the
vendored version.

```toml
[radios.rigctl]
audio_name = "USB Audio CODEC"
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
at all, whalemodem can be frozen into a standalone, no-Python-required
onedir bundle with PyInstaller -- a folder containing the
`whalemodem-server` executable plus its own Python runtime, numpy/scipy,
and (vendored the same way `hamlib` is vendored above) hamlib and, on
Linux, PortAudio. It is a folder you download and run directly, not yet an
installer, system package, or service -- nothing registers it to start on
boot, and there is no upgrade mechanism beyond replacing the folder. The
same command-line flags shown under
[Starting a station](#starting-a-station) apply, just against the frozen
executable instead of `python -m whale.vara_server`:

```console
whalemodem-server/whalemodem-server --radio-config radios.toml --radio station-a \
  --mycall STA1 --cmd-port 8300 --data-port 8301
```

Building one is covered in `packaging/pyinstaller/README.md`; that
procedure, not this section, is the source of truth for the actual build
steps. In short: install the build-only `pyinstaller` dependency, then run
`pyinstaller packaging/pyinstaller/whalemodem.spec` from the repo root. The
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

**Validation status.** Only linux-x86_64 has actually been built and
exercised so far, inside Docker, without real audio or rig hardware
attached: `whalemodem-server --help` running to completion plus native
import/load checks for `whale.hw.hamlib` and `whale.hw.audio_io`, not a
full radio session. The other five platforms (linux-aarch64, linux-armv7,
macos-arm64, macos-x86_64, windows-x86_64) are built by the
`.github/workflows/standalone-builds.yml` CI matrix but have not yet run
on real GitHub Actions or on real hardware. As with every mode covered by
this project's [current status](../README.md#current-status),
"builds and imports cleanly" is not the same claim as "verified" --
standalone builds for any platform other than the one smoke-tested here
should be treated as unvalidated until they have actually run on that
target OS/architecture, and the Linux/Raspberry-Pi-class path specifically
still needs a real audio-plus-rig hardware pass before it is used for
anything beyond a bench trial.

## Starting a station

For VHF FM:

```console
python -m whale.vara_server --radio-config radios.toml --radio station-a \
  --mycall STA1 --cmd-port 8300 --data-port 8301 --channel vhf-fm
```

For HF SSB, both peers must select the HF policy:

```console
python -m whale.vara_server --radio-config radios.toml --radio station-a \
  --mycall STA1 --cmd-port 8300 --data-port 8301 --channel hf-ssb
```

The channel selects local timeouts, retry policy, useful-keying budget, and
the offered waveform ladder. It is not an on-air field. Mode IDs are
negotiated, but operators should still configure both ends for the actual
channel.

`--mode-level` defaults to `default`. The `optional` and `experimental`
levels are for deliberate qualification runs; see
[MODE_QUALIFICATION.md](../MODE_QUALIFICATION.md).

## Hardware checks

With both radios connected and tuned to the same frequency, proceed from the
smallest test to the full link:

```console
python scripts/hw_smoke_single_frame.py
python scripts/hw_smoke_link.py
python scripts/sweep_modes.py --channel vhf-fm
```

For the original HF bench:

```console
python scripts/hw_hf_frames.py --mode hc0
python scripts/sweep_modes.py --channel hf-ssb
python scripts/run_acceptance_test.py --channel hf-ssb \
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
