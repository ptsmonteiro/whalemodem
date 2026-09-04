# Standalone build (PyInstaller)

Builds `whalemodem-server` into a standalone, no-Python-required onedir
bundle, so an end user does not need Python, numpy/scipy, or a system
hamlib/PortAudio install to run a station.

PyInstaller is a **build-only** dependency, not part of the package's
runtime `dependencies` in `pyproject.toml`. Install it manually before
building:

```
pip install pyinstaller
```

Then, from the repo root:

```
pyinstaller packaging/pyinstaller/whalemodem.spec
```

The bundle lands in `dist/whalemodem-server/`; run it with
`dist/whalemodem-server/whalemodem-server --help`.

The build must run **natively** on each target OS/arch -- no
cross-compilation -- because the spec bundles that build host's own
vendored hamlib (and, on Linux, PortAudio) binaries from
`whale/hw/_vendor/`. A build done on macOS arm64 only produces a macOS
arm64 bundle, etc.

For the full six-platform build matrix, see the
`.github/workflows/standalone-builds.yml` CI workflow
(`workflow_dispatch`, or push a `v*` tag). Only its linux-x86_64 leg has
actually been run end-to-end so far (in Docker, during development); the
other five legs are written but not yet exercised on real GitHub Actions or
real hardware -- see docs/HARDWARE.md's "Standalone builds" section for the
full validation status.
