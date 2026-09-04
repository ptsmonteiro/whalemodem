# PyInstaller spec for a standalone whalemodem-server bundle.
#
# Built onedir (not onefile): this runs as a long-lived server, often on
# low-end hardware like a Raspberry Pi, so avoiding onefile's self-extraction
# cost on every process start matters more than shipping a single file.
# Only the *current build host's* vendored hamlib copy is bundled (trimming
# ~68MB of six-platform vendor data down to ~11-13MB for one), placed at the
# same relative path hamlib.py already looks it up from. On Linux hosts only,
# the vendored PortAudio .so is additionally bundled at the fixed
# `_vendor_portaudio/libportaudio.so.2` path that whale/hw/audio_io.py's
# frozen-mode preload hook expects.

import os
import sys

# `SPEC` is injected into this file's globals by PyInstaller at spec-eval
# time; it holds the path to this very .spec file.
_spec_dir = os.path.dirname(os.path.abspath(SPEC))
REPO_ROOT = os.path.dirname(os.path.dirname(_spec_dir))

sys.path.insert(0, REPO_ROOT)

from whale.hw._platform_tags import platform_tag  # noqa: E402

TAG = platform_tag()
if TAG is None:
    raise SystemExit(
        "whalemodem.spec: platform_tag() did not recognize this build host "
        f"(system={os.name!r}, sys.platform={sys.platform!r}). This build "
        "must run natively on one of the six platforms whale/hw/_vendor/ "
        "carries binaries for; see whale/hw/_platform_tags.py."
    )


def _vendor_datas(vendor_dir: str, dest_root: str) -> list[tuple[str, str]]:
    """Walks vendor_dir, returning (src_file, dest_dir) pairs for `datas=`.

    Preserves any subdirectory structure under vendor_dir, rooted at
    dest_root inside the frozen bundle.
    """
    entries = []
    for dirpath, _dirnames, filenames in os.walk(vendor_dir):
        rel_subdir = os.path.relpath(dirpath, vendor_dir)
        dest_dir = dest_root if rel_subdir == "." else os.path.join(dest_root, rel_subdir)
        for filename in filenames:
            entries.append((os.path.join(dirpath, filename), dest_dir))
    return entries


datas = _vendor_datas(
    os.path.join(REPO_ROOT, "whale", "hw", "_vendor", "hamlib", TAG),
    os.path.join("whale", "hw", "_vendor", "hamlib", TAG),
)

if TAG.startswith("linux-"):
    portaudio_lib = os.path.join(
        REPO_ROOT, "whale", "hw", "_vendor", "portaudio", TAG, "libportaudio.so.2"
    )
    datas.append((portaudio_lib, "_vendor_portaudio"))

# Deliberately empty: filled in from actual ModuleNotFoundErrors reported by
# running the frozen build, not guessed upfront (recent PyInstaller ships
# numpy/scipy hooks, so this list is expected to stay short).
hiddenimports = []

a = Analysis(
    [os.path.join(REPO_ROOT, "packaging", "pyinstaller", "entrypoint.py")],
    pathex=[REPO_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="whalemodem-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="whalemodem-server",
)
