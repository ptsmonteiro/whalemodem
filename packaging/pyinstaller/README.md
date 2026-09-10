# Standalone build (PyInstaller)

Builds `whale-server` and `whale-configure` into a standalone,
no-Python-required onedir bundle, so an end user does not need Python,
numpy/scipy, or a system hamlib/PortAudio install to run a station or
configure its radio inventory.

PyInstaller is a **build-only** dependency, not part of the package's
runtime `dependencies` in `pyproject.toml`. Install it manually before
building:

```
pip install pyinstaller
```

Then, from the repo root:

```
pyinstaller packaging/pyinstaller/whale.spec
```

The bundle lands in `dist/whale/`, holding both `whale-server` and
`whale-configure` next to one shared `_internal/` (their common Python
runtime and vendored hamlib binaries, deduped via PyInstaller's `MERGE()`
rather than copied into two separate folders). Run either with
`dist/whale/whale-server --help` or `dist/whale/whale-configure --help`.

The build must run **natively** on each target OS/arch -- no
cross-compilation -- because the spec bundles that build host's own
vendored hamlib (and, on Linux, PortAudio) binaries from
`whale/hw/_vendor/`. A build done on macOS arm64 only produces a macOS
arm64 bundle, etc.

For the full six-platform build matrix, see the
`.github/workflows/standalone-builds.yml` CI workflow
(`workflow_dispatch`, or push a `v*` tag). The linux-x86_64 leg (in Docker,
during development) and windows-x86_64 leg (native build + smoke test on a
Windows host) have been run end-to-end so far; the other four legs are
written but not yet exercised on real GitHub Actions or real hardware --
see docs/HARDWARE.md's "Standalone builds" section for the full validation
status.
