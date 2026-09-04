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
ptt.model = 3073        # rig model number; see `rigctl -l`
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
