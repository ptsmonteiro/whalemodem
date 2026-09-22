import argparse
import tomllib
from pathlib import Path

import pytest

from whale.version import __version__, add_version_argument


def test_version_is_the_installed_project_version():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert __version__ == declared


def test_common_version_argument_exits_without_starting_the_application(capsys):
    parser = argparse.ArgumentParser(prog="whale-command")
    add_version_argument(parser)

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"whale-command {__version__}\n"
