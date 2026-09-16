"""Configured radios: audio-device selection plus a pluggable PTT backend."""
from __future__ import annotations
from dataclasses import dataclass, field
import math
import os
import re
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - compatibility for dev Python 3.10
    import tomli as tomllib
from typing import Any, Mapping
from .ptt_backends import PttCapabilities, available_backends, open_backend

#: The channels (whale/policy.py's CHANNELS keys: "fm", "hf") a radio
#: may be selected for. Kept as a literal set rather than importing
#: whale.policy so this hardware-inventory module doesn't take a dependency
#: on the link-layer policy module; whale/vara_server.py checks the running
#: --channel against this set at startup.
VALID_CHANNELS = frozenset({"fm", "hf"})

@dataclass(frozen=True)
class Radio:
    id: str
    name: str
    audio_input_name: str
    audio_output_name: str
    ptt_backend: str
    channels: frozenset[str]
    ptt_config: Mapping[str, Any] = field(default_factory=dict)
    tx_level_db: float = 0.0

    @property
    def tx_level_linear(self) -> float:
        return 10.0 ** (self.tx_level_db / 20.0)

    @property
    def capabilities(self) -> PttCapabilities:
        return available_backends()[self.ptt_backend].capabilities

    def devices(self):
        from . import audio_io

        return (audio_io.find_device(self.audio_output_name, "output"), audio_io.find_device(self.audio_input_name, "input"))

    def ptt(self):
        config = self.ptt_config
        if self.ptt_backend == "icom-civ":
            # The icom-civ backend wants a human-readable label for its error
            # messages; reuse the radio's own name rather than asking for a
            # second, redundant one in ptt.* config.
            config = {**config, "radio_name": self.name}
        return open_backend(self.ptt_backend, config)

@dataclass(frozen=True)
class RadioInventory:
    radios: dict[str, Radio]
    default: str | None = None

def _radio(id_: str, value: Mapping[str, Any]) -> Radio:
    ptt_value = value.get("ptt", {})
    audio_value = value.get("audio", {})
    try:
        backend, audio_input, audio_output = ptt_value["backend"], audio_value["input"], audio_value["output"]
    except (KeyError, TypeError):
        raise ValueError(f"radio {id_!r} requires audio.input, audio.output, and ptt.backend") from None
    channels_value = value.get("channels")
    if not isinstance(channels_value, list) or not channels_value:
        raise ValueError(
            f"radio {id_!r} requires a non-empty channels list, one or more of "
            f"{sorted(VALID_CHANNELS)}")
    channels = frozenset(channels_value)
    if not channels <= VALID_CHANNELS:
        raise ValueError(
            f"radio {id_!r} has invalid channels {sorted(channels - VALID_CHANNELS)}; "
            f"must be a subset of {sorted(VALID_CHANNELS)}")
    config = {key: item for key, item in ptt_value.items() if key != "backend"}
    level = audio_value.get("tx_level_db", 0.0)
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not math.isfinite(level) or not -60 <= level <= 0:
        raise ValueError(f"radio {id_!r} audio.tx_level_db must be between -60 and 0 dB")
    return Radio(id_, value.get("name", id_), audio_input, audio_output, backend, channels, config, float(level))

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
    radios = {id_: _radio(id_, value) for id_, value in values.items()}
    default = document.get("default_radio")
    if default is not None:
        if not isinstance(default, str):
            raise ValueError(f"{path}: default_radio must be a string, got {default!r}")
        if default not in radios:
            raise ValueError(f"{path}: default_radio {default!r} is not one of {sorted(radios)}")
    elif len(radios) == 1:
        default = next(iter(radios))
    return RadioInventory(radios, default)

# Low-level radio helpers read the radio tables from the app configuration.
DEFAULT_RADIO_CONFIG = "config.toml"

def radio_inventory(path: str | os.PathLike[str] | None = None) -> RadioInventory:
    configured = path or os.environ.get("WHALE_CONFIG") or DEFAULT_RADIO_CONFIG
    return load_radios(configured)

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
    for id_, radio in inventory.radios.items():
        lines.append(f"[radios.{_toml_key(id_)}]")
        lines.append(f"name = {_toml_string(radio.name)}")
        lines.append(f"audio.input = {_toml_string(radio.audio_input_name)}")
        lines.append(f"audio.output = {_toml_string(radio.audio_output_name)}")
        if radio.tx_level_db != 0:
            lines.append(f"audio.tx_level_db = {radio.tx_level_db:g}")
        channels_literal = ", ".join(_toml_string(channel) for channel in sorted(radio.channels))
        lines.append(f"channels = [{channels_literal}]")
        lines.append(f"ptt.backend = {_toml_string(radio.ptt_backend)}")
        for key, value in radio.ptt_config.items():
            lines.append(f"ptt.{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")
    Path(path).write_text("\n".join(lines).rstrip("\n") + "\n")
