import curses

from whale.hw import audio_io, hamlib, ptt
from whale.hw.radios import Radio
from whale.radio_config_tui import NOTHING, POP, RadioDetailView, RadioListView, ListPickerView


class _FakeScreen:
    """Enough of a curses window for render() to run against with no real
    terminal -- getmaxyx()/addnstr() only, matching what _safe_addnstr uses.

    This exists because a real bug (KeyError: 'save', from render() calling
    _display_value() on an "action" row that has no backend_config entry)
    was invisible to every handle_key()-only test above: nothing in this
    file ever called render() at all. Caught via an actual pty-driven
    interactive run, not by any unit test -- these tests close that gap.
    """

    def __init__(self, height=24, width=80):
        self._height = height
        self._width = width

    def getmaxyx(self):
        return (self._height, self._width)

    def addnstr(self, y, x, text, n, attr=0):
        pass


def _type(view, text):
    for ch in text:
        view.handle_key(ord(ch))


def _enter(view):
    return view.handle_key(curses.KEY_ENTER)


def _clear_edit_buffer(view):
    """Backspaces out whatever _start_edit pre-filled edit_buffer with --
    the append/backspace-at-end editor pre-seeds from the current value, so
    replacing it (rather than appending) means clearing it first, the way a
    real user would."""
    while view.edit_buffer:
        view.handle_key(curses.KEY_BACKSPACE)


def _select_row(view, key):
    rows = view._rows()
    for index, row in enumerate(rows):
        if row.key == key:
            view.selected = index
            return row
    raise AssertionError(f"no such row: {key}")


def _new_view(existing=None, other_names=None, on_done=None):
    return RadioDetailView(existing=existing, other_names=other_names or [], on_done=on_done)


# -- field navigation --

def test_navigation_up_down_through_backend_specific_rows():
    view = _new_view()
    _select_row(view, "ptt_backend")
    _enter(view)  # vox -> serial-line, adds 4 extra rows
    assert view.ptt_backend == "serial-line"
    rows = view._rows()
    keys = [r.key for r in rows]
    assert keys == ["name", "description", "audio_name", "ptt_backend",
                     "port", "line", "baud", "active_high", "save", "cancel"]

    view.selected = 0
    for _ in range(len(rows) - 1):
        view.handle_key(curses.KEY_DOWN)
    assert view.selected == len(rows) - 1
    view.handle_key(ord("j"))
    assert view.selected == len(rows) - 1  # clamps at bottom

    for _ in range(len(rows) - 1):
        view.handle_key(curses.KEY_UP)
    assert view.selected == 0
    view.handle_key(ord("k"))
    assert view.selected == 0  # clamps at top


# -- text editing --

def test_text_edit_type_backspace_commit():
    view = _new_view()
    _select_row(view, "name")
    result = _enter(view)
    assert result is NOTHING
    assert view.editing_field == "name"

    _type(view, "ic705")
    view.handle_key(curses.KEY_BACKSPACE)
    assert view.edit_buffer == "ic70"

    _type(view, "5")
    _enter(view)
    assert view.editing_field is None
    assert view.name == "ic705"


def test_text_edit_esc_reverts_only_that_field():
    view = _new_view()
    view.name = "original"
    view.description = "kept"
    _select_row(view, "name")
    _enter(view)
    _clear_edit_buffer(view)
    _type(view, "xxx")
    result = view.handle_key(27)
    assert result is NOTHING
    assert view.editing_field is None
    assert view.name == "original"  # reverted
    assert view.description == "kept"  # untouched


def test_adding_fresh_seeds_blank_description_from_name():
    view = _new_view()
    assert view.description == ""
    _select_row(view, "name")
    _enter(view)
    _type(view, "ic705")
    _enter(view)
    assert view.name == "ic705"
    assert view.description == "ic705"


def test_editing_existing_radio_name_does_not_overwrite_existing_description():
    radio = Radio("old", "My IC-705", "IC-705", "vox", {})
    view = _new_view(existing=("old", radio))
    _select_row(view, "name")
    _enter(view)
    _clear_edit_buffer(view)
    _type(view, "new")
    _enter(view)
    assert view.name == "new"
    assert view.description == "My IC-705"  # untouched: old_name is not None


# -- backend cycling --

def test_backend_cycling_changes_visible_rows():
    view = _new_view()
    assert view.ptt_backend == "vox"
    assert [r.key for r in view._rows() if r.key not in ("name", "description", "audio_name",
                                                           "ptt_backend", "save", "cancel")] == []

    _select_row(view, "ptt_backend")
    _enter(view)
    assert view.ptt_backend == "serial-line"
    assert "port" in [r.key for r in view._rows()]

    _enter(view)
    assert view.ptt_backend == "icom-civ"
    assert "usb_id" in [r.key for r in view._rows()]

    _enter(view)
    assert view.ptt_backend == "hamlib"
    assert "model" in [r.key for r in view._rows()]

    _enter(view)
    assert view.ptt_backend == "vox"


