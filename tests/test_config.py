from dataclasses import replace

import pytest

from whale.config import Config, get_radio, load_config, save_config
from whale.hw.radios import Radio
from whale.config_tui import ConfigView


def _radio(name: str, channels: set[str]) -> Radio:
    return Radio(name, name, "input", "output", "vox", frozenset(channels))


def test_config_round_trip_and_channel_defaults(tmp_path):
    path = tmp_path / "config.toml"
    original = Config("F4JAW", 2,
                      {"both": _radio("both", {"fm", "hf"}),
                       "hf": _radio("hf", {"hf"})},
                      default_fm_radio="both", default_hf_radio="hf",
                      cmd_port=8400, data_port=8401, log_file="logs/whale.log")
    save_config(path, original)

    loaded = load_config(path)
    assert loaded == original
    assert loaded.station_callsign == "F4JAW-2"
    assert get_radio(None, "fm", path).id == "both"
    assert get_radio(None, "hf", path).id == "hf"


def test_ssid_is_optional(tmp_path):
    path = tmp_path / "config.toml"
    save_config(path, Config("F4JAW", None, {"fm": _radio("fm", {"fm"})}, "fm"))
    assert load_config(path).station_callsign == "F4JAW"


def test_default_must_support_its_channel(tmp_path):
    path = tmp_path / "config.toml"
    config = Config("F4JAW", None, {"hf": _radio("hf", {"hf"})})
    with pytest.raises(ValueError, match="does not support 'fm'"):
        save_config(path, replace(config, default_fm_radio="hf"))


@pytest.mark.parametrize("ssid", [-1, 16, True, "2"])
def test_invalid_ssid_is_rejected(tmp_path, ssid):
    with pytest.raises(ValueError, match="ssid"):
        save_config(tmp_path / "config.toml",
                    Config("F4JAW", ssid, {"fm": _radio("fm", {"fm"})}))


def test_config_view_radio_rows_do_not_have_default_shortcuts():
    view = ConfigView("config.toml", "F4JAW", None,
                      {"fm": _radio("fm", {"fm"}), "hf": _radio("hf", {"hf"})})
    view.selected = view.SETTING_COUNT
    view.handle_key(ord("f"))
    view.handle_key(ord("h"))
    assert view.default_fm_radio is None
    assert view.default_hf_radio is None
    assert view.status == ""


def test_config_view_radio_rename_carries_both_defaults():
    old = _radio("both", {"fm", "hf"})
    view = ConfigView("config.toml", "F4JAW", None, {"both": old}, "both", "both")
    renamed = replace(old, id="renamed")
    view._apply_edit("both", "renamed", renamed)
    assert view.default_fm_radio == "renamed"
    assert view.default_hf_radio == "renamed"


def test_runtime_settings_have_defaults_when_omitted(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('callsign = "F4JAW"\n\n[radios.fm]\nname = "fm"\n'
                    'audio.input = "input"\naudio.output = "output"\n'
                    'channels = ["fm"]\nptt.backend = "vox"\n')
    config = load_config(path)
    assert (config.cmd_port, config.data_port, config.log_file) == (8300, 8301, None)


def test_config_view_validates_ports_and_allows_stderr():
    view = ConfigView("config.toml", "F4JAW", None, {"fm": _radio("fm", {"fm"})})
    assert view._set_port("cmd", "8301") == "Command and data ports must be different."
    assert view._set_port("cmd", "70000") == "Port must be an integer between 1 and 65535."
    assert view._set_port("cmd", "8400") is None
    assert view.cmd_port == 8400
    view.log_file = "old.log"
    assert view._set_log_file("  ") is None
    assert view.log_file is None
