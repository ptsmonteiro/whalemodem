from pathlib import Path
from types import SimpleNamespace

import pytest

from whale.hw import audio_io, hamlib, ptt, ptt_backends, radios
from whale.hw._platform_tags import platform_tag


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
    tag = platform_tag()
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


def test_single_radio_file_defaults_implicitly(tmp_path: Path):
    """No default_radio and exactly one [radios.*] table: that radio is default."""
    path = tmp_path / "radios.toml"
    path.write_text('''
[radios.field]
audio_name = "Codec"
ptt.backend = "vox"
''')
    inventory = radios.load_radios(path)
    assert inventory.default == "field"
    assert radios.get_radio(None, path).name == "field"


def test_default_radio_resolves(tmp_path: Path):
    path = tmp_path / "radios.toml"
    path.write_text('''
default_radio = "b"

[radios.a]
audio_name = "Codec A"
ptt.backend = "vox"

[radios.b]
audio_name = "Codec B"
ptt.backend = "vox"
''')
    inventory = radios.load_radios(path)
    assert inventory.default == "b"
    assert radios.get_radio(None, path).name == "b"
    # explicit names are unaffected by the default
    assert radios.get_radio("a", path).name == "a"


def test_unknown_default_radio_raises(tmp_path: Path):
    path = tmp_path / "radios.toml"
    path.write_text('''
default_radio = "nope"

[radios.a]
audio_name = "Codec A"
ptt.backend = "vox"
''')
    with pytest.raises(ValueError):
        radios.load_radios(path)


def test_no_default_among_multiple_radios_raises_on_none(tmp_path: Path):
    path = tmp_path / "radios.toml"
    path.write_text('''
[radios.a]
audio_name = "Codec A"
ptt.backend = "vox"

[radios.b]
audio_name = "Codec B"
ptt.backend = "vox"
''')
    inventory = radios.load_radios(path)
    assert inventory.default is None
    with pytest.raises(ValueError):
        radios.get_radio(None, path)


def test_save_and_load_round_trips_multi_radio_inventory(tmp_path: Path):
    path = tmp_path / "radios.toml"
    original = radios.RadioInventory(
        {
            "shack-icom": radios.Radio(
                "shack-icom", "Icom controlled over CI-V", "IC-705", "icom-civ",
                {"usb_id": "0C26:0036", "radio_name": "IC-705", "address": 0xA4},
            ),
            "rigctl": radios.Radio(
                "rigctl", "rigctl", "USB Audio CODEC", "hamlib",
                {"model": 3073, "device": "/dev/ttyUSB0", "baud": 115200},
            ),
            "digirig": radios.Radio(
                "digirig", "digirig", "USB Audio Device", "serial-line",
                {"port": "/dev/ttyUSB0", "line": "rts", "active_high": False, "timeout": 0.5},
            ),
        },
        default="shack-icom",
    )
    radios.save_radios(path, original)
    loaded = radios.load_radios(path)
    assert loaded == original


def test_save_radios_quotes_non_bare_keys(tmp_path: Path):
    path = tmp_path / "radios.toml"
    original = radios.RadioInventory(
        {"my radio": radios.Radio("my radio", "has a space", "Codec", "vox", {})},
        default="my radio",
    )
    radios.save_radios(path, original)
    loaded = radios.load_radios(path)
    assert loaded == original


def _install_fake_devices(monkeypatch, devices):
    """Points audio_io at a fake WASAPI-like host API 0 plus an unrelated
    host API 1, and a fixed device list, so find_device(s)/list_devices can
    be tested without a real sound card."""
    monkeypatch.setenv("WHALE_AUDIO_HOST_API", "wasapi")
    monkeypatch.setattr(audio_io.sd, "query_hostapis",
                         lambda: [{"name": "Windows WASAPI"}, {"name": "MME"}])
    monkeypatch.setattr(audio_io.sd, "query_devices", lambda: devices)


