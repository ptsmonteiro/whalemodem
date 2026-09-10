"""The package boundary between shipped code and scratch work.

`whale/` is the product; `experiments/` is scratch and evidence. Production
DSP used to live under `experiments/` and be reached from `whale/` through
`sys.path` manipulation, which meant there was no boundary to enforce and no
way to retire an experiment directory without breaking the modem. These
tests are what keeps that from happening again.

Layering inside `whale/`, downward only:

    whale/dsp/    waveform-independent kernels
    whale/phy/    complete waveforms built from those kernels
    whale/modes/  link-facing `WaveformMode` adapters over a PHY

Imports are read with `ast` rather than grepped, so a mention inside a
docstring or a comment -- provenance notes naming the experiment a module
came from are deliberate and expected -- never trips these.
"""

from __future__ import annotations

import ast
from pathlib import Path

WHALE = Path(__file__).resolve().parents[1] / "whale"

def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py")
                  if "__pycache__" not in p.parts)


def _imported_modules(path: Path) -> list[tuple[str, int]]:
    """Every module name `path` imports, with the line it is imported on.

    Relative imports are resolved against the file's own package, so
    Relative imports are resolved against the file's own package.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = list(path.relative_to(WHALE.parent).with_suffix("").parts)
    if package[-1] == "__init__":
        package.pop()
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - node.level]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            # Only the fully qualified names, so `from whale.modes import
            found.extend((f"{prefix}.{alias.name}" if prefix else alias.name,
                          node.lineno) for alias in node.names)
    return found


def _is_within(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def test_whale_never_imports_experiments():
    offenders = []
    for path in _python_files(WHALE):
        for module, lineno in _imported_modules(path):
            if _is_within(module, "experiments"):
                offenders.append(f"{path}:{lineno} imports {module}")
    assert not offenders, (
        "whale/ must not import from experiments/ -- experiments/ is scratch "
        "work and evidence, and anything whale/ depends on belongs in "
        "whale/dsp/ (a primitive) or whale/phy/ (a complete waveform):\n  "
        + "\n  ".join(offenders))


def test_dsp_never_imports_upward():
    """The kernels are the bottom of the stack and depend on nothing above."""
    offenders = []
    for path in _python_files(WHALE / "dsp"):
        for module, lineno in _imported_modules(path):
            if (_is_within(module, "whale.phy")
                    or _is_within(module, "whale.modes")):
                offenders.append(f"{path}:{lineno} imports {module}")
    assert not offenders, (
        "whale/dsp/ must not import from whale/phy/ or whale/modes/ -- the "
        "kernels are waveform-independent, and anything that needs a "
        "waveform's frame geometry is a PHY, not a kernel:\n  "
        + "\n  ".join(offenders))


def test_phy_never_imports_modes():
    offenders = []
    for path in _python_files(WHALE / "phy"):
        for module, lineno in _imported_modules(path):
            if _is_within(module, "whale.modes"):
                offenders.append(f"{path}:{lineno} imports {module}")
    assert not offenders, (
        "whale/phy/ must not import from whale/modes/ -- the mode adapters "
        "sit above the PHYs, not below them:\n  " + "\n  ".join(offenders))
