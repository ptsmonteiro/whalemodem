#!/usr/bin/env python3
"""Fetches and relocates prebuilt libhamlib (+ libusb) binaries for every
platform whale/hw/hamlib.py bundles, writing them into
whale/hw/_vendor/hamlib/<platform-tag>/.

Rerun this whenever the pinned hamlib/libusb versions below are bumped. See
whale/hw/_vendor/hamlib/SOURCES.md (written by this script) for exactly what
version and URL produced the files currently checked in.

Each source was chosen and verified by hand, not just picked for convenience
-- see the comments below and PLAN context. In particular:

  - Homebrew's *Linux* bottles are NOT used for hamlib/libusb even though
    Homebrew publishes them: they carry a baked-in RPATH into a full
    Homebrew-on-Linux install (~20 other Homebrew-managed libraries), so
    they are not self-contained. Debian's official .deb packages are used
    for all three Linux architectures instead.
  - Debian's *sid* hamlib build (4.7.2) was rejected in favor of trixie's
    stable 4.6.2: sid's build additionally links libindiclient/libnova/
    libstdc++ (pulled in by INDI rotator support), which trixie's does not.

macOS relocation requires running this script's macOS legs *on* macOS
(install_name_tool has no portable equivalent); the Linux and Windows legs
only need curl/tar/ar/zip and can run anywhere.
"""
from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent.parent / "whale" / "hw" / "_vendor" / "hamlib"

HAMLIB_VERSION_MAC_WIN = "4.7.2"
HAMLIB_VERSION_LINUX = "4.6.2-1+b1"
LIBUSB_VERSION_MAC = "1.0.30"
LIBUSB_VERSION_LINUX = "2:1.0.28-1"

# platform tag -> (hamlib source url, hamlib kind, libusb source url, libusb kind)
# kind is one of: "brew-bottle" (gzipped tar from a Homebrew bottle blob),
# "deb" (Debian .deb archive), "win-zip" (hamlib's own release zip, which
# bundles its own libusb + MinGW runtime DLLs, so libusb_url is None there).
SOURCES = {
    "macos-arm64": dict(
        hamlib_url="https://ghcr.io/v2/homebrew/core/hamlib/blobs/sha256:45a87a2b474931b39d2e8407ef931b7753681be7d992536acbf9c71b9e54bc29",
        hamlib_kind="brew-bottle",
        libusb_url="https://ghcr.io/v2/homebrew/core/libusb/blobs/sha256:74fa9ed0291e2d3e7827a06ea836a57c96d8861a7079544d47be231f08eb4c02",
        libusb_kind="brew-bottle",
        note="Homebrew bottle tag arm64_sonoma (oldest currently published, for broadest compatibility)",
    ),
    "macos-x86_64": dict(
        hamlib_url="https://ghcr.io/v2/homebrew/core/hamlib/blobs/sha256:fba407c9ce0e3a36dfe739e5df61ee05c4d437d350a851a6b9b1d78fa1ff6f8a",
        hamlib_kind="brew-bottle",
        libusb_url="https://ghcr.io/v2/homebrew/core/libusb/blobs/sha256:1387aea9bbed3a1e57884b5b43166fc83cfdae415e5f3803a8259ff77a4ba613",
        libusb_kind="brew-bottle",
        note="Homebrew bottle tag sonoma -- the only Intel-mac tag currently published; "
             "Intel users on macOS < 14 cannot use this bundled copy",
    ),
    "linux-x86_64": dict(
        hamlib_url="http://deb.debian.org/debian/pool/main/h/hamlib/libhamlib4t64_4.6.2-1+b1_amd64.deb",
        hamlib_kind="deb",
        libusb_url="http://deb.debian.org/debian/pool/main/libu/libusb-1.0/libusb-1.0-0_1.0.28-1_amd64.deb",
        libusb_kind="deb",
        note="Debian trixie (stable) amd64",
    ),
    "linux-aarch64": dict(
        hamlib_url="http://deb.debian.org/debian/pool/main/h/hamlib/libhamlib4t64_4.6.2-1+b1_arm64.deb",
        hamlib_kind="deb",
        libusb_url="http://deb.debian.org/debian/pool/main/libu/libusb-1.0/libusb-1.0-0_1.0.28-1_arm64.deb",
        libusb_kind="deb",
        note="Debian trixie (stable) arm64",
    ),
    "linux-armv7": dict(
        hamlib_url="http://deb.debian.org/debian/pool/main/h/hamlib/libhamlib4t64_4.6.2-1+b1_armhf.deb",
        hamlib_kind="deb",
        libusb_url="http://deb.debian.org/debian/pool/main/libu/libusb-1.0/libusb-1.0-0_1.0.28-1_armhf.deb",
        libusb_kind="deb",
        note="Debian trixie (stable) armhf",
    ),
    "windows-x86_64": dict(
        hamlib_url="https://github.com/Hamlib/Hamlib/releases/download/4.7.2/hamlib-w64-4.7.2.zip",
        hamlib_kind="win-zip",
        libusb_url=None,
        libusb_kind=None,
        note="Hamlib's own official w64 release archive (MinGW build); bundles its own "
             "libusb-1.0.dll, libgcc_s_seh-1.dll, libwinpthread-1.dll",
    ),
}


