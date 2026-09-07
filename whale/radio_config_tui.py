"""Curses TUI for creating and editing a radio inventory TOML file.

Navigation shell: a small view stack (``App.stack``), each view a plain
object implementing the ``View`` protocol (``render`` + ``handle_key``).
``handle_key`` returns a ``KeyResult`` telling the app whether to do
nothing, push a new view, pop the current one, or quit. Three concrete
views: ``RadioListView`` (list/default/delete/save/quit), ``RadioDetailView``
(add/edit one radio, pushed by the list view), and ``ListPickerView`` (a
reusable browse-and-select list the detail view uses for its audio-device,
serial-port, and hamlib-model fields).

None of these views call a curses drawing function from ``handle_key`` --
all key handling only mutates plain Python state, so their logic is
unit-testable with no real terminal.
"""
from __future__ import annotations

import argparse
import curses
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Generic, Mapping, Protocol, TypeVar

from whale.hw import audio_io, hamlib, ptt
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
                line = (f"{marker} {name}  {radio.description}  "
                        f"in={radio.audio_input_name} out={radio.audio_output_name}  ptt={radio.ptt_backend}")
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
        return (f"[j/k or up/down] move  [a] add  [Enter] edit  [d] set default  "
                f"[x] delete  [s] save  [q] quit{dirty_note}")

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
        if key == ord("a"):
            self.status = ""
            return Push(RadioDetailView(existing=None, other_names=names, on_done=self._apply_edit))
        if key in (curses.KEY_ENTER, 10, 13):
            self.status = ""
            if names:
                name = names[self.selected]
                other_names = [n for n in names if n != name]
                return Push(RadioDetailView(existing=(name, self.radios[name]), other_names=other_names,
                                             on_done=self._apply_edit))
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

    def _apply_edit(self, old_name: str | None, new_name: str, radio: Radio) -> None:
        """The ``on_done`` callback handed to a pushed ``RadioDetailView``.

        ``old_name`` is ``None`` for "add", or the radio's key at the time
        the detail view was opened for "edit" (which may differ from
        ``new_name`` if the user renamed it while editing). A rename that
        moves the current default carries ``self.default`` along with it --
        the same dangling-default trap ``_delete_selected`` already guards
        against.
        """
        if old_name is not None and old_name != new_name:
            del self.radios[old_name]
            if self.default == old_name:
                self.default = new_name
        self.radios[new_name] = radio
        self.dirty = True

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


# --- Generic list picker --------------------------------------------------

T = TypeVar("T")


