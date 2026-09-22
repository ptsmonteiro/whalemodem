import json

from whale import mode_history, modes
from whale.link import Link
from link_harness import FakeTransport


def test_history_survives_restart_and_separates_radio_paths(tmp_path):
    path = tmp_path / "modes.json"
    first = mode_history.ModeHistory(path, "radio-a:fm")
    mode_history.record_good_mode(first, "STA1", "STA2", 19)

    assert mode_history.last_good_mode(
        mode_history.ModeHistory(path, "radio-a:fm"), "STA1", "STA2") == 19
    assert mode_history.last_good_mode(
        mode_history.ModeHistory(path, "radio-b:hf"), "STA1", "STA2") is None
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_only_a_faster_success_replaces_path_history():
    history = {}
    station = Link(FakeTransport(), "STA1", mode_history_store=history)
    station.peer_call = "STA2"
    ladder = list(modes.default_registry().supported_ids)

    station._remember_working_mode(ladder[1])
    station._remember_working_mode(ladder[0])

    assert mode_history.last_good_mode(history, "STA1", "STA2") == ladder[1]


def test_invalid_history_is_safe_to_ignore(tmp_path):
    path = tmp_path / "modes.json"
    path.write_text("not json", encoding="utf-8")

    assert mode_history.ModeHistory(path).get(("STA1", "STA2")) is None