# index: name, hostapi, max_input_channels, max_output_channels, default_samplerate
_FAKE_DEVICES = [
    {"name": "USB Audio CODEC", "hostapi": 0, "max_input_channels": 2,
     "max_output_channels": 2, "default_samplerate": 48000.0},
    {"name": "USB Audio CODEC 2", "hostapi": 0, "max_input_channels": 2,
     "max_output_channels": 2, "default_samplerate": 48000.0},
    {"name": "Line In", "hostapi": 0, "max_input_channels": 1,
     "max_output_channels": 0, "default_samplerate": 44100.0},
    {"name": "MME Device", "hostapi": 1, "max_input_channels": 2,
     "max_output_channels": 2, "default_samplerate": 44100.0},
    {"name": "Silent Device", "hostapi": 0, "max_input_channels": 0,
     "max_output_channels": 0, "default_samplerate": 48000.0},
]


def test_find_devices_returns_zero_one_or_many_matches(monkeypatch):
    _install_fake_devices(monkeypatch, _FAKE_DEVICES)
    assert audio_io.find_devices("nonexistent", "input") == []
    assert audio_io.find_devices("Line In", "input") == [2]
    assert audio_io.find_devices("codec", "input") == [0, 1]


def test_find_device_raises_on_zero_or_many_matches_but_not_one(monkeypatch):
    _install_fake_devices(monkeypatch, _FAKE_DEVICES)
    with pytest.raises(LookupError):
        audio_io.find_device("nonexistent", "input")
    with pytest.raises(LookupError):
        audio_io.find_device("codec", "input")
    assert audio_io.find_device("Line In", "input") == 2


def test_list_devices_filters_by_host_api_and_kind(monkeypatch):
    _install_fake_devices(monkeypatch, _FAKE_DEVICES)
    all_devices = audio_io.list_devices()
    # device 3 is on hostapi 1 (MME) and must not appear on the WASAPI list;
    # kind=None still surfaces device 4, which has no channels either way.
    assert [d.index for d in all_devices] == [0, 1, 2, 4]

    inputs = audio_io.list_devices("input")
    assert [d.index for d in inputs] == [0, 1, 2]

    outputs = audio_io.list_devices("output")
    assert [d.index for d in outputs] == [0, 1]

    first = all_devices[0]
    assert first == audio_io.AudioDevice(
        index=0, name="USB Audio CODEC", host_api="Windows WASAPI",
        max_input_channels=2, max_output_channels=2, default_samplerate=48000.0,
    )


def test_list_serial_ports_maps_vid_pid_and_sorts_by_device(monkeypatch):
    fake_ports = [
        SimpleNamespace(device="/dev/ttyUSB1", description="CP210x UART Bridge",
                         manufacturer="Silicon Labs", vid=0x10C4, pid=0xEA60),
        SimpleNamespace(device="/dev/ttyUSB0", description="IC-705 CI-V",
                         manufacturer="Icom", vid=0x0C26, pid=0x0036),
        SimpleNamespace(device="/dev/ttyS0", description="n/a",
                         manufacturer=None, vid=None, pid=None),
        SimpleNamespace(device="/dev/ttyUSB2", description="partial USB info",
                         manufacturer=None, vid=0x1234, pid=None),
    ]
    monkeypatch.setattr(ptt.list_ports, "comports", lambda: fake_ports)

    result = ptt.list_serial_ports()

    assert [p.device for p in result] == [
        "/dev/ttyS0", "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2",
    ]
    by_device = {p.device: p for p in result}
    assert by_device["/dev/ttyUSB0"].usb_id == "0C26:0036"
    assert by_device["/dev/ttyUSB1"].usb_id == "10C4:EA60"
    assert by_device["/dev/ttyS0"].usb_id is None
    assert by_device["/dev/ttyUSB2"].usb_id is None  # pid missing
    assert by_device["/dev/ttyUSB0"].description == "IC-705 CI-V"
    assert by_device["/dev/ttyUSB0"].manufacturer == "Icom"


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
