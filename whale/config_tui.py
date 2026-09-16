"""Curses TUI for creating and editing whale's application configuration.

Navigation shell: a small view stack (``App.stack``), each view a plain
object implementing the ``View`` protocol (``render`` + ``handle_key``).
``handle_key`` returns a ``KeyResult`` telling the app whether to do
nothing, push a new view, pop the current one, launch level tuning, or quit.
Three concrete
views: ``ConfigView`` (station settings plus the radio list),
``RadioDetailView`` (add/edit one radio), and ``ListPickerView`` (a reusable
browse-and-select list for defaults, audio devices, serial ports, and hamlib
models).

None of these views call a curses drawing function from ``handle_key`` --
all key handling only mutates plain Python state, so their logic is
unit-testable with no real terminal.
"""
from __future__ import annotations

import argparse
import curses
import os
import sys
from dataclasses import dataclass, replace
from typing import Any, Callable, Generic, Protocol, TypeVar

from whale.config import (Config, DEFAULT_CMD_PORT, DEFAULT_CONFIG,
                          DEFAULT_DATA_PORT, load_config, save_config)
from whale.hw.radios import Radio, RadioInventory, save_radios
from whale.radio_config_form import FormRow, RadioForm

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


@dataclass(frozen=True)
class RunLevels:
    """Temporarily hand the terminal to the live level tuner."""

    owner: "ConfigView"
    transmit_name: str
    receive_name: str | None


# Singletons for the no-payload results, so callers can just return them.
POP = Pop()
QUIT = Quit()
NOTHING = Nothing()

KeyResult = Push | Pop | Quit | Nothing | RunLevels


class View(Protocol):
    def render(self, stdscr) -> None: ...
    def handle_key(self, key: int) -> KeyResult: ...


class TextInputView:
    """Small single-value editor used for station settings."""

    def __init__(self, title: str, value: str, on_save: Callable[[str], str | None]) -> None:
        self.title = title
        self.value = value
        self.on_save = on_save
        self.status = ""

    def render(self, stdscr) -> None:
        height, _ = stdscr.getmaxyx()
        _safe_addnstr(stdscr, 0, 0, self.title, curses.A_BOLD)
        _safe_addnstr(stdscr, 2, 0, self.value + "_", curses.A_REVERSE)
        _safe_addnstr(stdscr, height - 1, 0, self.status or "[Enter] save  [Esc] cancel")

    def handle_key(self, key: int) -> KeyResult:
        if key == 27:
            return POP
        if key in (curses.KEY_ENTER, 10, 13):
            error = self.on_save(self.value)
            if error:
                self.status = error
                return NOTHING
            return POP
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.value = self.value[:-1]
        elif 32 <= key <= 126:
            self.value += chr(key)
        return NOTHING


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
            elif isinstance(result, RunLevels):
                # A receiver picker is no longer useful after making its
                # selection.  Remove it before handing the terminal over so
                # the config home screen is restored when tuning finishes.
                if self.stack[-1] is not result.owner:
                    self.stack.pop()
                self._run_levels(stdscr, result)

    @staticmethod
    def _run_levels(stdscr, request: RunLevels) -> None:
        """Run hardware I/O outside view key handling and apply its result."""
        from whale import level_tui

        owner = request.owner
        transmit_radio = owner.radios.get(request.transmit_name)
        receive_radio = (transmit_radio if request.receive_name is None
                         else owner.radios.get(request.receive_name))
        if transmit_radio is None or receive_radio is None:
            owner.status = "A selected level-tuning radio no longer exists."
            return
        try:
            # The selected radio is always the transmitter being calibrated.
            # Without a peer, still meter its own input for local checks.
            tx_device, _ = transmit_radio.devices()
            _, rx_device = receive_radio.devices()
            level_tui.run_level_tuner(
                stdscr, transmit_radio, receive_radio, owner.path,
                tx_device, rx_device,
                lambda level_db: owner._apply_tx_level(transmit_radio.id, level_db),
                request.receive_name is not None,
                apply_label="Apply",
                probe_analysis_available=request.receive_name is not None,
            )
        except Exception as exc:
            owner.status = f"Level tuner failed: {exc}"
            return
        finally:
            # The tuner uses timed reads; restore the config shell's normal
            # blocking input mode even when device setup or cleanup fails.
            stdscr.timeout(-1)

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