def test_backend_cycling_via_space():
    view = _new_view()
    _select_row(view, "ptt_backend")
    result = view.handle_key(ord(" "))
    assert result is NOTHING
    assert view.ptt_backend == "serial-line"


def test_selector_and_bool_rows_toggle():
    view = _new_view()
    _select_row(view, "ptt_backend")
    _enter(view)
    assert view.ptt_backend == "serial-line"

    _select_row(view, "line")
    assert view.backend_config["serial-line"]["line"] == "rts"
    _enter(view)
    assert view.backend_config["serial-line"]["line"] == "dtr"
    view.handle_key(ord(" "))
    assert view.backend_config["serial-line"]["line"] == "rts"

    _select_row(view, "active_high")
    assert view.backend_config["serial-line"]["active_high"] is True
    _enter(view)
    assert view.backend_config["serial-line"]["active_high"] is False


# -- validation failures --

def _goto_backend(view, backend):
    while view.ptt_backend != backend:
        _select_row(view, "ptt_backend")
        _enter(view)


def _fill_text(view, key, text):
    _select_row(view, key)
    _enter(view)
    _clear_edit_buffer(view)
    _type(view, text)
    _enter(view)


def test_save_fails_on_empty_name():
    view = _new_view()
    _fill_text(view, "description", "desc")
    _fill_text(view, "audio_name", "Card")
    _select_row(view, "save")
    result = _enter(view)
    assert result is NOTHING
    assert "name" in view.status.lower()


def test_save_fails_on_name_collision_with_other_names():
    view = _new_view(other_names=["taken"])
    _fill_text(view, "name", "taken")
    _fill_text(view, "description", "desc")
    _fill_text(view, "audio_name", "Card")
    _select_row(view, "save")
    result = _enter(view)
    assert result is NOTHING
    assert "taken" in view.status
    assert "already exists" in view.status


def test_save_fails_on_missing_serial_line_port():
    view = _new_view()
    _fill_text(view, "name", "ht")
    _fill_text(view, "description", "desc")
    _fill_text(view, "audio_name", "Card")
    _goto_backend(view, "serial-line")
    _select_row(view, "save")
    result = _enter(view)
    assert result is NOTHING
    assert "port" in view.status.lower()


def test_save_fails_on_missing_icom_civ_usb_id():
    view = _new_view()
    _fill_text(view, "name", "ic705")
    _fill_text(view, "description", "desc")
    _fill_text(view, "audio_name", "Card")
    _goto_backend(view, "icom-civ")
    _select_row(view, "save")
    result = _enter(view)
    assert result is NOTHING
    assert "usb_id" in view.status.lower()


def test_save_fails_on_bad_baud():
    view = _new_view()
    _fill_text(view, "name", "ht")
    _fill_text(view, "description", "desc")
    _fill_text(view, "audio_name", "Card")
    _goto_backend(view, "serial-line")
    _fill_text(view, "port", "COM5")
    _fill_text(view, "baud", "not-a-number")
    _select_row(view, "save")
    result = _enter(view)
    assert result is NOTHING
    assert "baud" in view.status.lower()
    assert "whole number" in view.status.lower()


def test_save_fails_on_bad_icom_address():
    view = _new_view()
    _fill_text(view, "name", "ic705")
    _fill_text(view, "description", "desc")
    _fill_text(view, "audio_name", "Card")
    _goto_backend(view, "icom-civ")
    _fill_text(view, "usb_id", "0C26:0036")
    _fill_text(view, "address", "not-hex-either")
    _select_row(view, "save")
    result = _enter(view)
    assert result is NOTHING
    assert "address" in view.status.lower()
    assert "integer" in view.status.lower()


# -- full valid saves --

def test_full_valid_save_vox():
    captured = {}

    def on_done(old_name, new_name, radio):
        captured["old_name"] = old_name
        captured["new_name"] = new_name
        captured["radio"] = radio

    view = _new_view(on_done=on_done)
    _fill_text(view, "name", "handheld")
    _fill_text(view, "description", "Handheld VOX radio")
    _fill_text(view, "audio_name", "USB Audio")
    _select_row(view, "save")
    result = _enter(view)

    assert result is POP
    assert captured["old_name"] is None
    assert captured["new_name"] == "handheld"
    assert captured["radio"] == Radio("handheld", "Handheld VOX radio", "USB Audio", "vox", {})


