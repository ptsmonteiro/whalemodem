"""Whale application version, shared by every command."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


def _application_version() -> str:
    # In a checkout, pyproject.toml is authoritative even if another Whale
    # version is installed in the active environment. Installed packages and
    # frozen releases use the distribution metadata embedded by the build.
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.is_file():
        try:
            project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
            if project.get("name") == "whale":
                return str(project["version"])
        except (KeyError, OSError, tomllib.TOMLDecodeError):
            pass
    try:
        return version("whale")
    except PackageNotFoundError:
        return "unknown"


__version__ = _application_version()


def add_version_argument(parser) -> None:
    """Add the common, side-effect-free ``--version`` CLI option."""
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