class ListPickerView(Generic[T]):
    """Browse ``items`` (optionally live-filtered), then hand one back.

    Reused for all three hardware pickers (audio devices, serial ports,
    hamlib rig models) -- they are the same shape: a list, an optional
    filter box, up/down + Enter/Esc. Selecting an item calls ``on_select``
    and returns POP; Esc returns POP without calling it. Like the other
    views in this module, ``handle_key`` never touches curses.

    When ``search_key`` is given, arrow keys (not j/k -- those are needed as
    literal typed characters while a filter box is active) move within the
    *filtered* list, any printable character appends to the filter query
    (case-insensitive substring match against ``search_key(item)``),
    Backspace removes a character, and Enter selects the highlighted
    *filtered* item -- not an index into the unfiltered ``items``. Without
    ``search_key``, up/down and j/k both move over the unfiltered list.
    """

    def __init__(self, title: str, items: list[T], format_item: Callable[[T], str],
                 on_select: Callable[[T], None],
                 search_key: Callable[[T], str] | None = None) -> None:
        self.title = title
        self.items = items
        self.format_item = format_item
        self.on_select = on_select
        self.search_key = search_key
        self.query = ""
        self.highlighted = 0
        self.status = ""

    def _filtered(self) -> list[T]:
        if self.search_key is None or not self.query:
            return self.items
        query = self.query.lower()
        return [item for item in self.items if query in self.search_key(item).lower()]

    def _clamp(self) -> None:
        filtered = self._filtered()
        self.highlighted = 0 if not filtered else max(0, min(self.highlighted, len(filtered) - 1))

    # -- rendering --

    def render(self, stdscr) -> None:
        height, width = stdscr.getmaxyx()
        if height < 3 or width < 20:
            _safe_addnstr(stdscr, 0, 0, "terminal too small")
            return
        _safe_addnstr(stdscr, 0, 0, self.title, curses.A_BOLD)

        row = 1
        if self.search_key is not None:
            _safe_addnstr(stdscr, row, 0, f"filter: {self.query}")
            row += 1
        filtered = self._filtered()
        _safe_addnstr(stdscr, row, 0, f"{len(filtered)}/{len(self.items)} shown")
        row += 1
        if not filtered:
            _safe_addnstr(stdscr, row, 0, "no matches" if self.query else "nothing to pick from")
        for index, item in enumerate(filtered):
            if row >= height - 2:
                break
            attr = curses.A_REVERSE if index == self.highlighted else 0
            _safe_addnstr(stdscr, row, 0, self.format_item(item), attr)
            row += 1

        _safe_addnstr(stdscr, height - 1, 0, self.status or "[Enter] select  [Esc] cancel")

    # -- key handling (pure logic, no curses drawing calls) --

    def handle_key(self, key: int) -> KeyResult:
        if key == 27:  # Esc
            return POP
        if key in (curses.KEY_ENTER, 10, 13):
            filtered = self._filtered()
            if not filtered:
                return NOTHING
            self.on_select(filtered[self.highlighted])
            return POP
        if self.search_key is None:
            if key in (curses.KEY_UP, ord("k")):
                self.highlighted = max(0, self.highlighted - 1)
                return NOTHING
            if key in (curses.KEY_DOWN, ord("j")):
                filtered = self._filtered()
                self.highlighted = min(max(0, len(filtered) - 1), self.highlighted + 1)
                return NOTHING
            return NOTHING
        # Filtering active: arrow keys move; every printable char (j/k included) types.
        if key == curses.KEY_UP:
            self.highlighted = max(0, self.highlighted - 1)
            return NOTHING
        if key == curses.KEY_DOWN:
            filtered = self._filtered()
            self.highlighted = min(max(0, len(filtered) - 1), self.highlighted + 1)
            return NOTHING
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.query = self.query[:-1]
            self._clamp()
            return NOTHING
        if 32 <= key <= 126:
            self.query += chr(key)
            self._clamp()
            return NOTHING
        return NOTHING


# --- Radio detail view (add/edit) ------------------------------------------

@dataclass
class _Row:
    """One navigable row of a RadioDetailView form."""

    key: str
    label: str
    kind: str  # "text" | "selector" | "backend_selector" | "bool" | "action" | "device"
    picker: str | None = None  # None | "audio_input" | "audio_output" | "serial" | "serial_icom" | "hamlib"


# Backend-specific rows, in the exact field order/requiredness the real
# open() methods in whale/hw/ptt_backends.py expect -- see that module for
# what each key does. Order here matches the wizard spec table.
_BACKEND_ROWS: dict[str, list[_Row]] = {
    "vox": [],
    "serial-line": [
        _Row("port", "Port", "text", picker="serial"),
        _Row("line", "Line", "selector"),
        _Row("baud", "Baud", "text"),
        _Row("active_high", "Active high", "bool"),
    ],
    "icom-civ": [
        _Row("usb_id", "USB ID (VID:PID)", "text", picker="serial_icom"),
        _Row("radio_name", "Radio name", "text"),
        _Row("address", "Address", "text"),
    ],
    "hamlib": [
        _Row("model", "Model", "text", picker="hamlib"),
        _Row("device", "Device", "text", picker="serial"),
        _Row("baud", "Baud", "text"),
        _Row("civaddr", "CI-V address", "text"),
        _Row("timeout", "Timeout", "text"),
        _Row("retry", "Retry", "text"),
    ],
}


