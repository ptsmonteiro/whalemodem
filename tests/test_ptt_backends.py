from pathlib import Path

import pytest

from whale.hw import hamlib, ptt_backends, radios


def test_builtin_backend_inventory():
    names = ptt_backends.available_backends()
    assert {"icom-civ", "serial-line", "hamlib", "vox"} <= names.keys()
    assert names["icom-civ"].capabilities.acknowledgement
    assert not names["vox"].capabilities.requires_explicit_keying


def test_hamlib_backend_keys_the_dummy_rig():
    """Exercises the real ctypes binding end to end, no radio hardware needed.

    Model 1 is Hamlib's built-in "Dummy" rig: rig_open()/rig_set_ptt() all
    succeed without touching a serial port, so this runs on any platform that
    has a libhamlib available -- which, per test_hamlib_backend_prefers_the_
    bundled_library below, is all six platforms whale/hw/_vendor/hamlib/
    bundles for, with no system install required.
    """
    try:
        controller = ptt_backends.open_backend("hamlib", {"model": 1})
    except OSError:
        pytest.skip("libhamlib is not installed and this platform isn't one whalemodem bundles for")
    assert controller.key(True)
    assert not controller.key_state_unknown
    assert controller.key(False)
    assert not controller.key_state_unknown
    controller.close()


def test_hamlib_backend_prefers_the_bundled_library():
    """On any of the six bundled platforms, this must not fall back to a
    system install -- that's the entire point of vendoring the binaries.
    """
    tag = hamlib._platform_tag()
    if tag is None:
        pytest.skip("this platform is not one whalemodem bundles hamlib for")
    ptt_backends.open_backend("hamlib", {"model": 1}).close()
    assert hamlib.LOADED_FROM is not None
    assert f"_vendor/hamlib/{tag}/" in hamlib.LOADED_FROM.replace("\\", "/")


def test_hamlib_backend_rejects_unknown_model():
    try:
        with pytest.raises(ValueError):
            ptt_backends.open_backend("hamlib", {"model": 999_999})
    except OSError:
        pytest.skip("libhamlib is not installed and this platform isn't one whalemodem bundles for")


def test_toml_radio_inventory(tmp_path: Path):
    path = tmp_path / "radios.toml"
    path.write_text('''
[radios.field]
audio_name = "Codec"
ptt.backend = "vox"
''')
    radio = radios.get_radio("field", path)
    assert radio.ptt_backend == "vox"
    assert radio.ptt().key(True)


def test_application_backend_registration():
    class Controller:
        key_state_unknown = False
        def key(self, on): return True
        def close(self): pass
    class Backend:
        name = "test-plugin"
        capabilities = ptt_backends.PttCapabilities(can_report_state=True)
        def open(self, config): return Controller()
    ptt_backends.register_backend(Backend(), replace=True)
    assert ptt_backends.open_backend("test-plugin", {}).key(True)
