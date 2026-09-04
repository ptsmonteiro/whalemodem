"""ctypes binding to libhamlib, for in-process PTT control.

Talks to the C library directly (rig_init/rig_open/rig_set_ptt/rig_close/
rig_cleanup) instead of shelling out to rigctl per call. Shelling out means a
process spawn plus a fresh rig_open() -- and for most CAT backends rig_open()
itself does a serial handshake -- on every single PTT toggle, which is real
dead air on the front of every transmission. A persistent RIG* handle pays
that cost once, at Rig() construction, and every subsequent key() is a single
function call.

Only the small, version-stable slice of the API is used. Configuration goes
through rig_token_lookup()/rig_set_conf() -- the same generic token mechanism
rigctl's `-r`, `-s`, `-c`, and `-C` flags use internally (see `rigctl -L` for
the token list of a given model) -- rather than poking hamlib_port_t/
rig_state fields directly, whose struct layout is not part of hamlib's ABI
guarantee across versions or distros.

Residual risk carried over from hamlib itself, not fixed by this wrapper:
the `timeout`/`retry` conf tokens bound how long hamlib will wait for a
*reply*, but hamlib's serial write path does not expose a write-side
timeout the way whale.hw.ptt's WRITE_TIMEOUT does for pyserial. A serial
bridge wedged mid-write (the same class of failure documented at length in
whale/hw/ptt.py) can still block a set_ptt(False) call indefinitely. This is
a hamlib limitation, not one this binding can route around from the outside.

Library discovery prefers the prebuilt binaries vendored under
whale/hw/_vendor/hamlib/<platform-tag>/ (see scripts/vendor_hamlib.py) over
whatever the host has installed, so a plain `pip install` works without a
separate hamlib install on the six platforms that ships for. Set
WHALEMODEM_SYSTEM_HAMLIB=1 to skip the bundled copy and search the system
instead -- e.g. to pick up a rig added to hamlib after our vendored version,
or on a platform we don't bundle for.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
from pathlib import Path
from typing import Mapping

from whale.hw._platform_tags import platform_tag

_VENDOR_ROOT = Path(__file__).resolve().parent / "_vendor" / "hamlib"


def _load_bundled():
    """Loads the vendored library for this platform, or returns (None, None).

    Returning None (rather than raising) on any failure here is deliberate:
    a bundling gap or a corrupt vendored file should fall back to the system
    search in _load_library(), not take down every platform because one of
    the six vendored copies has a problem.
    """
    if os.environ.get("WHALEMODEM_SYSTEM_HAMLIB"):
        return None, None
    tag = platform_tag()
    if tag is None:
        return None, None
    vendor_dir = _VENDOR_ROOT / tag
    system = platform.system()
    try:
        if system == "Windows":
            hamlib_path = vendor_dir / "libhamlib.dll"
            if not hamlib_path.exists():
                return None, None
            # libhamlib.dll imports libusb-1.0.dll/libwinpthread-1.dll by
            # bare name; adding the vendor dir to the DLL search path lets
            # Windows resolve those against our bundled copies instead of
            # requiring them on PATH.
            os.add_dll_directory(str(vendor_dir))
            return ctypes.CDLL(str(hamlib_path)), str(hamlib_path)
        if system == "Linux":
            hamlib_path = vendor_dir / "libhamlib.so"
            libusb_path = vendor_dir / "libusb-1.0.so"
            if not hamlib_path.exists() or not libusb_path.exists():
                return None, None
            # libhamlib.so's NEEDED entry for libusb-1.0.so.0 is a bare
            # soname (no RPATH baked in -- see scripts/vendor_hamlib.py).
            # Loading our copy first, RTLD_GLOBAL, makes the dynamic linker
            # resolve that dependency against the already-loaded module by
            # soname rather than searching the filesystem.
            ctypes.CDLL(str(libusb_path), mode=ctypes.RTLD_GLOBAL)
            return ctypes.CDLL(str(hamlib_path)), str(hamlib_path)
        if system == "Darwin":
            hamlib_path = vendor_dir / "libhamlib.dylib"
            if not hamlib_path.exists():
                return None, None
            # The dylib's own dependency on libusb was rewritten to
            # @loader_path at vendor time, so it resolves next to itself
            # with no extra step here.
            return ctypes.CDLL(str(hamlib_path)), str(hamlib_path)
    except OSError:
        return None, None
    return None, None

RIG_OK = 0
RIG_PTT_OFF = 0
RIG_PTT_ON = 1
RIG_VFO_CURR = 1 << 29  # RIG_VFO_N(29); see hamlib/rig.h
RIG_DEBUG_WARN = 3

_ERROR_NAMES = {
    0: "RIG_OK", 1: "RIG_EINVAL", 2: "RIG_ECONF", 3: "RIG_ENOMEM",
    4: "RIG_ENIMPL", 5: "RIG_ETIMEOUT", 6: "RIG_EIO", 7: "RIG_EINTERNAL",
    8: "RIG_EPROTO", 9: "RIG_ERJCTED", 10: "RIG_ETRUNC", 11: "RIG_ENAVAIL",
    12: "RIG_ENTARGET", 13: "RIG_BUSERROR", 14: "RIG_BUSBUSY", 15: "RIG_EARG",
    16: "RIG_EVFO", 17: "RIG_EDOM", 18: "RIG_EDEPRECATED", 19: "RIG_ESECURITY",
    20: "RIG_EPOWER", 21: "RIG_EMPTY",
}


class HamlibError(RuntimeError):
    def __init__(self, call: str, code: int):
        super().__init__(f"hamlib {call} failed: {code} ({_ERROR_NAMES.get(code, 'unknown')})")
        self.call, self.code = call, code


LOADED_FROM: str | None = None


def _load_library():
    global LOADED_FROM
    bundled, bundled_path = _load_bundled()
    if bundled is not None:
        LOADED_FROM = bundled_path
        return bundled

    tried = []
    for name in ("hamlib", "hamlib.4"):
        path = ctypes.util.find_library(name)
        if path:
            try:
                LOADED_FROM = path
                return ctypes.CDLL(path)
            except OSError as exc:
                tried.append(f"{path}: {exc}")
    for candidate in ("libhamlib.dylib", "libhamlib.so.4", "libhamlib.so", "libhamlib-4.dll"):
        try:
            LOADED_FROM = candidate
            return ctypes.CDLL(candidate)
        except OSError as exc:
            tried.append(f"{candidate}: {exc}")
    LOADED_FROM = None
    raise OSError(
        "could not load libhamlib (tried the bundled copy for this platform, then: "
        + "; ".join(tried) + "). Install Hamlib, e.g. 'brew install hamlib' or "
        "'apt install libhamlib4', or check whale/hw/_vendor/hamlib/ for this platform."
    )


_lib = None


def _hamlib():
    global _lib
    if _lib is None:
        lib = _load_library()
        lib.rig_init.restype = ctypes.c_void_p
        lib.rig_init.argtypes = [ctypes.c_uint32]
        lib.rig_open.restype = ctypes.c_int
        lib.rig_open.argtypes = [ctypes.c_void_p]
        lib.rig_close.restype = ctypes.c_int
        lib.rig_close.argtypes = [ctypes.c_void_p]
        lib.rig_cleanup.restype = ctypes.c_int
        lib.rig_cleanup.argtypes = [ctypes.c_void_p]
        lib.rig_set_ptt.restype = ctypes.c_int
        lib.rig_set_ptt.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
        lib.rig_token_lookup.restype = ctypes.c_long
        lib.rig_token_lookup.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.rig_set_conf.restype = ctypes.c_int
        lib.rig_set_conf.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_char_p]
        lib.rig_set_debug.restype = None
        lib.rig_set_debug.argtypes = [ctypes.c_int]
        # Left uncalled, hamlib's own default is RIG_DEBUG_TRACE, which
        # writes a torrent of per-call trace lines to stderr on every PTT
        # toggle. rigctl itself sets WARN unless run with -v; match that.
        lib.rig_set_debug(RIG_DEBUG_WARN)
        _lib = lib
    return _lib


class Rig:
    """One open hamlib RIG* handle. Not thread-safe; callers must serialize."""

    def __init__(self, model: int, conf: Mapping[str, str]):
        self._lib = _hamlib()
        self._handle = self._lib.rig_init(ctypes.c_uint32(model))
        if not self._handle:
            raise ValueError(f"hamlib: unknown rig model {model}")
        try:
            for token, value in conf.items():
                self._set_conf(token, value)
            code = self._lib.rig_open(self._handle)
            if code != RIG_OK:
                raise HamlibError("rig_open", code)
        except Exception:
            self._lib.rig_cleanup(self._handle)
            self._handle = None
            raise

    def _set_conf(self, name: str, value: str) -> None:
        token = self._lib.rig_token_lookup(self._handle, name.encode())
        if token == 0:
            raise ValueError(f"hamlib: unknown config token {name!r}")
        code = self._lib.rig_set_conf(self._handle, token, str(value).encode())
        if code != RIG_OK:
            raise HamlibError(f"rig_set_conf({name})", code)

    def set_ptt(self, on: bool) -> None:
        code = self._lib.rig_set_ptt(self._handle, RIG_VFO_CURR, RIG_PTT_ON if on else RIG_PTT_OFF)
        if code != RIG_OK:
            raise HamlibError("rig_set_ptt", code)

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            self._lib.rig_close(self._handle)
        finally:
            self._lib.rig_cleanup(self._handle)
            self._handle = None
