"""Registry and built-in push-to-talk backends.

Third-party packages register objects through the ``whalemodem.ptt_backends``
entry-point group. A backend has a name, capabilities, and an ``open`` method.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import inspect
import subprocess
from typing import Any, Mapping, Protocol, runtime_checkable

from . import ptt


@dataclass(frozen=True)
class PttCapabilities:
    acknowledgement: bool = False
    can_report_state: bool = False
    requires_explicit_keying: bool = True


@runtime_checkable
class PttController(Protocol):
    key_state_unknown: bool
    def key(self, on: bool) -> bool: ...
    def close(self) -> None: ...


@runtime_checkable
class PttBackend(Protocol):
    name: str
    capabilities: PttCapabilities
    def open(self, config: Mapping[str, Any]) -> PttController: ...


_BACKENDS: dict[str, PttBackend] = {}
_DISCOVERED = False


def register_backend(backend: PttBackend, *, replace: bool = False) -> None:
    if not backend.name or (backend.name in _BACKENDS and not replace):
        raise ValueError(f"PTT backend {backend.name!r} is already registered or invalid")
    _BACKENDS[backend.name] = backend


def discover_backends() -> None:
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    eps = metadata.entry_points()
    matches = eps.select(group="whalemodem.ptt_backends") if hasattr(eps, "select") else eps.get("whalemodem.ptt_backends", ())
    for entry_point in matches:
        candidate = entry_point.load()
        backend = candidate() if inspect.isclass(candidate) or not hasattr(candidate, "open") else candidate
        register_backend(backend)


def available_backends() -> dict[str, PttBackend]:
    discover_backends()
    return dict(_BACKENDS)


def open_backend(name: str, config: Mapping[str, Any] | None = None) -> PttController:
    discover_backends()
    if name not in _BACKENDS:
        raise ValueError(f"unknown PTT backend {name!r}; have {sorted(_BACKENDS)}")
    controller = _BACKENDS[name].open(config or {})
    if not isinstance(controller, PttController):
        raise TypeError(f"backend {name!r} returned an invalid PTT controller")
    return controller


def _required(config, key):
    if key not in config:
        raise ValueError(f"PTT backend requires {key!r}")
    return config[key]


class SerialLineBackend:
    name = "serial-line"
    capabilities = PttCapabilities()
    def open(self, config):
        return ptt.LinePtt(_required(config, "port"), line=config.get("line", "rts"),
                           baud=int(config.get("baud", 9600)),
                           active_high=bool(config.get("active_high", True)))


class IcomCivBackend:
    name = "icom-civ"
    capabilities = PttCapabilities(acknowledgement=True, can_report_state=True)
    def open(self, config):
        usb_id = config.get("usb_id")
        if isinstance(usb_id, str):
            usb_id = tuple(int(part, 16) for part in usb_id.split(":"))
        if not usb_id or len(usb_id) != 2:
            raise ValueError("icom-civ requires usb_id = 'VID:PID'")
        address = config.get("address")
        if isinstance(address, str):
            address = int(address, 0)
        return ptt.open_icom_ptt(tuple(usb_id), config.get("radio_name", "Icom"), address)


class VoxController:
    key_state_unknown = False
    def key(self, on: bool) -> bool: return True
    def close(self) -> None: pass


class VoxBackend:
    name = "vox"
    capabilities = PttCapabilities(requires_explicit_keying=False)
    def open(self, config): return VoxController()


class HamlibController:
    def __init__(self, command, timeout):
        self.command, self.timeout, self.key_state_unknown = command, timeout, False
    def key(self, on: bool) -> bool:
        try:
            subprocess.run([*self.command, "T", "1" if on else "0"], check=True,
                           capture_output=True, text=True, timeout=self.timeout)
        except Exception:
            self.key_state_unknown = True
            if on:
                raise
            return False
        self.key_state_unknown = False
        return True
    def close(self) -> None: self.key(False)


class HamlibBackend:
    name = "hamlib"
    capabilities = PttCapabilities(acknowledgement=True)
    def open(self, config):
        command = [str(config.get("executable", "rigctl"))]
        for option, flag in (("model", "-m"), ("device", "-r"), ("baud", "-s")):
            if option in config:
                command.extend((flag, str(config[option])))
        return HamlibController(command, float(config.get("timeout", 2.0)))


for _backend in (SerialLineBackend(), IcomCivBackend(), VoxBackend(), HamlibBackend()):
    register_backend(_backend)
