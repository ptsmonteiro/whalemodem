"""Persistent per-path memory of the fastest DATA mode that has worked."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading


class ModeHistory:
    """A small, atomically written mode history keyed by directed radio path."""

    def __init__(self, path: str | os.PathLike[str], namespace: str = "default"):
        self.path = Path(path)
        self.namespace = namespace
        self._lock = threading.Lock()
        self._values: dict[tuple[str, str], int] = {}
        self._load()

    def _load(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            self._values = {
                (entry["local"], entry["peer"]): entry["mode_id"]
                for entry in document.get("paths", [])
                if (entry.get("namespace", "default") == self.namespace
                    and isinstance(entry.get("local"), str)
                    and isinstance(entry.get("peer"), str)
                    and isinstance(entry.get("mode_id"), int)
                    and not isinstance(entry.get("mode_id"), bool))
            }
        except (OSError, ValueError, TypeError, AttributeError):
            self._values = {}

    def get(self, key, default=None):
        with self._lock:
            return self._values.get(key, default)

    def record(self, key: tuple[str, str], mode_id: int) -> None:
        with self._lock:
            self._values[key] = mode_id
            self._save_locked()

    def _save_locked(self) -> None:
        others = []
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            others = [entry for entry in document.get("paths", [])
                      if entry.get("namespace", "default") != self.namespace]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        entries = others + [
            {"namespace": self.namespace, "local": local, "peer": peer,
             "mode_id": mode_id}
            for (local, peer), mode_id in sorted(self._values.items())
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "paths": entries}, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(temporary, self.path)


def last_good_mode(history: dict, mycall: str, peer_call: str):
    return history.get((mycall, peer_call))


def record_good_mode(history: dict, mycall: str, peer_call: str, mode_id: int):
    key = (mycall, peer_call)
    if hasattr(history, "record"):
        history.record(key, mode_id)
    else:
        history[key] = mode_id
