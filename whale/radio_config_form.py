"""Curses-free form model for creating and editing radio configurations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from whale.hw.radios import Radio


@dataclass(frozen=True)
class FormRow:
    """One field or action in the radio editor."""

    key: str
    label: str
    kind: str
    picker: str | None = None


BACKEND_ROWS: dict[str, tuple[FormRow, ...]] = {
    "vox": (),
    "serial-line": (
        FormRow("port", "Port", "text", picker="serial"),
        FormRow("line", "Line", "selector"),
        FormRow("baud", "Baud", "selector"),
        FormRow("active_high", "Active high", "bool"),
    ),
    "icom-civ": (
        FormRow("usb_id", "USB ID (VID:PID)", "text", picker="serial_icom"),
        FormRow("address", "Address", "text"),
    ),
    "hamlib": (
        FormRow("model", "Model", "text", picker="hamlib"),
        FormRow("device", "Device", "text", picker="serial"),
        FormRow("baud", "Baud", "selector"),
        FormRow("civaddr", "CI-V address", "text"),
        FormRow("timeout", "Timeout", "text"),
        FormRow("retry", "Retry", "text"),
    ),
}

BAUD_OPTIONS = ("", "300", "1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200")

SELECTOR_OPTIONS: dict[str, tuple[str, ...]] = {
    "line": ("rts", "dtr"),
    "baud": BAUD_OPTIONS,
}


def default_backend_values(backend: str) -> dict[str, Any]:
    """Return editable defaults for a PTT backend."""
    if backend == "serial-line":
        return {"port": "", "line": "rts", "baud": "", "active_high": True}
    if backend == "icom-civ":
        return {"usb_id": "", "address": ""}
    if backend == "hamlib":
        return {"model": "", "device": "", "baud": "", "civaddr": "", "timeout": "", "retry": ""}
    return {}


def values_from_config(backend: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """Convert persisted PTT configuration into editable form values."""
    values = default_backend_values(backend)
    if backend == "serial-line":
        if "port" in config:
            values["port"] = str(config["port"])
        if config.get("line") in ("rts", "dtr"):
            values["line"] = config["line"]
        if "baud" in config and str(config["baud"]) in BAUD_OPTIONS:
            values["baud"] = str(config["baud"])
        if "active_high" in config:
            values["active_high"] = bool(config["active_high"])
    elif backend == "icom-civ":
        for key in ("usb_id", "address"):
            if key in config:
                values[key] = str(config[key])
    elif backend == "hamlib":
        for key in ("model", "device", "civaddr", "timeout", "retry"):
            if key in config:
                values[key] = str(config[key])
        if "baud" in config and str(config["baud"]) in BAUD_OPTIONS:
            values["baud"] = str(config["baud"])
    return values


class RadioForm:
    """Editable radio state, independent of any terminal user interface."""

    BACKEND_ORDER = ["vox", "serial-line", "icom-civ", "hamlib"]
    _TOP_LEVEL_FIELDS = {
        "name", "audio_input_name", "audio_output_name",
        "channel_fm", "channel_hf",
    }

    def __init__(self, existing: tuple[str, Radio] | None, other_names: list[str]) -> None:
        self.old_name = existing[0] if existing else None
        self.other_names = list(other_names)

        radio = existing[1] if existing else None
        self.name = radio.id if radio else ""
        self.audio_input_name = radio.audio_input_name if radio else ""
        self.audio_output_name = radio.audio_output_name if radio else ""
        self.tx_level_db = radio.tx_level_db if radio else 0.0
        self.channel_fm = "fm" in radio.channels if radio else True
        self.channel_hf = "hf" in radio.channels if radio else False
        self.ptt_backend = radio.ptt_backend if (radio and radio.ptt_backend in self.BACKEND_ORDER) else "vox"

        self.backend_config = {
            name: default_backend_values(name) for name in self.BACKEND_ORDER
        }
        if radio is not None and radio.ptt_backend in self.backend_config:
            self.backend_config[radio.ptt_backend] = values_from_config(
                radio.ptt_backend, radio.ptt_config
            )

    def rows(self) -> list[FormRow]:
        rows = [
            FormRow("name", "Name", "text"),
            FormRow("audio_input_name", "Audio input", "device", picker="audio_input"),
            FormRow("audio_output_name", "Audio output", "device", picker="audio_output"),
            FormRow("channel_fm", "Channel: fm", "bool"),
            FormRow("channel_hf", "Channel: hf", "bool"),
            FormRow("ptt_backend", "PTT backend", "backend_selector"),
        ]
        rows.extend(BACKEND_ROWS[self.ptt_backend])
        rows.extend((FormRow("save", "Save", "action"), FormRow("cancel", "Cancel", "action")))
        return rows

    # Compatibility aliases used by the existing TUI and its callers.
    def _rows(self) -> list[FormRow]:
        return self.rows()

    def get_value(self, key: str) -> Any:
        if key == "ptt_backend":
            return self.ptt_backend
        if key in self._TOP_LEVEL_FIELDS:
            return getattr(self, key)
        return self.backend_config[self.ptt_backend][key]

    def _get_value(self, key: str) -> Any:
        return self.get_value(key)

    def set_value(self, key: str, value: Any) -> None:
        if key in self._TOP_LEVEL_FIELDS:
            setattr(self, key, value)
        else:
            self.backend_config[self.ptt_backend][key] = value

    def _set_value(self, key: str, value: Any) -> None:
        self.set_value(key, value)

    def cycle_backend(self) -> None:
        index = self.BACKEND_ORDER.index(self.ptt_backend)
        self.ptt_backend = self.BACKEND_ORDER[(index + 1) % len(self.BACKEND_ORDER)]

    def cycle_selector(self, key: str) -> None:
        options = SELECTOR_OPTIONS[key]
        try:
            index = options.index(self.get_value(key))
        except ValueError:
            index = -1
        self.set_value(key, options[(index + 1) % len(options)])

    def toggle_bool(self, key: str) -> None:
        self.set_value(key, not self.get_value(key))

    def build_radio(self) -> tuple[Radio | None, list[str]]:
        """Validate the form and return the configured radio or errors."""
        errors: list[str] = []
        name = self.name.strip()
        if not name:
            errors.append("name is required")
        elif name in self.other_names:
            errors.append(f"a radio named {name!r} already exists")

        audio_input_name = self.audio_input_name.strip()
        if not audio_input_name:
            errors.append("audio input is required")
        audio_output_name = self.audio_output_name.strip()
        if not audio_output_name:
            errors.append("audio output is required")

        channels = frozenset(
            channel for channel, selected in (("fm", self.channel_fm), ("hf", self.channel_hf))
            if selected
        )
        if not channels:
            errors.append("at least one channel (fm or hf) is required")

        ptt_config = self.build_ptt_config(errors)
        if errors:
            return None, errors
        return Radio(
            id=name,
            name=name,
            audio_input_name=audio_input_name,
            audio_output_name=audio_output_name,
            ptt_backend=self.ptt_backend,
            channels=channels,
            ptt_config=ptt_config,
            tx_level_db=self.tx_level_db,
        ), []

    def build_ptt_config(self, errors: list[str]) -> dict[str, Any]:
        """Serialize the selected backend, appending validation errors."""
        backend = self.ptt_backend
        values = self.backend_config[backend]
        config: dict[str, Any] = {}

        if backend == "serial-line":
            port = values["port"].strip()
            if not port:
                errors.append("port is required")
            else:
                config["port"] = port
            config["line"] = values["line"]
            config["baud"] = int(values["baud"]) if values["baud"] else 9600
            config["active_high"] = bool(values["active_high"])
        elif backend == "icom-civ":
            usb_id = values["usb_id"].strip()
            if not usb_id:
                errors.append("usb_id is required")
            else:
                config["usb_id"] = usb_id
            address_text = values["address"].strip()
            if address_text:
                try:
                    config["address"] = int(address_text, 0)
                except ValueError:
                    errors.append("address must be an integer, e.g. 164 or 0xA4")
        elif backend == "hamlib":
            model_text = values["model"].strip()
            if not model_text:
                errors.append("model is required")
            else:
                try:
                    config["model"] = int(model_text)
                except ValueError:
                    errors.append("model must be a whole number")
            device = values["device"].strip()
            if device:
                config["device"] = device
            if values["baud"]:
                config["baud"] = int(values["baud"])
            civaddr = values["civaddr"].strip()
            if civaddr:
                config["civaddr"] = civaddr
            timeout_text = values["timeout"].strip()
            if timeout_text:
                try:
                    config["timeout"] = float(timeout_text)
                except ValueError:
                    errors.append("timeout must be a number")
            retry_text = values["retry"].strip()
            if retry_text:
                try:
                    config["retry"] = int(retry_text)
                except ValueError:
                    errors.append("retry must be a whole number")
        return config

    def _build_ptt_config(self, errors: list[str]) -> dict[str, Any]:
        return self.build_ptt_config(errors)
