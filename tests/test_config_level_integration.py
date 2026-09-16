import curses

from whale import level_tui
from whale.config_tui import (App, ConfigView, ListPickerView, NOTHING, Push,
                              RunLevels)
from whale.hw.radios import Radio


def _radio(name: str, level_db: float = -12) -> Radio:
    return Radio(name, name.title(), f"{name} input", f"{name} output", "vox",
                 frozenset({"fm"}), {}, level_db)


def _view(path, names=("tx", "rx")) -> ConfigView:
    view = ConfigView(str(path), "N0CALL", None,
                      {name: _radio(name) for name in names})
    view.selected = view.SETTING_COUNT
    return view


def test_levels_picker_defaults_to_single_radio_measurement(tmp_path):
    view = _view(tmp_path / "config.toml", ("tx",))

    result = view.handle_key(ord("l"))

    assert isinstance(result, Push)
    assert isinstance(result.view, ListPickerView)
    assert result.view.items == [None]
    assert result.view.format_item(None) == "None — single-radio levels only"
    selected = result.view.handle_key(curses.KEY_ENTER)
    assert isinstance(selected, RunLevels)
    assert (selected.transmit_name, selected.receive_name) == ("tx", None)


def test_levels_picker_offers_no_receiver_before_other_radios(tmp_path):
    view = _view(tmp_path / "config.toml")

    result = view.handle_key(ord("l"))

    assert isinstance(result, Push)
    assert result.view.items == [None, "rx"]


def test_levels_receiver_picker_excludes_transmitting_radio(tmp_path):
    view = _view(tmp_path / "config.toml", ("tx", "monitor-a", "monitor-b"))

    result = view.handle_key(ord("l"))
    assert isinstance(result, Push)
    assert isinstance(result.view, ListPickerView)
    assert result.view.items == [None, "monitor-a", "monitor-b"]

    result.view.highlighted = 2
    selected = result.view.handle_key(curses.KEY_ENTER)
    assert isinstance(selected, RunLevels)
    assert (selected.transmit_name, selected.receive_name) == ("tx", "monitor-b")


class _Screen:
    def __init__(self):
        self.timeouts = []

    def timeout(self, value):
        self.timeouts.append(value)


def test_over_air_level_test_uses_selected_transmitter_and_applies_to_it(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("original file stays untouched")
    view = _view(path)
    screen = _Screen()
    monkeypatch.setattr(Radio, "devices",
                        lambda self: (3, 4) if self.id == "tx" else (5, 6))

    def run_tuner(_screen, tx, receiver, context, tx_device, rx_device,
                  on_apply, distortion_enabled, *, apply_label,
                  probe_analysis_available):
        assert (tx.id, receiver.id) == ("tx", "rx")
        assert (context, tx_device, rx_device) == (str(path), 3, 6)
        assert distortion_enabled is True
        assert probe_analysis_available is True
        assert apply_label == "Apply"
        on_apply(-7)

    monkeypatch.setattr(level_tui, "run_level_tuner", run_tuner)

    App._run_levels(screen, RunLevels(view, "tx", "rx"))

    assert view.radios["tx"].tx_level_db == -7
    assert view.radios["rx"].tx_level_db == -12
    assert view.dirty is True
    assert "unsaved" in view.status.lower()
    assert path.read_text() == "original file stays untouched"
    assert screen.timeouts == [-1]


def test_local_level_test_uses_selected_radio_input_without_probe_analysis(tmp_path, monkeypatch):
    view = _view(tmp_path / "config.toml", ("tx",))
    screen = _Screen()
    monkeypatch.setattr(Radio, "devices", lambda self: (3, 4))

    def run_tuner(_screen, tx, receiver, _context, tx_device, rx_device,
                  _on_apply, distortion_enabled, *, apply_label,
                  probe_analysis_available):
        assert tx.id == "tx"
        assert receiver.id == "tx"
        assert (tx_device, rx_device) == (3, 4)
        assert distortion_enabled is False
        assert probe_analysis_available is False
        assert apply_label == "Apply"

    monkeypatch.setattr(level_tui, "run_level_tuner", run_tuner)

    App._run_levels(screen, RunLevels(view, "tx", None))
    assert screen.timeouts == [-1]


def test_level_tuner_error_returns_to_config_with_status(tmp_path, monkeypatch):
    view = _view(tmp_path / "config.toml")
    screen = _Screen()
    monkeypatch.setattr(Radio, "devices", lambda self: (3, 4))
    monkeypatch.setattr(level_tui, "run_level_tuner",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            RuntimeError("audio device busy")))

    App._run_levels(screen, RunLevels(view, "tx", "rx"))

    assert view.dirty is False
    assert "audio device busy" in view.status
    assert screen.timeouts == [-1]
