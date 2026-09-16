"""Application-wide configuration for whale."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from whale.hw.radios import Radio, _radio, _toml_key, _toml_string, _toml_value


DEFAULT_CONFIG = "config.toml"
DEFAULT_CMD_PORT = 8300
DEFAULT_DATA_PORT = 8301


@dataclass(frozen=True)
class Config:
    callsign: str
    ssid: int | None
    radios: dict[str, Radio]
    default_fm_radio: str | None = None
    default_hf_radio: str | None = None
    cmd_port: int = DEFAULT_CMD_PORT
    data_port: int = DEFAULT_DATA_PORT
    log_file: str | None = None

    @property
    def station_callsign(self) -> str:
        return self.callsign if self.ssid is None else f"{self.callsign}-{self.ssid}"

    def default_radio(self, channel: str) -> str | None:
        if channel == "fm":
            return self.default_fm_radio
        if channel == "hf":
            return self.default_hf_radio
        raise ValueError(f"unknown channel {channel!r}")


def _validate_station(callsign: Any, ssid: Any) -> tuple[str, int | None]:
    if not isinstance(callsign, str) or not callsign:
        raise ValueError("callsign must be a non-empty string")
    if callsign != callsign.upper() or not callsign.isascii() or not callsign.isalnum():
        raise ValueError("callsign must contain only uppercase ASCII letters and digits")
    if ssid is not None and (isinstance(ssid, bool) or not isinstance(ssid, int) or not 0 <= ssid <= 15):
        raise ValueError("ssid must be an integer between 0 and 15")
    effective = callsign if ssid is None else f"{callsign}-{ssid}"
    if len(effective) > 15:
        raise ValueError("callsign with SSID must be at most 15 characters")
    return callsign, ssid


def _default(document: dict[str, Any], key: str, radios: dict[str, Radio], channel: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if value not in radios:
        raise ValueError(f"{key} {value!r} is not one of {sorted(radios)}")
    if channel not in radios[value].channels:
        raise ValueError(f"{key} {value!r} does not support {channel!r}")
    return value


def _runtime_settings(document: dict[str, Any]) -> tuple[int, int, str | None]:
    cmd_port = document.get("cmd_port", DEFAULT_CMD_PORT)
    data_port = document.get("data_port", DEFAULT_DATA_PORT)
    for key, value in (("cmd_port", cmd_port), ("data_port", data_port)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValueError(f"{key} must be an integer between 1 and 65535")
    if cmd_port == data_port:
        raise ValueError("cmd_port and data_port must be different")
    log_file = document.get("log_file")
    if log_file is not None and (not isinstance(log_file, str) or not log_file.strip()):
        raise ValueError("log_file must be a non-empty string")
    return cmd_port, data_port, log_file


def load_config(path: str | os.PathLike[str]) -> Config:
    with Path(path).open("rb") as stream:
        document = tomllib.load(stream)
    callsign, ssid = _validate_station(document.get("callsign"), document.get("ssid"))
    values = document.get("radios")
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{path} contains no [radios.NAME] tables")
    radios = {id_: _radio(id_, value) for id_, value in values.items()}
    cmd_port, data_port, log_file = _runtime_settings(document)
    return Config(
        callsign, ssid, radios,
        _default(document, "default_fm_radio", radios, "fm"),
        _default(document, "default_hf_radio", radios, "hf"),
        cmd_port, data_port, log_file,
    )


def app_config(path: str | os.PathLike[str] | None = None) -> Config:
    return load_config(path or os.environ.get("WHALE_CONFIG") or DEFAULT_CONFIG)


def get_radio(name: str | None, channel: str, path: str | os.PathLike[str] | None = None) -> Radio:
    config = app_config(path)
    selected = name or config.default_radio(channel)
    if selected is None:
        raise ValueError(f"no radio given and no default_{channel}_radio is configured")
    try:
        radio = config.radios[selected]
    except KeyError:
        raise ValueError(f"unknown radio {selected!r}; have {sorted(config.radios)}") from None
    if channel not in radio.channels:
        raise ValueError(f"radio {selected!r} is not configured for channel {channel!r}")
    return radio


def save_config(path: str | os.PathLike[str], config: Config) -> None:
    callsign, ssid = _validate_station(config.callsign, config.ssid)
    if not config.radios:
        raise ValueError("at least one radio must be configured")
    # Apply the same cross-field validation as loading before replacing a file.
    document = {"default_fm_radio": config.default_fm_radio,
                "default_hf_radio": config.default_hf_radio}
    _default(document, "default_fm_radio", config.radios, "fm")
    _default(document, "default_hf_radio", config.radios, "hf")
    _runtime_settings({"cmd_port": config.cmd_port, "data_port": config.data_port,
                       "log_file": config.log_file})

    lines = [f"callsign = {_toml_string(callsign)}"]
    if ssid is not None:
        lines.append(f"ssid = {ssid}")
    if config.default_fm_radio is not None:
        lines.append(f"default_fm_radio = {_toml_string(config.default_fm_radio)}")
    if config.default_hf_radio is not None:
        lines.append(f"default_hf_radio = {_toml_string(config.default_hf_radio)}")
    if config.cmd_port != DEFAULT_CMD_PORT:
        lines.append(f"cmd_port = {config.cmd_port}")
    if config.data_port != DEFAULT_DATA_PORT:
        lines.append(f"data_port = {config.data_port}")
    if config.log_file is not None:
        lines.append(f"log_file = {_toml_string(config.log_file)}")
    lines.append("")
    for id_, radio in config.radios.items():
        lines.append(f"[radios.{_toml_key(id_)}]")
        lines.append(f"name = {_toml_string(radio.name)}")
        lines.append(f"audio.input = {_toml_string(radio.audio_input_name)}")
        lines.append(f"audio.output = {_toml_string(radio.audio_output_name)}")
        if radio.tx_level_db != 0:
            lines.append(f"audio.tx_level_db = {radio.tx_level_db:g}")
        channels = ", ".join(_toml_string(channel) for channel in sorted(radio.channels))
        lines.append(f"channels = [{channels}]")
        lines.append(f"ptt.backend = {_toml_string(radio.ptt_backend)}")
        for key, value in radio.ptt_config.items():
            lines.append(f"ptt.{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")
    Path(path).write_text("\n".join(lines).rstrip("\n") + "\n")
