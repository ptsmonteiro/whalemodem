"""Single source of truth for the vendored-binary platform-tag scheme (used by whale/hw/hamlib.py, and by packaging/build tooling)."""
from __future__ import annotations

import platform


def platform_tag() -> str | None:
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin":
        return {"arm64": "macos-arm64", "x86_64": "macos-x86_64"}.get(machine)
    if system == "Linux":
        return {
            "x86_64": "linux-x86_64",
            "aarch64": "linux-aarch64",
            "armv7l": "linux-armv7",
        }.get(machine)
    if system == "Windows":
        return {"amd64": "windows-x86_64"}.get(machine)
    return None
