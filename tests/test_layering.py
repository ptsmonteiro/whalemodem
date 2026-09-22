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


def _family_base_classes(root: Path) -> set[str]:
    """Names of classes under `root` that declare `family_base = True`.

    A parametric waveform family base (e.g. `ScFdeMode`) is a PHY class
    that a rung in whale/modes/ legitimately subclasses without importing
    whale.waveform itself -- the marker is a plain class attribute so this
    check needs no import of whale/ and can't be fooled by a docstring.
    """
    names: set[str] = set()
    for path in _python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                targets = (stmt.targets if isinstance(stmt, ast.Assign)
                           else [stmt.target] if isinstance(stmt, ast.AnnAssign)
                           else [])
                if any(isinstance(t, ast.Name) and t.id == "family_base"
                       for t in targets):
                    names.add(node.name)
    return names


def _imported_names(path: Path) -> set[str]:
    """Bare names imported by `path` via `from ... import name`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def test_modes_holds_only_link_facing_adapters():
    """A PHY filed under whale/modes/ is invisible to the tests above.

    They check the direction of imports, not where a module lives, so a
    complete waveform dropped into the adapters package breaks none of them
    -- which is how whale/modes/{hc0,hc1w,hr0}.py sat there. Every module
    here is one of: a link-facing adapter that imports whale.waveform, a
    mode that subclasses a sibling adapter, or a parameterisation of a
    declared parametric waveform family from whale/phy/ (a class there
    marked `family_base = True`, as vfs2 and vfs3 are rungs of
    `whale.phy.scfde.ScFdeMode` and vf16 is a rung of
    `whale.phy.vf12.Vf12Waveform`). A module that is none of
    these is a PHY and belongs in whale/phy/.
    """
    families = _family_base_classes(WHALE / "phy")
    offenders = []
    for path in _python_files(WHALE / "modes"):
        if path.name == "__init__.py":
            continue
        modules = [module for module, _ in _imported_modules(path)]
        if any(_is_within(module, "whale.waveform") for module in modules):
            continue
        # A mode may instead subclass a sibling adapter.
        if any(_is_within(module, "whale.modes") for module in modules):
            continue
        # Or parameterise a declared parametric waveform family from phy/.
        if _imported_names(path) & families:
            continue
        offenders.append(str(path))
    assert not offenders, (
        "every module in whale/modes/ is a link-facing WaveformMode adapter "
        "importing whale.waveform, a mode subclassing a sibling that does, "
        "or a parameterisation of a declared parametric family from "
        "whale/phy/ (a class marked family_base = True); these are none of "
        "those, so they are complete waveforms and belong in whale/phy/:\n  "
        + "\n  ".join(offenders))