def test_full_valid_save_icom_civ_hex_address():
    captured = {}

    def on_done(old_name, new_name, radio):
        captured["result"] = (old_name, new_name, radio)

    view = _new_view(on_done=on_done)
    _fill_text(view, "name", "ic705")
    _fill_text(view, "description", "IC-705")
    _fill_text(view, "audio_name", "IC-705")
    _goto_backend(view, "icom-civ")
    _fill_text(view, "usb_id", "0C26:0036")
    _fill_text(view, "radio_name", "IC-705")
    _fill_text(view, "address", "0xA4")
    _select_row(view, "save")
    result = _enter(view)

    assert result is POP
    old_name, new_name, radio = captured["result"]
    assert old_name is None
    assert new_name == "ic705"
    assert radio == Radio(
        "ic705", "IC-705", "IC-705", "icom-civ",
        {"usb_id": "0C26:0036", "radio_name": "IC-705", "address": 164},
    )
    assert isinstance(radio.ptt_config["address"], int)


def test_full_valid_save_serial_line_defaults_baud_and_line():
    captured = {}

    def on_done(old_name, new_name, radio):
        captured["result"] = (old_name, new_name, radio)

    view = _new_view(on_done=on_done)
    _fill_text(view, "name", "ht")
    _fill_text(view, "description", "HT via Digirig")
    _fill_text(view, "audio_name", "USB Audio")
    _goto_backend(view, "serial-line")
    _fill_text(view, "port", "COM5")
    _select_row(view, "save")
    result = _enter(view)

    assert result is POP
    _, _, radio = captured["result"]
    assert radio.ptt_config == {"port": "COM5", "line": "rts", "baud": 9600, "active_high": True}


def test_full_valid_save_hamlib_omits_blank_optional_fields():
    captured = {}

    def on_done(old_name, new_name, radio):
        captured["result"] = (old_name, new_name, radio)

    view = _new_view(on_done=on_done)
    _fill_text(view, "name", "ft991a")
    _fill_text(view, "description", "FT-991A")
    _fill_text(view, "audio_name", "USB Audio")
    _goto_backend(view, "hamlib")
    _fill_text(view, "model", "3081")
    _select_row(view, "save")
    result = _enter(view)

    assert result is POP
    _, _, radio = captured["result"]
    assert radio.ptt_config == {"model": 3081}  # device/baud/civaddr/timeout/retry all absent


def test_cancel_pops_without_calling_on_done():
    called = []
    view = _new_view(on_done=lambda *a: called.append(a))
    _select_row(view, "cancel")
    result = _enter(view)
    assert result is POP
    assert called == []


def test_esc_pops_without_calling_on_done():
    called = []
    view = _new_view(on_done=lambda *a: called.append(a))
    result = view.handle_key(27)
    assert result is POP
    assert called == []


# -- RadioListView._apply_edit --

def _list_view(names, default=None, selected=0):
    radios = {name: Radio(name, "d", "Card", "vox", {}) for name in names}
    return RadioListView(path="unused.toml", radios=radios, default=default, selected=selected)


def test_apply_edit_add_inserts_under_new_name():
    view = _list_view(["a"])
    new_radio = Radio("b", "desc", "Card", "vox", {})
    view._apply_edit(None, "b", new_radio)
    assert view.radios["b"] == new_radio
    assert view.radios.keys() == {"a", "b"}
    assert view.dirty is True


def test_apply_edit_edit_without_rename_replaces_in_place():
    view = _list_view(["a", "b"], default="a")
    updated = Radio("a", "new desc", "Card2", "vox", {})
    view._apply_edit("a", "a", updated)
    assert view.radios["a"] == updated
    assert view.radios.keys() == {"a", "b"}
    assert view.default == "a"


def test_apply_edit_edit_with_rename_moves_entry():
    view = _list_view(["a", "b"], default="b")
    updated = Radio("renamed", "desc", "Card", "vox", {})
    view._apply_edit("a", "renamed", updated)
    assert view.radios.keys() == {"renamed", "b"}
    assert "a" not in view.radios
    assert view.default == "b"  # unrelated default is untouched


def test_apply_edit_rename_of_current_default_carries_default_along():
    view = _list_view(["a", "b"], default="a")
    updated = Radio("renamed", "desc", "Card", "vox", {})
    view._apply_edit("a", "renamed", updated)
    assert view.radios.keys() == {"renamed", "b"}
    assert view.default == "renamed"  # must not dangle


# -- RadioListView key bindings for add/edit --