def _default_backend_values(backend: str) -> dict[str, Any]:
    """The blank-form starting values for one backend's extra rows."""
    if backend == "serial-line":
        return {"port": "", "line": "rts", "baud": "", "active_high": True}
    if backend == "icom-civ":
        return {"usb_id": "", "radio_name": "", "address": ""}
    if backend == "hamlib":
        return {"model": "", "device": "", "baud": "", "civaddr": "", "timeout": "", "retry": ""}
    return {}  # vox has no extra rows


def _values_from_config(backend: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """Turns a saved Radio.ptt_config back into editable text/selector/bool values.

    Not required to round-trip byte-for-byte -- just enough to seed the form
    with what was there before, same spirit as the rest of this wizard.
    """
    values = _default_backend_values(backend)
    if backend == "serial-line":
        if "port" in config:
            values["port"] = str(config["port"])
        if config.get("line") in ("rts", "dtr"):
            values["line"] = config["line"]
        if "baud" in config:
            values["baud"] = str(config["baud"])
        if "active_high" in config:
            values["active_high"] = bool(config["active_high"])
    elif backend == "icom-civ":
        for key in ("usb_id", "radio_name", "address"):
            if key in config:
                values[key] = str(config[key])
    elif backend == "hamlib":
        for key in ("model", "device", "baud", "civaddr", "timeout", "retry"):
            if key in config:
                values[key] = str(config[key])
    return values


class RadioDetailView:
    """Add/edit form for one radio; pushed onto the stack by RadioListView.

    A fixed list of rows navigated top-to-bottom (the same up/down + j/k
    convention as RadioListView), plus a set of backend-specific rows that
    changes with the selected PTT backend -- see _BACKEND_ROWS. Save
    validates everything and hands the finished Radio to `on_done`, then
    pops; Cancel/Esc pop without calling it. Like RadioListView, no curses
    drawing calls happen anywhere in handle_key.
    """

    BACKEND_ORDER = ["vox", "serial-line", "icom-civ", "hamlib"]
    _TOP_LEVEL_FIELDS = {"name", "description", "audio_input_name", "audio_output_name"}

    def __init__(self, existing: tuple[str, Radio] | None, other_names: list[str],
                 on_done: Callable[[str | None, str, Radio], None] | None) -> None:
        self.old_name = existing[0] if existing else None
        self.other_names = list(other_names)
        self.on_done = on_done

        radio = existing[1] if existing else None
        self.name = radio.name if radio else ""
        self.description = radio.description if radio else ""
        self.audio_input_name = radio.audio_input_name if radio else ""
        self.audio_output_name = radio.audio_output_name if radio else ""
        self.ptt_backend = radio.ptt_backend if (radio and radio.ptt_backend in self.BACKEND_ORDER) else "vox"

        self.backend_config: dict[str, dict[str, Any]] = {
            name: _default_backend_values(name) for name in self.BACKEND_ORDER
        }
        if radio is not None and radio.ptt_backend in self.backend_config:
            self.backend_config[radio.ptt_backend] = _values_from_config(radio.ptt_backend, radio.ptt_config)

        self.selected = 0
        self.editing_field: str | None = None
        self.edit_buffer = ""
        self.status = ""

    def _rows(self) -> list[_Row]:
        rows = [
            _Row("name", "Name", "text"),
            _Row("description", "Description", "text"),
            _Row("audio_input_name", "Audio input", "device", picker="audio_input"),
            _Row("audio_output_name", "Audio output", "device", picker="audio_output"),
            _Row("ptt_backend", "PTT backend", "backend_selector"),
        ]
        rows.extend(_BACKEND_ROWS[self.ptt_backend])
        rows.append(_Row("save", "Save", "action"))
        rows.append(_Row("cancel", "Cancel", "action"))
        return rows

    def _clamp_selection(self, rows: list[_Row]) -> None:
        self.selected = 0 if not rows else max(0, min(self.selected, len(rows) - 1))

    def _get_value(self, key: str) -> Any:
        if key == "ptt_backend":
            return self.ptt_backend
        if key in self._TOP_LEVEL_FIELDS:
            return getattr(self, key)
        return self.backend_config[self.ptt_backend][key]

    def _set_value(self, key: str, value: Any) -> None:
        if key in self._TOP_LEVEL_FIELDS:
            setattr(self, key, value)
        else:
            self.backend_config[self.ptt_backend][key] = value

    # -- rendering --

    def render(self, stdscr) -> None:
        height, width = stdscr.getmaxyx()
        if height < 3 or width < 20:
            _safe_addnstr(stdscr, 0, 0, "terminal too small")
            return

        title = "Add radio" if self.old_name is None else f"Edit radio -- {self.old_name}"
        _safe_addnstr(stdscr, 0, 0, title, curses.A_BOLD)

        rows = self._rows()
        row_y = 2
        for index, row in enumerate(rows):
            if row_y >= height - 2:
                break
            attr = curses.A_REVERSE if index == self.selected else 0
            marker = ">" if index == self.selected else " "
            if row.kind == "action":
                line = f"{marker} {row.label}"
            else:
                line = f"{marker} {row.label}: {self._display_value(row)}"
                if row.kind == "device":
                    line += self._device_availability(row)
            _safe_addnstr(stdscr, row_y, 0, line, attr)
            row_y += 1

        _safe_addnstr(stdscr, height - 1, 0, self.status or self._help_line())

    def _help_line(self) -> str:
        if self.editing_field is not None:
            return "[type] edit  [Enter] commit  [Esc] revert field"
        rows = self._rows()
        row = rows[self.selected] if rows else None
        picker_hint = "  [p] pick" if row is not None and row.picker is not None else ""
        return f"[j/k or up/down] move  [Enter/Space] act{picker_hint}  [Esc] cancel form"

    def _display_value(self, row: _Row) -> str:
        if self.editing_field == row.key:
            return self.edit_buffer + "_"
        value = self._get_value(row.key)
        if row.kind == "bool":
            return "yes" if value else "no"
        return "" if value is None else str(value)

    def _device_availability(self, row: _Row) -> str:
        """Plain-ASCII marker showing whether a stored device row's value is
        among the *currently enumerated* devices for its direction.

        Informational only -- never blocks Save, a radio might simply not be
        plugged in right now. Three renderable outcomes, kept visually
        distinct:

          - field empty, or the stored name is among the current devices:
            no marker at all.
          - field non-empty but the name is not currently enumerated: " [not
            found]" -- a real, checked absence.
          - the enumeration itself failed with LookupError (no PortAudio
            host API at all -- routine in a sandbox/CI with no sound card;
            see audio_io._host_api_index()): " (availability unknown)",
            deliberately different from "[not found]" so a machine with no
            sound card at all doesn't look like every device is missing.
        """
        kind = "input" if row.picker == "audio_input" else "output"
        name = str(self._get_value(row.key) or "").strip()
        if not name:
            return ""
        try:
            current = {d.name for d in audio_io.list_devices(kind=kind)}
        except LookupError:
            return " (availability unknown)"
        return "" if name in current else " [not found]"

    # -- key handling (pure logic, no curses drawing calls) --

    def handle_key(self, key: int) -> KeyResult:
        rows = self._rows()
        self._clamp_selection(rows)
        if self.editing_field is not None:
            return self._handle_edit_key(key)
        return self._handle_normal_key(key, rows)

    def _handle_normal_key(self, key: int, rows: list[_Row]) -> KeyResult:
        if key == 27:  # Esc == Cancel
            return POP
        if key in (curses.KEY_UP, ord("k")):
            self.status = ""
            self.selected = max(0, self.selected - 1)
            return NOTHING
        if key in (curses.KEY_DOWN, ord("j")):
            self.status = ""
            self.selected = min(len(rows) - 1, self.selected + 1)
            return NOTHING
        row = rows[self.selected]
        if key == ord("p"):
            return self._handle_picker(row)
        if key in (curses.KEY_ENTER, 10, 13):
            return self._handle_enter(row)
        if key == ord(" "):
            return self._handle_space(row)
        return NOTHING

    def _handle_enter(self, row: _Row) -> KeyResult:
        if row.kind == "text":
            self._start_edit(row.key)
            return NOTHING
        if row.kind == "device":
            return self._handle_picker(row)
        if row.kind == "backend_selector":
            self._cycle_backend()
            return NOTHING
        if row.kind == "selector":
            self._cycle_selector(row.key)
            return NOTHING
        if row.kind == "bool":
            self._toggle_bool(row.key)
            return NOTHING
        if row.kind == "action":
            if row.key == "save":
                return self._do_save()
            if row.key == "cancel":
                return POP
        return NOTHING

    def _handle_space(self, row: _Row) -> KeyResult:
        if row.kind == "backend_selector":
            self._cycle_backend()
        elif row.kind == "selector":
            self._cycle_selector(row.key)
        elif row.kind == "bool":
            self._toggle_bool(row.key)
        return NOTHING

    def _handle_picker(self, row: _Row) -> KeyResult:
        if row.picker == "audio_input":
            return self._push_audio_picker(row.key, "input")
        if row.picker == "audio_output":
            return self._push_audio_picker(row.key, "output")
        if row.picker == "serial":
            return self._push_serial_picker(row.key)
        if row.picker == "serial_icom":
            return self._push_icom_serial_picker(row.key)
        if row.picker == "hamlib":
            return self._push_hamlib_picker(row.key)
        return NOTHING

    def _start_edit(self, key: str) -> None:
        self.status = ""
        self.editing_field = key
        value = self._get_value(key)
        self.edit_buffer = "" if value is None else str(value)

    def _handle_edit_key(self, key: int) -> KeyResult:
        if key == 27:  # Esc reverts *this field*, not the whole form.
            self.editing_field = None
            return NOTHING
        if key in (curses.KEY_ENTER, 10, 13):
            self._commit_edit()
            return NOTHING
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.edit_buffer = self.edit_buffer[:-1]
            return NOTHING
        if 32 <= key <= 126:
            self.edit_buffer += chr(key)
            return NOTHING
        return NOTHING

    def _commit_edit(self) -> None:
        key = self.editing_field
        self._set_value(key, self.edit_buffer)
        # Seed Description from Name on a fresh add, same spirit as
        # radios.py._radio() defaulting an *absent* description to the name
        # -- just surfaced here as an overwritable starting point.
        if key == "name" and self.old_name is None and not self.description.strip():
            self.description = self.edit_buffer
        self.editing_field = None
        self.status = ""

    def _cycle_backend(self) -> None:
        self.status = ""
        index = self.BACKEND_ORDER.index(self.ptt_backend)
        self.ptt_backend = self.BACKEND_ORDER[(index + 1) % len(self.BACKEND_ORDER)]

    def _cycle_selector(self, key: str) -> None:
        self.status = ""
        if key == "line":
            current = self._get_value(key)
            self._set_value(key, "dtr" if current == "rts" else "rts")

    def _toggle_bool(self, key: str) -> None:
        self.status = ""
        self._set_value(key, not self._get_value(key))

    # -- hardware pickers (each degrades to a status message, never crashes) --

    def _push_audio_picker(self, key: str, kind: str) -> KeyResult:
        try:
            devices = audio_io.list_devices(kind=kind)
        except LookupError as exc:
            self.status = f"device list unavailable: {exc}"
            return NOTHING

        def format_item(device: audio_io.AudioDevice) -> str:
            return (f"{device.name} (in={device.max_input_channels} "
                    f"out={device.max_output_channels}, {device.host_api})")

        def on_select(device: audio_io.AudioDevice) -> None:
            self._set_value(key, device.name)

        title = "Audio input devices" if kind == "input" else "Audio output devices"
        return Push(ListPickerView(title, devices, format_item, on_select,
                                    search_key=lambda d: d.name))

    def _serial_ports_or_status(self) -> list[ptt.SerialPort] | None:
        try:
            return ptt.list_serial_ports()
        except Exception as exc:
            self.status = f"serial port list unavailable: {exc}"
            return None

    @staticmethod
    def _format_serial_port(port: ptt.SerialPort) -> str:
        line = f"{port.device}  {port.description}"
        return line if port.usb_id is None else f"{line} [{port.usb_id}]"

    def _push_serial_picker(self, key: str) -> KeyResult:
        ports = self._serial_ports_or_status()
        if ports is None:
            return NOTHING

        def on_select(port: ptt.SerialPort) -> None:
            self._set_value(key, port.device)

        return Push(ListPickerView("Serial ports", ports, self._format_serial_port, on_select,
                                    search_key=self._format_serial_port))

    def _push_icom_serial_picker(self, key: str) -> KeyResult:
        ports = self._serial_ports_or_status()
        if ports is None:
            return NOTHING

        def on_select(port: ptt.SerialPort) -> None:
            if port.usb_id is not None:
                self._set_value(key, port.usb_id)
            else:
                self.status = f"{port.device} has no USB VID:PID; usb_id left unchanged"

        return Push(ListPickerView("Serial ports", ports, self._format_serial_port, on_select,
                                    search_key=self._format_serial_port))

    def _push_hamlib_picker(self, key: str) -> KeyResult:
        try:
            models = hamlib.list_rig_models()
        except OSError as exc:
            self.status = f"hamlib model list unavailable: {exc}"
            return NOTHING

        def format_item(model: hamlib.RigModel) -> str:
            return f"{model.model}  {model.manufacturer} {model.model_name}  ({model.status})"

        def search_key(model: hamlib.RigModel) -> str:
            return f"{model.manufacturer} {model.model_name}"

        def on_select(model: hamlib.RigModel) -> None:
            self._set_value(key, str(model.model))
            if not self.description.strip():
                self.description = f"{model.manufacturer} {model.model_name}"

        return Push(ListPickerView("Hamlib rig models", models, format_item, on_select,
                                    search_key=search_key))

    # -- validation + save --

    def _do_save(self) -> KeyResult:
        errors: list[str] = []

        name = self.name.strip()
        if not name:
            errors.append("name is required")
        elif name in self.other_names:
            errors.append(f"a radio named {name!r} already exists")

        description = self.description.strip()
        if not description:
            errors.append("description is required")

        audio_input_name = self.audio_input_name.strip()
        if not audio_input_name:
            errors.append("audio input is required")

        audio_output_name = self.audio_output_name.strip()
        if not audio_output_name:
            errors.append("audio output is required")

        ptt_config = self._build_ptt_config(errors)

        if errors:
            self.status = "; ".join(errors)
            return NOTHING

        radio = Radio(name=name, description=description, audio_input_name=audio_input_name,
                      audio_output_name=audio_output_name, ptt_backend=self.ptt_backend,
                      ptt_config=ptt_config)
        if self.on_done is not None:
            self.on_done(self.old_name, name, radio)
        return POP

    def _build_ptt_config(self, errors: list[str]) -> dict[str, Any]:
        """Builds ptt_config for the current backend, appending to `errors`.

        Optional fields left blank are omitted entirely rather than written
        as empty strings -- an absent key and an empty string are not the
        same thing to whale/hw/ptt_backends.py (e.g. `"civaddr" in config`).
        """
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
            baud_text = values["baud"].strip()
            if baud_text:
                try:
                    config["baud"] = int(baud_text)
                except ValueError:
                    errors.append("baud must be a whole number")
            else:
                config["baud"] = 9600
            config["active_high"] = bool(values["active_high"])

        elif backend == "icom-civ":
            usb_id = values["usb_id"].strip()
            if not usb_id:
                errors.append("usb_id is required")
            else:
                config["usb_id"] = usb_id
            radio_name = values["radio_name"].strip()
            if radio_name:
                config["radio_name"] = radio_name
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
            baud_text = values["baud"].strip()
            if baud_text:
                try:
                    config["baud"] = int(baud_text)
                except ValueError:
                    errors.append("baud must be a whole number")
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

        # vox: no ptt_config keys at all.
        return config


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
