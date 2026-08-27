"""Configured radios: audio-device selection plus a pluggable PTT backend."""
from __future__ import annotations
from dataclasses import dataclass, field
import os
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - compatibility for dev Python 3.10
    import tomli as tomllib
from typing import Any, Mapping
from . import audio_io, ptt
from .ptt_backends import PttCapabilities, PttTiming, available_backends, open_backend

@dataclass(frozen=True)
class Radio:
    name: str
    description: str
    audio_name: str
    ptt_backend: str
    ptt_config: Mapping[str, Any] = field(default_factory=dict)
    timing: PttTiming = field(default_factory=PttTiming)

    @property
    def capabilities(self) -> PttCapabilities:
        return available_backends()[self.ptt_backend].capabilities

    def devices(self):
        return (audio_io.find_device(self.audio_name, "output"), audio_io.find_device(self.audio_name, "input"))

    def ptt(self):
        return open_backend(self.ptt_backend, self.ptt_config)

def _radio(name: str, value: Mapping[str, Any]) -> Radio:
    ptt_value, timing_value = value.get("ptt", {}), value.get("timing", {})
    try:
        backend, audio_name = ptt_value["backend"], value["audio_name"]
    except (KeyError, TypeError):
        raise ValueError(f"radio {name!r} requires audio_name and ptt.backend") from None
    config = {key: item for key, item in ptt_value.items() if key != "backend"}
    return Radio(name, value.get("description", name), audio_name, backend, config,
                 PttTiming(float(timing_value.get("lead", .22)), float(timing_value.get("tail", .05))))

def load_radios(path: str | os.PathLike[str]) -> dict[str, Radio]:
    """Load ``[radios.NAME]`` tables from a TOML inventory."""
    with Path(path).open("rb") as stream:
        document = tomllib.load(stream)
    values = document.get("radios")
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{path} contains no [radios.NAME] tables")
    return {name: _radio(name, value) for name, value in values.items()}

# Compatibility inventory for the original bench. Installations should use
# an external file selected by --radio-config or WHALE_RADIO_CONFIG.
RADIOS = {
    "ic705": Radio("ic705", "IC-705 (VHF), CI-V PTT", "IC-705", "icom-civ", {"usb_id": "0C26:0036", "radio_name": "IC-705", "address": ptt.IC705_DEFAULT_ADDR}),
    "ic7300": Radio("ic7300", "IC-7300 (HF), CI-V PTT", "IC-7300", "icom-civ", {"usb_id": "10C4:EA60", "radio_name": "IC-7300", "address": ptt.IC7300_DEFAULT_ADDR}),
    "ht": Radio("ht", "HT via serial-interface RTS", "USB Audio Device", "serial-line", {"port": "COM5", "line": "rts"}),
}

def radio_inventory(path: str | os.PathLike[str] | None = None) -> dict[str, Radio]:
    configured = path or os.environ.get("WHALE_RADIO_CONFIG")
    return load_radios(configured) if configured else dict(RADIOS)

def get_radio(name: str, path: str | os.PathLike[str] | None = None) -> Radio:
    inventory = radio_inventory(path)
    try:
        return inventory[name]
    except KeyError:
        raise ValueError(f"unknown radio {name!r}; have {sorted(inventory)}") from None