def _ghcr_token(repository: str) -> str:
    url = f"https://ghcr.io/token?scope=repository:{repository}:pull"
    with urllib.request.urlopen(url, timeout=30) as response:
        import json
        return json.load(response)["token"]


def _download(url: str, dest: Path) -> None:
    print(f"  fetching {url}")
    headers = {"User-Agent": "whalemodem-vendor-script"}
    if url.startswith("https://ghcr.io/v2/"):
        # homebrew/core/<formula>/blobs/... -> repository is everything
        # between /v2/ and /blobs/.
        repository = url.split("/v2/", 1)[1].split("/blobs/", 1)[0]
        headers["Authorization"] = f"Bearer {_ghcr_token(repository)}"
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
    # The glob catches the real file plus its SONAME symlink(s); the real
    # file (resolved through the symlink, if that's what matched) is largest.
    return max(matches, key=lambda p: p.stat().st_size)


def _extract_brew_bottle_member(tar_gz_path: Path, member_glob: str, work_dir: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "xzf", str(tar_gz_path)], cwd=work_dir, check=True)
    matches = sorted(p for p in work_dir.glob(member_glob) if not p.is_symlink())
    if not matches:
        raise FileNotFoundError(f"no member matching {member_glob!r} in {tar_gz_path.name}")
    # Bottles ship a real file plus SONAME/dev symlinks; the real file is the largest.
    return max(matches, key=lambda p: p.stat().st_size)


def _macos_relocate(dylib: Path, libusb_name: str) -> None:
    subprocess.run(["install_name_tool", "-id", f"@loader_path/{dylib.name}", str(dylib)], check=True)
    dep_lines = subprocess.run(["otool", "-L", str(dylib)], check=True, capture_output=True, text=True).stdout
    for line in dep_lines.splitlines():
        line = line.strip()
        if "libusb" in line and line.endswith(")"):
            old_path = line.split(" (")[0]
            subprocess.run(
                ["install_name_tool", "-change", old_path, f"@loader_path/{libusb_name}", str(dylib)],
                check=True,
            )
    # install_name_tool invalidates the original (Homebrew) code signature;
    # an unsigned/invalid-signature dylib gets silently SIGKILL'd on load
    # rather than raising a Python-visible error, so this step is not
    # optional. Ad-hoc (unsigned-identity) signing is enough to satisfy
    # local validation.
    subprocess.run(["codesign", "--sign", "-", "--force", str(dylib)], check=True)


