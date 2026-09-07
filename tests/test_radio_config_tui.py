import curses

from whale.hw.radios import Radio, load_radios
from whale.radio_config_tui import NOTHING, QUIT, RadioListView


def _radio(name, description="d", audio_input_name="AudioCard", audio_output_name="AudioCard",
           ptt_backend="vox"):
    return Radio(name, description, audio_input_name, audio_output_name, ptt_backend, {})


def _view(names, default=None, selected=0):
    radios = {name: _radio(name) for name in names}
    return RadioListView(path="unused.toml", radios=radios, default=default, selected=selected)


def test_move_selection_down_and_up_clamps_at_boundaries():
    view = _view(["a", "b", "c"])
    assert view.selected == 0

    view.handle_key(curses.KEY_UP)
    assert view.selected == 0  # clamps, does not wrap

    view.handle_key(curses.KEY_DOWN)
    view.handle_key(curses.KEY_DOWN)
    assert view.selected == 2

    view.handle_key(ord("j"))
    assert view.selected == 2  # clamps at the bottom too

    view.handle_key(ord("k"))
    assert view.selected == 1


def test_move_selection_on_empty_inventory_does_not_crash():
    view = _view([])
    result = view.handle_key(curses.KEY_DOWN)
    assert result is NOTHING
    assert view.selected == 0


def test_d_sets_selected_radio_as_default():
    view = _view(["a", "b"], default=None, selected=1)
    result = view.handle_key(ord("d"))
    assert result is NOTHING
    assert view.default == "b"
    assert view.dirty is True


def test_delete_then_yes_removes_radio_and_clears_default_if_it_was_default():
    view = _view(["a", "b"], default="a", selected=0)
    result = view.handle_key(ord("x"))
    assert result is NOTHING
    assert view.pending == "delete"

    result = view.handle_key(ord("y"))
    assert result is NOTHING
    assert view.pending is None
    assert "a" not in view.radios
    assert view.default is None


def test_delete_then_no_cancels_and_changes_nothing():
    view = _view(["a", "b"], default="a", selected=0)
    view.handle_key(ord("x"))
    result = view.handle_key(ord("n"))
    assert result is NOTHING
    assert view.pending is None
    assert view.radios.keys() == {"a", "b"}
    assert view.default == "a"
    assert view.dirty is False


def test_delete_then_esc_cancels_like_no():
    view = _view(["a", "b"], default="a", selected=0)
    view.handle_key(ord("x"))
    result = view.handle_key(27)
    assert result is NOTHING
    assert view.pending is None
    assert "a" in view.radios


def test_deleting_down_to_one_remaining_radio_makes_it_the_implicit_default():
    view = _view(["a", "b"], default="a", selected=0)
    view.handle_key(ord("x"))
    view.handle_key(ord("y"))
    assert view.radios.keys() == {"b"}
    assert view.default is None  # no explicit default was set on it
    assert view.effective_default() == "b"  # but it behaves as the default in the view


def test_deleting_down_to_zero_radios_leaves_empty_state_and_refuses_save(tmp_path):
    view = _view(["only"], default="only", selected=0)
    view.handle_key(ord("x"))
    view.handle_key(ord("y"))
    assert view.radios == {}
    assert view.default is None

    path = tmp_path / "radios.toml"
    view.path = str(path)
    result = view.handle_key(ord("s"))
    assert result is NOTHING
    assert not path.exists()
    assert "empty" in view.status.lower() or "nothing to save" in view.status.lower()


def test_quit_without_unsaved_changes_quits_immediately():
    view = _view(["a"], default="a")
    assert view.dirty is False
    result = view.handle_key(ord("q"))
    assert result is QUIT


def test_quit_with_unsaved_changes_prompts_then_saves_on_yes(tmp_path):
    path = tmp_path / "radios.toml"
    view = _view(["a", "b"], default="a")
    view.path = str(path)
    view.handle_key(ord("d"))  # dirties state (sets default to "a" again, still dirty=True)
    assert view.dirty is True

    result = view.handle_key(ord("q"))
    assert result is NOTHING
    assert view.pending == "quit"

    result = view.handle_key(ord("y"))
    assert result is QUIT
    assert path.exists()


