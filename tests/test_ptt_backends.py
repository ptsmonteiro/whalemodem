from pathlib import Path

from whale.hw import ptt_backends, radios


def test_builtin_backend_inventory():
    names = ptt_backends.available_backends()
    assert {"icom-civ", "serial-line", "hamlib", "vox"} <= names.keys()
    assert names["icom-civ"].capabilities.acknowledgement
    assert not names["vox"].capabilities.requires_explicit_keying


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