def _scroll_offset(selected: int, count: int, visible: int) -> int:
    """First index to draw so that ``selected`` stays on screen.

    Recomputed fresh every render from the current selection instead of
    being tracked as persistent state -- there's no prior-offset to carry
    between frames, and this "eager scroll" (jump exactly far enough to
    keep the cursor in view, no further) is simple and always correct.
    """
    if visible <= 0 or count <= visible:
        return 0
    offset = max(0, selected - visible + 1)
    return min(offset, count - visible)


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

        title = f"whale radio config -- {self.path}"
        if self.is_new_file:
            title += " (new file, not yet saved)"
        _safe_addnstr(stdscr, 0, 0, title, curses.A_BOLD)

        names = self._names()
        effective_default = self.effective_default()
        if not names:
            _safe_addnstr(stdscr, 2, 0, "No radios configured.")
        else:
            list_top = 2
            visible = max(0, height - 2 - list_top)
            offset = _scroll_offset(self.selected, len(names), visible)
            row = list_top
            for index in range(offset, len(names)):
                if row >= height - 2:
                    break
                name = names[index]
                radio = self.radios[name]
                marker = "*" if name == effective_default else " "
                channels = "/".join(sorted(radio.channels))
                line = (f"{marker} {name}  {radio.name}  "
                        f"in={radio.audio_input_name} out={radio.audio_output_name}  "
                        f"channels={channels}  ptt={radio.ptt_backend}")
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


