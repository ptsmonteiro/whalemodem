#!/usr/bin/env python3
"""Fetches prebuilt libportaudio2 binaries for the Linux platforms a future
PyInstaller build needs, writing them into
whale/hw/_vendor/portaudio/<platform-tag>/libportaudio.so.2.

Rerun this whenever the pinned portaudio19 version below is bumped. See
whale/hw/_vendor/portaudio/SOURCES.md (written by this script) for exactly
what version and URL produced the files currently checked in.

Only Linux is vendored here: the `sounddevice` PyPI wheel already bundles
PortAudio on Windows and macOS (see docs/HARDWARE.md), so there is nothing
to fetch for those platforms -- just the three Linux tags whale/hw/hamlib.py
also targets. Sourced from Debian's official .deb packages, for the same
reason scripts/vendor_hamlib.py uses Debian rather than Homebrew-on-Linux
bottles for its own Linux legs (no baked-in RPATH into an unrelated
Homebrew-managed package tree -- see that script's docstring), and from
Debian trixie (stable) rather than sid, matching the distro/glibc baseline
this project already targets for hamlib/libusb.

This is a plain download-and-extract script: unlike vendor_hamlib.py there
is no macOS relocation step (install_name_tool/codesign) here, because
there is no macOS leg to relocate.
"""
from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent.parent / "whale" / "hw" / "_vendor" / "portaudio"

PORTAUDIO_VERSION = "19.7.0-1+b1"

# platform tag -> dict(url, note). Every entry is a Debian .deb archive, so
# (unlike vendor_hamlib.py's SOURCES table) there's no "kind" field needed.
SOURCES = {
    "linux-x86_64": dict(
        url=f"http://deb.debian.org/debian/pool/main/p/portaudio19/libportaudio2_{PORTAUDIO_VERSION}_amd64.deb",
        note="Debian trixie (stable) amd64",
    ),
    "linux-aarch64": dict(
        url=f"http://deb.debian.org/debian/pool/main/p/portaudio19/libportaudio2_{PORTAUDIO_VERSION}_arm64.deb",
        note="Debian trixie (stable) arm64",
    ),
    "linux-armv7": dict(
        url=f"http://deb.debian.org/debian/pool/main/p/portaudio19/libportaudio2_{PORTAUDIO_VERSION}_armhf.deb",
        note="Debian trixie (stable) armhf",
    ),
}


def _download(url: str, dest: Path) -> None:
    print(f"  fetching {url}")
    headers = {"User-Agent": "whalemodem-vendor-script"}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def _extract_deb_member(deb_path: Path, member_glob: str, work_dir: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ar", "x", str(deb_path)], cwd=work_dir, check=True)
    data_archive = next(work_dir.glob("data.tar.*"))
    subprocess.run(["tar", "xf", str(data_archive)], cwd=work_dir, check=True)
    matches = list(work_dir.glob(member_glob))
    if not matches:
        raise FileNotFoundError(f"no member matching {member_glob!r} in {deb_path.name}")
    # The glob catches the real file (libportaudio.so.2.0.0) plus its SONAME
    # symlink (libportaudio.so.2). Path.stat() follows symlinks, so both
    # matches report the same (real) size -- and shutil.copy2() below
    # follows symlinks too, so whichever match wins the max(), the bytes
    # copied to the destination are the same real ELF file.
    return max(matches, key=lambda p: p.stat().st_size)


def vendor_platform(tag: str, spec: dict, work_dir: Path) -> None:
    print(f"\n== {tag} ({spec['note']}) ==")
    out_dir = VENDOR_DIR / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    plat_work = work_dir / tag
    plat_work.mkdir(parents=True, exist_ok=True)

    archive = plat_work / "libportaudio2.deb"
    _download(spec["url"], archive)
    src = _extract_deb_member(archive, "usr/lib/*/libportaudio.so.*", plat_work / "extract")
    # Named libportaudio.so.2 (not .so.2.0.0) to match the real SONAME, so a
    # future ctypes.CDLL() load by this exact path works.
    dst = out_dir / "libportaudio.so.2"
    shutil.copy2(src, dst)
    print(f"  wrote {dst}")


def write_sources_md() -> None:
    lines = [
        "# Vendored PortAudio binaries",
        "",
        "Produced by `scripts/vendor_portaudio.py`. Do not hand-edit the binaries;",
        "rerun the script (after updating its SOURCES table) to refresh them.",
        "",
        "Linux only: the `sounddevice` PyPI wheel already bundles PortAudio on",
        "Windows and macOS, so there is nothing to vendor for those platforms.",
        "",
        f"Generated: {datetime.date.today().isoformat()}",
        "",
    ]
    for tag, spec in SOURCES.items():
        lines.append(f"## {tag}")
        lines.append(f"- {spec['note']}")
        lines.append(f"- portaudio19: {spec['url']}")
        lines.append("")
    (VENDOR_DIR / "SOURCES.md").write_text("\n".join(lines))


def main() -> None:
    only = sys.argv[1:] or list(SOURCES)
    work_dir = Path("/tmp/vendor_portaudio_work")
    work_dir.mkdir(exist_ok=True)
    for tag in only:
        vendor_platform(tag, SOURCES[tag], work_dir)
    write_sources_md()


if __name__ == "__main__":
    main()
