"""Configured radios: audio-device selection plus a pluggable PTT backend."""
from __future__ import annotations
from dataclasses import dataclass, field
import os
import re
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - compatibility for dev Python 3.10
    import tomli as tomllib
from typing import Any, Mapping
from . import audio_io, ptt
from .ptt_backends import PttCapabilities, available_backends, open_backend

@dataclass(frozen=True)
class Radio:
    name: str
    description: str
    audio_name: str
    ptt_backend: str
    ptt_config: Mapping[str, Any] = field(default_factory=dict)

    @property
    def capabilities(self) -> PttCapabilities:
        return available_backends()[self.ptt_backend].capabilities

    def devices(self):
        return (audio_io.find_device(self.audio_name, "output"), audio_io.find_device(self.audio_name, "input"))

    def ptt(self):
        return open_backend(self.ptt_backend, self.ptt_config)

@dataclass(frozen=True)
class RadioInventory:
    radios: dict[str, Radio]
    default: str | None = None

def _radio(name: str, value: Mapping[str, Any]) -> Radio:
    ptt_value = value.get("ptt", {})
    try:
        backend, audio_name = ptt_value["backend"], value["audio_name"]
    except (KeyError, TypeError):
        raise ValueError(f"radio {name!r} requires audio_name and ptt.backend") from None
    config = {key: item for key, item in ptt_value.items() if key != "backend"}
    return Radio(name, value.get("description", name), audio_name, backend, config)

def load_radios(path: str | os.PathLike[str]) -> RadioInventory:
    """Load ``[radios.NAME]`` tables and the optional ``default_radio`` key from a TOML inventory.

    ``default_radio``, if present, must name a key present in ``[radios.*]``;
    otherwise this raises ``ValueError``. If it is absent and the file
    defines exactly one radio, that radio is the implicit default (the
    common single-radio station case). If it is absent and the file defines
    more than one radio, ``RadioInventory.default`` is ``None`` -- callers
    must pass an explicit name.
    """
    with Path(path).open("rb") as stream:
        document = tomllib.load(stream)
    values = document.get("radios")
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{path} contains no [radios.NAME] tables")
    radios = {name: _radio(name, value) for name, value in values.items()}
    default = document.get("default_radio")
    if default is not None:
        if not isinstance(default, str):
            raise ValueError(f"{path}: default_radio must be a string, got {default!r}")
        if default not in radios:
            raise ValueError(f"{path}: default_radio {default!r} is not one of {sorted(radios)}")
    elif len(radios) == 1:
        default = next(iter(radios))
    return RadioInventory(radios, default)

# Compatibility inventory for the original bench. Installations should use
# an external file selected by --radio-config or WHALE_RADIO_CONFIG.
RADIOS = {
    "ic705": Radio("ic705", "IC-705 (VHF), CI-V PTT", "IC-705", "icom-civ", {"usb_id": "0C26:0036", "radio_name": "IC-705", "address": ptt.IC705_DEFAULT_ADDR}),
    "ic7300": Radio("ic7300", "IC-7300 (HF), CI-V PTT", "IC-7300", "icom-civ", {"usb_id": "10C4:EA60", "radio_name": "IC-7300", "address": ptt.IC7300_DEFAULT_ADDR}),
    "ht": Radio("ht", "HT via serial-interface RTS", "USB Audio Device", "serial-line", {"port": "COM5", "line": "rts"}),
}

def radio_inventory(path: str | os.PathLike[str] | None = None) -> RadioInventory:
    configured = path or os.environ.get("WHALE_RADIO_CONFIG")
    return load_radios(configured) if configured else RadioInventory(dict(RADIOS), default=None)

def get_radio(name: str | None, path: str | os.PathLike[str] | None = None) -> Radio:
    """Look up ``name`` in the selected inventory; ``None`` resolves to its default radio."""
    inventory = radio_inventory(path)
    if name is None:
        if inventory.default is None:
            raise ValueError("no radio given and no default_radio is configured")
        name = inventory.default
    try:
        return inventory.radios[name]
    except KeyError:
        raise ValueError(f"unknown radio {name!r}; have {sorted(inventory.radios)}") from None

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_string(key)

def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    raise TypeError(f"unsupported ptt_config value type for TOML output: {type(value)!r}")

def save_radios(path: str | os.PathLike[str], inventory: RadioInventory) -> None:
    """Write ``inventory`` to ``path`` as a fresh TOML file.

    This generates a new file from scratch -- it does not preserve comments
    or formatting from an existing file at ``path``.
    """
    lines: list[str] = []
    if inventory.default is not None:
        lines.append(f"default_radio = {_toml_string(inventory.default)}")
        lines.append("")
    for name, radio in inventory.radios.items():
        lines.append(f"[radios.{_toml_key(name)}]")
        lines.append(f"description = {_toml_string(radio.description)}")
        lines.append(f"audio_name = {_toml_string(radio.audio_name)}")
        lines.append(f"ptt.backend = {_toml_string(radio.ptt_backend)}")
        for key, value in radio.ptt_config.items():
            lines.append(f"ptt.{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")
    Path(path).write_text("\n".join(lines).rstrip("\n") + "\n")
