from pathlib import Path

from whale import paths


def test_linux_platform_paths_follow_xdg_variables():
    env = {"XDG_CONFIG_HOME": "/cfg", "XDG_STATE_HOME": "/state"}
    assert paths.platform_config_path(
        platform="linux", environ=env, home=Path("/home/me")) == Path("/cfg/whale/config.toml")
    assert paths.platform_state_dir(
        platform="linux", environ=env, home=Path("/home/me")) == Path("/state/whale")


def test_macos_platform_paths_use_application_support():
    home = Path("/Users/me")
    expected = home / "Library" / "Application Support" / "Whale"
    assert paths.platform_config_dir(platform="darwin", environ={}, home=home) == expected
    assert paths.platform_state_dir(platform="darwin", environ={}, home=home) == expected


def test_windows_platform_paths_are_machine_local():
    env = {"LOCALAPPDATA": "C:/Users/me/AppData/Local"}
    expected = Path(env["LOCALAPPDATA"]) / "Whale"
    assert paths.platform_config_dir(platform="win32", environ=env, home=Path("C:/Users/me")) == expected
    assert paths.platform_state_dir(platform="win32", environ=env, home=Path("C:/Users/me")) == expected


def test_config_resolution_prefers_explicit_then_environment_then_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local = tmp_path / "config.toml"
    local.touch()
    monkeypatch.setenv("WHALE_CONFIG", "environment.toml")
    assert paths.config_path("explicit.toml") == Path("explicit.toml")
    assert paths.config_path() == Path("environment.toml")
    monkeypatch.delenv("WHALE_CONFIG")
    assert paths.config_path() == local


def test_missing_local_config_uses_platform_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WHALE_CONFIG", raising=False)
    expected = tmp_path / "platform" / "config.toml"
    monkeypatch.setattr(paths, "platform_config_path", lambda: expected)
    assert paths.config_path() == expected


def test_mode_history_is_sidecar_except_for_platform_config(tmp_path, monkeypatch):
    installed_config = tmp_path / "config" / "config.toml"
    state = tmp_path / "state"
    monkeypatch.setattr(paths, "platform_config_path", lambda: installed_config)
    monkeypatch.setattr(paths, "platform_state_dir", lambda: state)
    assert paths.mode_history_path(installed_config) == state / "mode-history.json"
    local = tmp_path / "checkout" / "config.toml"
    assert paths.mode_history_path(local) == local.with_name("config.toml.mode-history.json")


def test_server_paths_keep_current_directory_config_and_history_together(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WHALE_CONFIG", raising=False)
    config = tmp_path / "config.toml"
    config.touch()
    assert paths.server_paths() == (
        config, tmp_path / "config.toml.mode-history.json")


def test_server_paths_keep_environment_config_and_history_together(tmp_path, monkeypatch):
    config = tmp_path / "portable" / "station.toml"
    monkeypatch.setenv("WHALE_CONFIG", str(config))
    assert paths.server_paths() == (
        config, config.with_name("station.toml.mode-history.json"))


def test_server_paths_put_installed_history_in_platform_state(tmp_path, monkeypatch):
    config = tmp_path / "config" / "config.toml"
    state = tmp_path / "state"
    monkeypatch.delenv("WHALE_CONFIG", raising=False)
    monkeypatch.setattr(paths, "platform_config_path", lambda: config)
    monkeypatch.setattr(paths, "platform_state_dir", lambda: state)
    monkeypatch.setattr(paths.Path, "is_file", lambda self: False)
    assert paths.server_paths() == (config, state / "mode-history.json")
