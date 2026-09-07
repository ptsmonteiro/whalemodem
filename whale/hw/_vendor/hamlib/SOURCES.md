# Vendored hamlib/libusb binaries

Produced by `scripts/vendor_hamlib.py`. Do not hand-edit the binaries; rerun
the script (after updating its SOURCES table) to refresh them.

Generated: 2026-09-04

## macos-arm64
- Homebrew bottle tag arm64_sonoma (oldest currently published, for broadest compatibility)
- hamlib: https://ghcr.io/v2/homebrew/core/hamlib/blobs/sha256:45a87a2b474931b39d2e8407ef931b7753681be7d992536acbf9c71b9e54bc29
- libusb: https://ghcr.io/v2/homebrew/core/libusb/blobs/sha256:74fa9ed0291e2d3e7827a06ea836a57c96d8861a7079544d47be231f08eb4c02

## macos-x86_64
- Homebrew bottle tag sonoma -- the only Intel-mac tag currently published; Intel users on macOS < 14 cannot use this bundled copy
- hamlib: https://ghcr.io/v2/homebrew/core/hamlib/blobs/sha256:fba407c9ce0e3a36dfe739e5df61ee05c4d437d350a851a6b9b1d78fa1ff6f8a
- libusb: https://ghcr.io/v2/homebrew/core/libusb/blobs/sha256:1387aea9bbed3a1e57884b5b43166fc83cfdae415e5f3803a8259ff77a4ba613

## linux-x86_64
- Debian trixie (stable) amd64
- hamlib: http://deb.debian.org/debian/pool/main/h/hamlib/libhamlib4t64_4.6.2-1+b1_amd64.deb
- libusb: http://deb.debian.org/debian/pool/main/libu/libusb-1.0/libusb-1.0-0_1.0.28-1_amd64.deb

## linux-aarch64
- Debian trixie (stable) arm64
- hamlib: http://deb.debian.org/debian/pool/main/h/hamlib/libhamlib4t64_4.6.2-1+b1_arm64.deb
- libusb: http://deb.debian.org/debian/pool/main/libu/libusb-1.0/libusb-1.0-0_1.0.28-1_arm64.deb

## linux-armv7
- Debian trixie (stable) armhf
- hamlib: http://deb.debian.org/debian/pool/main/h/hamlib/libhamlib4t64_4.6.2-1+b1_armhf.deb
- libusb: http://deb.debian.org/debian/pool/main/libu/libusb-1.0/libusb-1.0-0_1.0.28-1_armhf.deb

## windows-x86_64
- Hamlib's own official w64 release archive (MinGW build); bundles its own libusb-1.0.dll, libgcc_s_seh-1.dll, libwinpthread-1.dll
- hamlib: https://github.com/Hamlib/Hamlib/releases/download/4.7.2/hamlib-w64-4.7.2.zip