def test_quit_with_unsaved_changes_no_quits_without_saving(tmp_path):
    path = tmp_path / "radios.toml"
    view = _view(["a", "b"], default="a")
    view.path = str(path)
    view.handle_key(ord("d"))
    view.handle_key(ord("q"))

    result = view.handle_key(ord("n"))
    assert result is QUIT
    assert not path.exists()


def test_quit_with_unsaved_changes_esc_cancels_quit():
    view = _view(["a", "b"], default="a")
    view.handle_key(ord("d"))
    view.handle_key(ord("q"))

    result = view.handle_key(27)
    assert result is NOTHING
    assert view.pending is None


def test_quit_prompt_refusing_empty_save_does_not_quit(tmp_path):
    path = tmp_path / "radios.toml"
    view = _view(["only"], default="only")
    view.path = str(path)
    view.handle_key(ord("x"))
    view.handle_key(ord("y"))  # now empty, dirty=True
    assert view.dirty is True

    view.handle_key(ord("q"))
    assert view.pending == "quit"

    result = view.handle_key(ord("y"))
    # Save is refused (empty inventory) -- must not quit and silently lose
    # the fact the file was never written.
    assert result is NOTHING
    assert not path.exists()


def test_full_load_mutate_save_round_trip(tmp_path):
    path = tmp_path / "radios.toml"
    path.write_text(
        'default_radio = "shack-icom"\n\n'
        "[radios.shack-icom]\n"
        'description = "Shack IC-7300"\n'
        'audio.input = "IC-7300"\n'
        'audio.output = "IC-7300"\n'
        'ptt.backend = "icom-civ"\n'
        "ptt.address = 148\n\n"
        "[radios.portable]\n"
        'description = "Portable HT"\n'
        'audio.input = "USB Audio"\n'
        'audio.output = "USB Audio"\n'
        'ptt.backend = "serial-line"\n'
        'ptt.port = "COM5"\n'
        'ptt.line = "rts"\n\n'
    )

    inventory = load_radios(path)
    view = RadioListView(path=str(path), radios=dict(inventory.radios), default=inventory.default)

    # Delete the current default, then set the remaining radio as default.
    view.selected = view._names().index("shack-icom")
    view.handle_key(ord("x"))
    view.handle_key(ord("y"))
    assert view.default is None
    assert view.radios.keys() == {"portable"}

    view.handle_key(ord("d"))
    assert view.default == "portable"

    result = view.handle_key(ord("s"))
    assert result is NOTHING
    assert view.status.startswith("Saved")

    reloaded = load_radios(path)
    assert reloaded.default == "portable"
    assert reloaded.radios.keys() == {"portable"}
    assert reloaded.radios["portable"].description == "Portable HT"
    assert reloaded.radios["portable"].ptt_backend == "serial-line"
    assert reloaded.radios["portable"].ptt_config == {"port": "COM5", "line": "rts"}


class _FakeScreen:
    """Enough of a curses window for render() to run with no real terminal."""

    def __init__(self, height=24, width=80):
        self._height, self._width = height, width

    def getmaxyx(self):
        return (self._height, self._width)

    def addnstr(self, y, x, text, n, attr=0):
        pass


def test_render_does_not_crash_empty_or_populated_or_pending():
    screen = _FakeScreen()
    _view([]).render(screen)
    view = _view(["a", "b"], default="a")
    view.render(screen)
    view.handle_key(ord("x"))
    view.render(screen)  # delete-confirm status line
    view.handle_key(ord("n"))
    view.handle_key(ord("d"))
    view.handle_key(ord("q"))
    view.render(screen)  # quit-confirm status line


def test_render_does_not_crash_on_a_terminal_too_small():
    _view(["a"]).render(_FakeScreen(height=2, width=10))