def test_a_pushes_blank_detail_view():
    view = _list_view(["a"])
    result = view.handle_key(ord("a"))
    from whale.radio_config_tui import Push
    assert isinstance(result, Push)
    detail = result.view
    assert isinstance(detail, RadioDetailView)
    assert detail.old_name is None
    assert detail.other_names == ["a"]


def test_enter_pushes_prepopulated_detail_view():
    view = _list_view(["a", "b"], selected=1)
    result = view.handle_key(curses.KEY_ENTER)
    from whale.radio_config_tui import Push
    assert isinstance(result, Push)
    detail = result.view
    assert detail.old_name == "b"
    assert detail.name == "b"
    assert detail.other_names == ["a"]


def test_enter_on_empty_list_is_noop():
    view = _list_view([])
    result = view.handle_key(curses.KEY_ENTER)
    assert result is NOTHING


def test_enter_alternate_keycodes_10_and_13_also_work():
    view = _list_view(["a"])
    from whale.radio_config_tui import Push
    assert isinstance(view.handle_key(10), Push)
    assert isinstance(view.handle_key(13), Push)


# -- ListPickerView --

def test_list_picker_filtering_narrows_list_and_enter_selects_filtered_item():
    items = ["apple", "banana", "avocado", "cherry"]
    selected = []
    picker = ListPickerView("fruit", items, lambda s: s, selected.append, search_key=lambda s: s)

    _type(picker, "av")
    assert picker._filtered() == ["avocado"]

    result = picker.handle_key(curses.KEY_ENTER)
    assert result is POP
    assert selected == ["avocado"]


def test_list_picker_esc_calls_nothing():
    items = ["a", "b"]
    selected = []
    picker = ListPickerView("t", items, lambda s: s, selected.append, search_key=lambda s: s)
    result = picker.handle_key(27)
    assert result is POP
    assert selected == []


def test_list_picker_selection_index_tracks_filtered_list_not_unfiltered():
    # Classic off-by-source-list bug: highlighting index 0 after filtering
    # to a subset must select the *filtered* item at 0, not items[0].
    items = ["zebra", "apple", "zoo"]
    selected = []
    picker = ListPickerView("t", items, lambda s: s, selected.append, search_key=lambda s: s)

    _type(picker, "z")
    assert picker._filtered() == ["zebra", "zoo"]
    picker.handle_key(curses.KEY_DOWN)
    assert picker.highlighted == 1
    picker.handle_key(curses.KEY_ENTER)
    assert selected == ["zoo"]  # not items[1] == "apple"


def test_list_picker_backspace_removes_query_char():
    items = ["one", "two"]
    picker = ListPickerView("t", items, lambda s: s, lambda x: None, search_key=lambda s: s)
    _type(picker, "on")
    assert picker.query == "on"
    picker.handle_key(curses.KEY_BACKSPACE)
    assert picker.query == "o"


def test_list_picker_no_search_key_uses_j_k_and_arrows_to_move():
    items = ["a", "b", "c"]
    picker = ListPickerView("t", items, lambda s: s, lambda x: None)
    picker.handle_key(curses.KEY_DOWN)
    assert picker.highlighted == 1
    picker.handle_key(ord("j"))
    assert picker.highlighted == 2
    picker.handle_key(ord("k"))
    assert picker.highlighted == 1


def test_list_picker_enter_on_empty_filtered_list_is_noop():
    items = ["apple"]
    selected = []
    picker = ListPickerView("t", items, lambda s: s, selected.append, search_key=lambda s: s)
    _type(picker, "zzz")
    result = picker.handle_key(curses.KEY_ENTER)
    assert result is NOTHING
    assert selected == []


# -- hardware-backed fields: real degrade paths --

def test_audio_preview_degrades_when_list_devices_raises(monkeypatch):
    def boom(*a, **k):
        raise LookupError("no host API matching 'core audio'")
    monkeypatch.setattr(audio_io, "find_devices", boom)
    monkeypatch.setattr(audio_io, "list_devices", boom)

    view = _new_view()
    view.audio_name = "USB Audio"
    assert view._audio_preview() == "device list unavailable"


def test_audio_preview_reports_match_counts(monkeypatch):
    Device = audio_io.AudioDevice
    devices = [
        Device(index=0, name="USB Audio CODEC", host_api="Core Audio",
               max_input_channels=2, max_output_channels=0, default_samplerate=48000.0),
        Device(index=1, name="Built-in Mic", host_api="Core Audio",
               max_input_channels=1, max_output_channels=0, default_samplerate=48000.0),
    ]
    monkeypatch.setattr(audio_io, "find_devices",
                         lambda name, kind: [0] if kind == "input" else [])
    monkeypatch.setattr(audio_io, "list_devices", lambda kind=None: devices)

    view = _new_view()
    view.audio_name = "USB"
    preview = view._audio_preview()
    assert "input: 1 match (USB Audio CODEC)" in preview
    assert "output: 0 matches" in preview


