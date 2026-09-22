"""Platform-native locations for Whale's local configuration and state."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Mapping


CONFIG_NAME = "config.toml"
MODE_HISTORY_NAME = "mode-history.json"


def platform_config_dir(*, platform: str | None = None,
                        environ: Mapping[str, str] | None = None,
                        home: Path | None = None) -> Path:
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Whale"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Whale"
    return Path(environ.get("XDG_CONFIG_HOME", home / ".config")) / "whale"


def platform_state_dir(*, platform: str | None = None,
                       environ: Mapping[str, str] | None = None,
                       home: Path | None = None) -> Path:
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Whale"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Whale"
    return Path(environ.get("XDG_STATE_HOME", home / ".local" / "state")) / "whale"


def platform_config_path(**kwargs) -> Path:
    return platform_config_dir(**kwargs) / CONFIG_NAME


def config_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve an override, a local config, or the platform default."""
    selected = path or os.environ.get("WHALE_CONFIG")
    if selected:
        return Path(selected).expanduser()
    local = Path.cwd() / CONFIG_NAME
    if local.is_file():
        return local
    return platform_config_path()


def mode_history_path(effective_config: str | os.PathLike[str]) -> Path:
    """Keep custom/local configurations isolated from installed state."""
    config = Path(effective_config).expanduser().resolve()
    if config == platform_config_path().expanduser().resolve():
        return platform_state_dir() / MODE_HISTORY_NAME
    return config.with_name(config.name + ".mode-history.json")


def server_paths(path: str | os.PathLike[str] | None = None) -> tuple[Path, Path]:
    """Resolve the server's config and its associated mode history together."""
    config = config_path(path)
    return config, mode_history_path(config)