@dataclass
class ConfigView:
    """App settings and radios together on the configuration home screen."""

    path: str
    callsign: str
    ssid: int | None
    radios: dict[str, Radio]
    default_fm_radio: str | None = None
    default_hf_radio: str | None = None
    cmd_port: int = DEFAULT_CMD_PORT
    data_port: int = DEFAULT_DATA_PORT
    log_file: str | None = None
    is_new_file: bool = False
    selected: int = 0
    pending: str | None = None
    dirty: bool = False
    status: str = ""

    SETTING_COUNT = 7

    def _names(self) -> list[str]:
        return list(self.radios)

    def _row_count(self) -> int:
        return self.SETTING_COUNT + len(self.radios)

    def _selected_radio(self) -> str | None:
        index = self.selected - self.SETTING_COUNT
        names = self._names()
        return names[index] if 0 <= index < len(names) else None

    @property
    def station_callsign(self) -> str:
        return self.callsign if self.ssid is None else f"{self.callsign}-{self.ssid}"

    def render(self, stdscr) -> None:
        height, _ = stdscr.getmaxyx()
        if height < 14:
            _safe_addnstr(stdscr, 0, 0, "terminal too small")
            return
        title = f"Whale Modem Configuration -- {self.path}"
        if self.is_new_file:
            title += " (new file)"
        _safe_addnstr(stdscr, 0, 0, title, curses.A_BOLD)
        _safe_addnstr(stdscr, 2, 0, "Station", curses.A_BOLD)
        values = [
            ("Callsign", self.callsign or "(not set)"),
            ("SSID", "(none)" if self.ssid is None else str(self.ssid)),
            ("Default FM radio", self.default_fm_radio or "(none)"),
            ("Default HF radio", self.default_hf_radio or "(none)"),
            ("Command port", str(self.cmd_port)),
            ("Data port", str(self.data_port)),
            ("Log file", self.log_file or "(stderr)"),
        ]
        for index, (label, value) in enumerate(values):
            attr = curses.A_REVERSE if self.selected == index else 0
            _safe_addnstr(stdscr, 3 + index, 2, f"{label:<18} {value}", attr)

        radio_top = 3 + self.SETTING_COUNT + 1
        _safe_addnstr(stdscr, radio_top, 0, "Radios", curses.A_BOLD)
        names = self._names()
        if not names:
            _safe_addnstr(stdscr, radio_top + 1, 2, "No radios configured.")
        else:
            visible = max(0, height - radio_top - 3)
            selected_radio_index = max(0, self.selected - self.SETTING_COUNT)
            offset = _scroll_offset(selected_radio_index, len(names), visible)
            row = radio_top + 1
            for index in range(offset, len(names)):
                if row >= height - 2:
                    break
                name = names[index]
                radio = self.radios[name]
                channels = ", ".join(channel.upper() for channel in sorted(radio.channels))
                attr = curses.A_REVERSE if self.selected == self.SETTING_COUNT + index else 0
                _safe_addnstr(stdscr, row, 2,
                              f"{name:<14} {radio.name:<24} {channels:<8} "
                              f"TX {radio.tx_level_db:+g} dB", attr)
                row += 1
        _safe_addnstr(stdscr, height - 2, 0,
                      f"Effective station callsign: {self.station_callsign or '(not set)'}")
        _safe_addnstr(stdscr, height - 1, 0, self._status_line())

    def _status_line(self) -> str:
        if self.pending == "delete":
            return f"Delete {self._selected_radio()!r}? (y/n)"
        if self.pending == "quit":
            return "Save before quitting? (y/n), or Esc to cancel"
        if self.status:
            return self.status
        dirty = " (unsaved changes)" if self.dirty else ""
        levels = "  [l] levels" if self._selected_radio() is not None else ""
        return ("[up/down] move  [Enter] edit  [a] add" + levels + "  "
                f"[x] delete  [s] save  [q] quit{dirty}")

    def handle_key(self, key: int) -> KeyResult:
        if self.pending == "delete":
            if key in (ord("y"), ord("Y")):
                self.pending = None
                self._delete_selected()
            elif key in (ord("n"), ord("N"), 27):
                self.pending = None
            return NOTHING
        if self.pending == "quit":
            if key in (ord("y"), ord("Y")):
                self.pending = None
                return QUIT if self._save() else NOTHING
            if key in (ord("n"), ord("N")):
                return QUIT
            if key == 27:
                self.pending = None
            return NOTHING

        if key in (curses.KEY_UP, ord("k")):
            self.selected = max(0, self.selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(max(0, self._row_count() - 1), self.selected + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            return self._edit_selected()
        elif key == ord("a"):
            return Push(RadioDetailView(None, self._names(), self._apply_edit))
        elif key == ord("l"):
            return self._choose_level_receiver()
        elif key in (ord("x"), curses.KEY_DC) and self._selected_radio() is not None:
            self.pending = "delete"
        elif key == ord("s"):
            self._save()
        elif key == ord("q"):
            if self.dirty:
                self.pending = "quit"
            else:
                return QUIT
        return NOTHING

    def _choose_level_receiver(self) -> KeyResult:
        """Optionally choose a peer to receive the over-air test probe."""
        transmit_name = self._selected_radio()
        if transmit_name is None:
            return NOTHING
        receivers: list[str | None] = [None] + [name for name in self._names() if name != transmit_name]
        self.status = ""
        return Push(ListPickerView(
            f"Optional over-air receiver for {transmit_name}", receivers,
            lambda name: "None — single-radio levels only" if name is None
            else f"{name} — receive probe from {transmit_name}",
            lambda receive_name: RunLevels(self, transmit_name, receive_name)))

    def _apply_tx_level(self, name: str, level_db: float) -> None:
        """Apply a tuned level to the working copy; normal save owns disk I/O."""
        radio = self.radios.get(name)
        if radio is None:
            self.status = f"Cannot apply level: radio {name!r} no longer exists."
            return
        self.radios[name] = replace(radio, tx_level_db=level_db)
        self.dirty = True
        self.status = f"Applied TX level {level_db:+g} dB to {name!r} (unsaved)."

    def _edit_selected(self) -> KeyResult:
        if self.selected == 0:
            return Push(TextInputView("Station callsign", self.callsign, self._set_callsign))
        if self.selected == 1:
            value = "" if self.ssid is None else str(self.ssid)
            return Push(TextInputView("SSID (blank for none)", value, self._set_ssid))
        if self.selected in (2, 3):
            channel = "fm" if self.selected == 2 else "hf"
            choices = [None] + [name for name, radio in self.radios.items()
                                if channel in radio.channels]
            return Push(ListPickerView(
                f"Default {channel.upper()} radio", choices,
                lambda item: "(none)" if item is None else str(item),
                lambda item: self._choose_default(channel, item)))
        if self.selected == 4:
            return Push(TextInputView("VARA API command port", str(self.cmd_port),
                                      lambda value: self._set_port("cmd", value)))
        if self.selected == 5:
            return Push(TextInputView("VARA API data port", str(self.data_port),
                                      lambda value: self._set_port("data", value)))
        if self.selected == 6:
            return Push(TextInputView("Log file (blank for stderr)", self.log_file or "",
                                      self._set_log_file))
        name = self._selected_radio()
        if name is None:
            return NOTHING
        return Push(RadioDetailView((name, self.radios[name]),
                                    [item for item in self._names() if item != name],
                                    self._apply_edit))

    def _set_callsign(self, value: str) -> str | None:
        value = value.strip().upper()
        if not value or not value.isascii() or not value.isalnum():
            return "Callsign must contain only ASCII letters and digits."
        effective = value if self.ssid is None else f"{value}-{self.ssid}"
        if len(effective) > 15:
            return "Callsign with SSID must be at most 15 characters."
        self.callsign = value
        self.dirty = True
        return None

    def _set_ssid(self, value: str) -> str | None:
        value = value.strip()
        if not value:
            ssid = None
        else:
            try:
                ssid = int(value)
            except ValueError:
                return "SSID must be an integer between 0 and 15."
            if not 0 <= ssid <= 15:
                return "SSID must be an integer between 0 and 15."
        effective = self.callsign if ssid is None else f"{self.callsign}-{ssid}"
        if len(effective) > 15:
            return "Callsign with SSID must be at most 15 characters."
        self.ssid = ssid
        self.dirty = True
        return None

    def _choose_default(self, channel: str, name: str | None) -> None:
        if channel == "fm":
            self.default_fm_radio = name
        else:
            self.default_hf_radio = name
        self.dirty = True

    def _set_port(self, kind: str, value: str) -> str | None:
        try:
            port = int(value.strip())
        except ValueError:
            return "Port must be an integer between 1 and 65535."
        if not 1 <= port <= 65535:
            return "Port must be an integer between 1 and 65535."
        other = self.data_port if kind == "cmd" else self.cmd_port
        if port == other:
            return "Command and data ports must be different."
        if kind == "cmd":
            self.cmd_port = port
        else:
            self.data_port = port
        self.dirty = True
        return None

    def _set_log_file(self, value: str) -> str | None:
        self.log_file = value.strip() or None
        self.dirty = True
        return None

    def _delete_selected(self) -> None:
        name = self._selected_radio()
        if name is None:
            return
        del self.radios[name]
        if self.default_fm_radio == name:
            self.default_fm_radio = None
        if self.default_hf_radio == name:
            self.default_hf_radio = None
        self.selected = min(self.selected, max(0, self._row_count() - 1))
        self.dirty = True
        self.status = f"Deleted {name!r}."

    def _apply_edit(self, old_name: str | None, new_name: str, radio: Radio) -> None:
        if old_name is not None and old_name != new_name:
            del self.radios[old_name]
            if self.default_fm_radio == old_name:
                self.default_fm_radio = new_name
            if self.default_hf_radio == old_name:
                self.default_hf_radio = new_name
        self.radios[new_name] = radio
        self.dirty = True

    def _save(self) -> bool:
        try:
            save_config(self.path, Config(self.callsign, self.ssid, dict(self.radios),
                                          self.default_fm_radio, self.default_hf_radio,
                                          self.cmd_port, self.data_port, self.log_file))
        except (ValueError, OSError) as exc:
            self.status = f"Cannot save: {exc}"
            return False
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
    and normally returns POP; a callback may return another navigation
    result for actions such as launching the level tuner. Esc returns POP
    without calling it. Like the other
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
                 on_select: Callable[[T], KeyResult | None],
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
        list_top = row
        visible = max(0, height - 2 - list_top)
        offset = _scroll_offset(self.highlighted, len(filtered), visible)
        for index in range(offset, len(filtered)):
            if row >= height - 2:
                break
            item = filtered[index]
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
            result = self.on_select(filtered[self.highlighted])
            return POP if result is None else result
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

_Row = FormRow


class RadioDetailView(RadioForm):
    """Add/edit form for one radio; pushed onto the stack by RadioListView.

    A fixed list of rows navigated top-to-bottom (the same up/down + j/k
    convention as RadioListView), plus a set of backend-specific rows that
    changes with the selected PTT backend -- see _BACKEND_ROWS. Save
    validates everything and hands the finished Radio to `on_done`, then
    pops; Cancel/Esc pop without calling it. Like RadioListView, no curses
    drawing calls happen anywhere in handle_key.
    """

    def __init__(self, existing: tuple[str, Radio] | None, other_names: list[str],
                 on_done: Callable[[str | None, str, Radio], None] | None) -> None:
        super().__init__(existing, other_names)
        self.on_done = on_done

        self.selected = 0
        self.editing_field: str | None = None
        self.edit_buffer = ""
        self.status = ""
        # A fresh blank name may follow Hamlib model selections until the
        # user supplies one. Existing configured names are always explicit.
        self._name_was_user_entered = existing is not None
        self._hamlib_models_by_id: dict[int, hamlib.RigModel] | None = None

    def _clamp_selection(self, rows: list[_Row]) -> None:
        self.selected = 0 if not rows else max(0, min(self.selected, len(rows) - 1))

    # -- rendering --

    def render(self, stdscr) -> None:
        height, width = stdscr.getmaxyx()
        if height < 3 or width < 20:
            _safe_addnstr(stdscr, 0, 0, "terminal too small")
            return

        title = "Add radio" if self.old_name is None else f"Edit radio -- {self.old_name}"
        _safe_addnstr(stdscr, 0, 0, title, curses.A_BOLD)

        rows = self._rows()
        list_top = 2
        visible = max(0, height - 2 - list_top)
        offset = _scroll_offset(self.selected, len(rows), visible)
        row_y = list_top
        for index in range(offset, len(rows)):
            row = rows[index]
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
        if row.key == "model" and self.ptt_backend == "hamlib":
            return self._hamlib_model_display(value)
        return "" if value is None else str(value)

    def _hamlib_model_display(self, value: Any) -> str:
        """Renders a stored hamlib rig id as "<manufacturer> <model_name> (id
        <id>)", falling back to the raw stored value whenever that isn't
        possible."""
        raw = "" if value is None else str(value)
        try:
            model_id = int(raw)
        except ValueError:
            return raw
        model = self._hamlib_models_by_id_map().get(model_id)
        return raw if model is None else f"{model.manufacturer} {model.model_name} (id {model.model})"

    def _hamlib_models_by_id_map(self) -> dict[int, hamlib.RigModel]:
        from whale.hw import hamlib

        # Cached per-instance: list_rig_models() is called from _display_value
        # on every render (potentially every keypress) while this form is
        # open, and the rig list never changes during the process's lifetime.
        if self._hamlib_models_by_id is None:
            try:
                models = hamlib.list_rig_models()
            except OSError:
                models = []
            self._hamlib_models_by_id = {model.model: model for model in models}
        return self._hamlib_models_by_id

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
        from whale.hw import audio_io

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
            if row.picker is not None and self._has_pickable_items(row):
                return self._handle_picker(row)
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

    def _has_pickable_items(self, row: _Row) -> bool:
        """Whether ``row``'s picker can currently offer at least one item.

        Gates the pick-only Enter behavior for the "text" rows underneath a
        PTT backend (port/usb_id/model/device): those still fall back to
        manual typing, but only when there's genuinely nothing to pick from
        (pyserial/libhamlib missing, or the hardware isn't connected to this
        machine -- e.g. pre-filling an inventory for a station you aren't
        sitting at). Swallows every exception the same way the picker
        actions below do; a failed probe just means "fall back to typing".
        """
        from whale.hw import hamlib, ptt

        try:
            if row.picker in ("serial", "serial_icom"):
                return bool(ptt.list_serial_ports())
            if row.picker == "hamlib":
                return bool(hamlib.list_rig_models())
        except Exception:
            return False
        return False

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
        if key == "name" and self.edit_buffer.strip():
            self._name_was_user_entered = True
        self.editing_field = None
        self.status = ""

    def _cycle_backend(self) -> None:
        self.status = ""
        self.cycle_backend()

    def _cycle_selector(self, key: str) -> None:
        self.status = ""
        self.cycle_selector(key)

    def _toggle_bool(self, key: str) -> None:
        self.status = ""
        self.toggle_bool(key)

    # -- hardware pickers (each degrades to a status message, never crashes) --

    def _push_audio_picker(self, key: str, kind: str) -> KeyResult:
        from whale.hw import audio_io

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
        from whale.hw import ptt

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
        from whale.hw import hamlib

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
            if not self.name.strip() or not self._name_was_user_entered:
                self.name = f"{model.manufacturer} {model.model_name}"

        return Push(ListPickerView("Hamlib rig models", models, format_item, on_select,
                                    search_key=search_key))

    # -- validation + save --

    def _do_save(self) -> KeyResult:
        radio, errors = self.build_radio()
        if errors:
            self.status = "; ".join(errors)
            return NOTHING

        assert radio is not None
        if self.on_done is not None:
            self.on_done(self.old_name, radio.id, radio)
        return POP


# --- CLI entry point -----------------------------------------------------

def _resolve_path(args: argparse.Namespace) -> str:
    return args.config or os.environ.get("WHALE_CONFIG") or DEFAULT_CONFIG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="application configuration TOML (or set WHALE_CONFIG); "
                                         f"defaults to {DEFAULT_CONFIG!r} in the current directory")
    args = parser.parse_args(argv)
    path = _resolve_path(args)

    try:
        config = load_config(path)
    except FileNotFoundError:
        config = Config("", None, {})
        is_new_file = True
    except ValueError as exc:
        print(f"error loading {path}: {exc}", file=sys.stderr)
        return 1
    else:
        is_new_file = False

    view = ConfigView(path=path, callsign=config.callsign, ssid=config.ssid,
                      radios=dict(config.radios),
                      default_fm_radio=config.default_fm_radio,
                      default_hf_radio=config.default_hf_radio,
                      cmd_port=config.cmd_port, data_port=config.data_port,
                      log_file=config.log_file,
                      is_new_file=is_new_file)
    app = App(view)
    curses.wrapper(app.main)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