def test_audio_picker_shows_status_and_manual_entry_still_works_when_list_devices_raises(monkeypatch):
    def boom(*a, **k):
        raise LookupError("no PortAudio host API")
    monkeypatch.setattr(audio_io, "list_devices", boom)

    view = _new_view()
    _select_row(view, "audio_name")
    result = view.handle_key(ord("p"))
    assert result is NOTHING
    assert "device list unavailable" in view.status

    # Manual typed entry still works after the picker degrade.
    _enter(view)
    _type(view, "USB Audio")
    _enter(view)
    assert view.audio_name == "USB Audio"


def test_hamlib_picker_shows_status_and_manual_model_entry_still_works_when_unavailable(monkeypatch):
    def boom():
        raise OSError("could not load libhamlib")
    monkeypatch.setattr(hamlib, "list_rig_models", boom)

    view = _new_view()
    _goto_backend(view, "hamlib")
    _select_row(view, "model")
    result = view.handle_key(ord("p"))
    assert result is NOTHING
    assert "hamlib model list unavailable" in view.status

    # Manual typed entry of a model number still works.
    _enter(view)
    _type(view, "3081")
    _enter(view)
    assert view.backend_config["hamlib"]["model"] == "3081"


def test_serial_port_picker_degrades_on_exception(monkeypatch):
    def boom():
        raise OSError("no comports backend")
    monkeypatch.setattr(ptt, "list_serial_ports", boom)

    view = _new_view()
    _goto_backend(view, "serial-line")
    _select_row(view, "port")
    result = view.handle_key(ord("p"))
    assert result is NOTHING
    assert "serial port list unavailable" in view.status


def test_hamlib_picker_selects_model_and_autofills_blank_description(monkeypatch):
    RigModel = hamlib.RigModel
    models = [RigModel(model=3081, manufacturer="Icom", model_name="IC-7300",
                        version="1.0", status="Stable")]
    monkeypatch.setattr(hamlib, "list_rig_models", lambda: models)

    view = _new_view()
    _goto_backend(view, "hamlib")
    _select_row(view, "model")
    result = view.handle_key(ord("p"))
    from whale.radio_config_tui import Push
    assert isinstance(result, Push)
    picker = result.view

    picker.handle_key(curses.KEY_ENTER)
    assert view.backend_config["hamlib"]["model"] == "3081"
    assert view.description == "Icom IC-7300"


# -- render() regression: every row of every backend must actually draw --

def test_render_does_not_crash_on_any_row_of_any_backend():
    """Walks the selection over every row (including Save/Cancel) for every
    PTT backend and calls the real render(), catching the KeyError('save')
    class of bug that every handle_key()-only test above cannot see.
    """
    screen = _FakeScreen()
    for backend in RadioDetailView.BACKEND_ORDER:
        view = _new_view()
        _goto_backend(view, backend)
        rows = view._rows()
        for index in range(len(rows)):
            view.selected = index
            view.render(screen)  # must not raise


def test_render_does_not_crash_while_mid_text_edit():
    screen = _FakeScreen()
    view = _new_view()
    _select_row(view, "name")
    _enter(view)
    _type(view, "partial")
    view.render(screen)  # must not raise while editing_field is set


def test_list_picker_render_does_not_crash():
    screen = _FakeScreen()
    picker = ListPickerView("t", ["a", "b"], lambda s: s, lambda x: None, search_key=lambda s: s)
    picker.render(screen)
    _type(picker, "a")
    picker.render(screen)


def test_serial_picker_for_icom_leaves_usb_id_unchanged_when_port_has_no_usb_id(monkeypatch):
    SerialPort = ptt.SerialPort
    ports = [SerialPort(device="/dev/ttyS0", description="Built-in serial",
                         manufacturer=None, usb_id=None)]
    monkeypatch.setattr(ptt, "list_serial_ports", lambda: ports)

    view = _new_view()
    view.backend_config["icom-civ"]["usb_id"] = "unchanged"
    _goto_backend(view, "icom-civ")
    _select_row(view, "usb_id")
    result = view.handle_key(ord("p"))
    from whale.radio_config_tui import Push
    assert isinstance(result, Push)
    picker = result.view
    picker.handle_key(curses.KEY_ENTER)

    assert view.backend_config["icom-civ"]["usb_id"] == "unchanged"
    assert "no USB VID:PID" in view.status
