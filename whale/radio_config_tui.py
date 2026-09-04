"""Curses TUI for creating and editing a radio inventory TOML file.

Navigation shell: a small view stack (``App.stack``), each view a plain
object implementing the ``View`` protocol (``render`` + ``handle_key``).
``handle_key`` returns a ``KeyResult`` telling the app whether to do
nothing, push a new view, pop the current one, or quit. This task ships
exactly one concrete view, ``RadioListView`` -- a follow-up task adds a
radio detail/add/edit view (built on the audio-device, serial-port, and
hamlib-model pickers already committed in ``whale/hw/``) that pushes
itself on top of the list.

``RadioListView`` deliberately never calls a curses drawing function from
``handle_key`` -- all key handling only mutates plain Python state (a
``dict[str, Radio]`` plus a ``str | None`` default, mirroring
``RadioInventory`` without needing a live, frozen instance kicking
around) -- so its logic is unit-testable with no real terminal.
"""
from __future__ import annotations

import argparse
import curses
import os
import sys
from dataclasses import dataclass
from typing import Protocol

from whale.hw.radios import Radio, RadioInventory, load_radios, save_radios

DEFAULT_RADIO_CONFIG = "radios.toml"


# --- Navigation shell -------------------------------------------------

class Push:
    """Push ``view`` onto the stack; it becomes the active view."""

    def __init__(self, view: "View") -> None:
        self.view = view


class Pop:
    """Pop the active view, returning to whatever is beneath it."""


class Quit:
    """Tear down the whole app."""


class Nothing:
    """Nothing happened; keep running the active view."""


# Singletons for the no-payload results, so callers can just return them.
POP = Pop()
QUIT = Quit()
NOTHING = Nothing()

KeyResult = Push | Pop | Quit | Nothing


class View(Protocol):
    def render(self, stdscr) -> None: ...
    def handle_key(self, key: int) -> KeyResult: ...


class App:
    """Owns the view stack and runs the curses main loop."""

    def __init__(self, root: View) -> None:
        self.stack: list[View] = [root]

    def run(self, stdscr) -> None:
        curses.curs_set(0)
        while self.stack:
            stdscr.erase()
            self.stack[-1].render(stdscr)
            stdscr.refresh()
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            result = self.stack[-1].handle_key(key)
            if isinstance(result, Push):
                self.stack.append(result.view)
            elif isinstance(result, Pop):
                self.stack.pop()
            elif isinstance(result, Quit):
                return

    def main(self, stdscr) -> None:
        self.run(stdscr)


def _safe_addnstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    """addnstr that swallows curses.error from a too-small terminal."""
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    try:
        stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)
    except curses.error:
        pass


# --- Radio list view ----------------------------------------------------