def vendor_platform(tag: str, spec: dict, work_dir: Path) -> None:
    print(f"\n== {tag} ({spec['note']}) ==")
    out_dir = VENDOR_DIR / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    plat_work = work_dir / tag
    plat_work.mkdir(parents=True, exist_ok=True)

    if spec["hamlib_kind"] == "win-zip":
        archive = plat_work / "hamlib.zip"
        _download(spec["hamlib_url"], archive)
        with zipfile.ZipFile(archive) as zf:
            names = {n.split("/")[-1]: n for n in zf.namelist()}
            for wanted, out_name in (
                ("libhamlib-4.dll", "libhamlib.dll"),
                ("libusb-1.0.dll", "libusb-1.0.dll"),
                ("libgcc_s_seh-1.dll", "libgcc_s_seh-1.dll"),
                ("libwinpthread-1.dll", "libwinpthread-1.dll"),
            ):
                with zf.open(names[wanted]) as src, (out_dir / out_name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            license_path = VENDOR_DIR / "LICENSE-hamlib.txt"
            if not license_path.exists():
                with zf.open(names["COPYING.LIB.txt"]) as src, license_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        print(f"  wrote {out_dir}")
        return

    is_mac = spec["hamlib_kind"] == "brew-bottle"
    hamlib_glob = "hamlib/*/lib/libhamlib.*.dylib" if is_mac else "usr/lib/*/libhamlib.so.*"
    libusb_glob = "libusb/*/lib/libusb-1.0.*.dylib" if is_mac else "usr/lib/*/libusb-1.0.so.*"
    extract = _extract_brew_bottle_member if is_mac else _extract_deb_member
    hamlib_ext = "dylib" if is_mac else "so"

    hamlib_archive = plat_work / ("hamlib." + ("tar.gz" if is_mac else "deb"))
    _download(spec["hamlib_url"], hamlib_archive)
    hamlib_src = extract(hamlib_archive, hamlib_glob, plat_work / "hamlib_extract")
    hamlib_dst = out_dir / f"libhamlib.{hamlib_ext}"
    shutil.copy2(hamlib_src, hamlib_dst)

    libusb_archive = plat_work / ("libusb." + ("tar.gz" if is_mac else "deb"))
    _download(spec["libusb_url"], libusb_archive)
    libusb_src = extract(libusb_archive, libusb_glob, plat_work / "libusb_extract")
    libusb_dst = out_dir / f"libusb-1.0.{hamlib_ext}"
    shutil.copy2(libusb_src, libusb_dst)

    if is_mac:
        if sys.platform != "darwin":
            raise RuntimeError(f"{tag} requires install_name_tool; rerun this script on macOS")
        _macos_relocate(hamlib_dst, libusb_dst.name)
        subprocess.run(["install_name_tool", "-id", f"@loader_path/{libusb_dst.name}", str(libusb_dst)], check=True)
        subprocess.run(["codesign", "--sign", "-", "--force", str(libusb_dst)], check=True)

    print(f"  wrote {out_dir}")


def write_license_files() -> None:
    # hamlib's own COPYING.LIB.txt is pulled from the windows-x86_64 zip
    # above; libusb ships no equivalent single-file license in its Debian
    # .deb (only a machine-readable debian/copyright), so its LGPL-2.1 text
    # is copied by hand from https://github.com/libusb/libusb/blob/master/COPYING
    # (identical text to hamlib's, both LGPL-2.1) if not already present.
    libusb_license = VENDOR_DIR / "LICENSE-libusb.txt"
    if not libusb_license.exists():
        print(f"  NOTE: {libusb_license} missing -- copy libusb's COPYING file in by hand")


def write_sources_md() -> None:
    lines = [
        "# Vendored hamlib/libusb binaries",
        "",
        "Produced by `scripts/vendor_hamlib.py`. Do not hand-edit the binaries; rerun",
        "the script (after updating its SOURCES table) to refresh them.",
        "",
        f"Generated: {datetime.date.today().isoformat()}",
        "",
    ]
    for tag, spec in SOURCES.items():
        lines.append(f"## {tag}")
        lines.append(f"- {spec['note']}")
        lines.append(f"- hamlib: {spec['hamlib_url']}")
        if spec["libusb_url"]:
            lines.append(f"- libusb: {spec['libusb_url']}")
        lines.append("")
    (VENDOR_DIR / "SOURCES.md").write_text("\n".join(lines))


def main() -> None:
    only = sys.argv[1:] or list(SOURCES)
    work_dir = Path("/tmp/vendor_hamlib_work")
    work_dir.mkdir(exist_ok=True)
    for tag in only:
        vendor_platform(tag, SOURCES[tag], work_dir)
    write_license_files()
    write_sources_md()


if __name__ == "__main__":
    main()