@dataclass
class RadioListView:
    """Shows every radio in the working inventory; move/set-default/delete/save/quit."""

    path: str
    radios: dict[str, Radio]
    default: str | None
    is_new_file: bool = False
    selected: int = 0
    pending: str | None = None  # None | "delete" | "quit"
    dirty: bool = False
    status: str = ""

    def _names(self) -> list[str]:
        return list(self.radios)

    def effective_default(self) -> str | None:
        """What load_radios() would treat as the default if this were saved and reloaded."""
        if self.default is not None:
            return self.default
        if len(self.radios) == 1:
            return next(iter(self.radios))
        return None

    def _clamp_selection(self) -> None:
        names = self._names()
        if not names:
            self.selected = 0
        else:
            self.selected = max(0, min(self.selected, len(names) - 1))

    # -- rendering --

    def render(self, stdscr) -> None:
        height, width = stdscr.getmaxyx()
        if height < 3 or width < 20:
            _safe_addnstr(stdscr, 0, 0, "terminal too small")
            return

        title = f"whalemodem radio config -- {self.path}"
        if self.is_new_file:
            title += " (new file, not yet saved)"
        _safe_addnstr(stdscr, 0, 0, title, curses.A_BOLD)

        names = self._names()
        effective_default = self.effective_default()
        if not names:
            _safe_addnstr(stdscr, 2, 0, "No radios configured.")
        else:
            row = 2
            for index, name in enumerate(names):
                if row >= height - 2:
                    break
                radio = self.radios[name]
                marker = "*" if name == effective_default else " "
                line = f"{marker} {name}  {radio.description}  [{radio.audio_name}]  ptt={radio.ptt_backend}"
                attr = curses.A_REVERSE if index == self.selected else 0
                _safe_addnstr(stdscr, row, 0, line, attr)
                row += 1

        _safe_addnstr(stdscr, height - 1, 0, self._status_line())

    def _status_line(self) -> str:
        if self.pending == "delete":
            names = self._names()
            name = names[self.selected] if names else ""
            return f"Delete {name!r}? (y/n)"
        if self.pending == "quit":
            return "Save before quitting? (y/n), or Esc to cancel"
        if self.status:
            return self.status
        dirty_note = " (unsaved changes)" if self.dirty else ""
        return f"[j/k or up/down] move  [d] set default  [x] delete  [s] save  [q] quit{dirty_note}"

    # -- key handling (pure logic, no curses drawing calls) --

    def handle_key(self, key: int) -> KeyResult:
        if self.pending == "delete":
            return self._handle_delete_confirm(key)
        if self.pending == "quit":
            return self._handle_quit_confirm(key)
        return self._handle_normal(key)

    def _handle_normal(self, key: int) -> KeyResult:
        names = self._names()
        if key in (curses.KEY_UP, ord("k")):
            self.status = ""
            if names:
                self.selected = max(0, self.selected - 1)
            return NOTHING
        if key in (curses.KEY_DOWN, ord("j")):
            self.status = ""
            if names:
                self.selected = min(len(names) - 1, self.selected + 1)
            return NOTHING
        if key == ord("d"):
            self.status = ""
            if names:
                self.default = names[self.selected]
                self.dirty = True
            return NOTHING
        if key in (ord("x"), curses.KEY_DC):
            self.status = ""
            if names:
                self.pending = "delete"
            return NOTHING
        if key == ord("s"):
            self._save()
            return NOTHING
        if key == ord("q"):
            if self.dirty:
                self.pending = "quit"
                return NOTHING
            return QUIT
        return NOTHING

    def _handle_delete_confirm(self, key: int) -> KeyResult:
        if key in (ord("y"), ord("Y")):
            self.pending = None
            self._delete_selected()
            return NOTHING
        if key in (ord("n"), ord("N"), 27):  # 27 == Esc
            self.pending = None
            self.status = ""
            return NOTHING
        return NOTHING

    def _handle_quit_confirm(self, key: int) -> KeyResult:
        if key in (ord("y"), ord("Y")):
            self.pending = None
            if not self._save():
                # Save was refused (e.g. empty inventory) -- stay put rather
                # than quitting with unsaved, unsavable changes silently lost.
                return NOTHING
            return QUIT
        if key in (ord("n"), ord("N")):
            self.pending = None
            return QUIT
        if key == 27:  # Esc cancels the quit entirely
            self.pending = None
            self.status = ""
            return NOTHING
        return NOTHING

    def _delete_selected(self) -> None:
        names = self._names()
        if not names:
            return
        name = names[self.selected]
        del self.radios[name]
        if self.default == name:
            self.default = None
        self.dirty = True
        self._clamp_selection()
        self.status = f"Deleted {name!r}."

    def _save(self) -> bool:
        """Attempt to save; returns True on success. Refuses to write an
        empty inventory, since load_radios() rejects a file with zero
        [radios.*] tables -- see radios.py.
        """
        if not self.radios:
            self.status = "Nothing to save: inventory is empty (load_radios rejects an empty file)."
            return False
        inventory = RadioInventory(dict(self.radios), self.default)
        save_radios(self.path, inventory)
        self.dirty = False
        self.is_new_file = False
        self.status = f"Saved to {self.path}."
        return True


# --- CLI entry point -----------------------------------------------------

def _resolve_path(args: argparse.Namespace) -> str:
    return args.radio_config or os.environ.get("WHALE_RADIO_CONFIG") or DEFAULT_RADIO_CONFIG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radio-config", help="TOML radio inventory (or set WHALE_RADIO_CONFIG); "
                                                 f"defaults to {DEFAULT_RADIO_CONFIG!r} in the current directory")
    args = parser.parse_args(argv)
    path = _resolve_path(args)

    try:
        inventory = load_radios(path)
    except FileNotFoundError:
        inventory = RadioInventory({}, None)
        is_new_file = True
    except ValueError as exc:
        print(f"error loading {path}: {exc}", file=sys.stderr)
        return 1
    else:
        is_new_file = False

    view = RadioListView(path=path, radios=dict(inventory.radios), default=inventory.default,
                          is_new_file=is_new_file)
    app = App(view)
    curses.wrapper(app.main)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
