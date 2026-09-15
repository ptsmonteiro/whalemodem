from whale.hw.radios import Radio
from whale.radio_config_form import RadioForm


def _valid_form() -> RadioForm:
    form = RadioForm(existing=None, other_names=[])
    form.name = "portable"
    form.description = "Portable station"
    form.audio_input_name = "USB input"
    form.audio_output_name = "USB output"
    return form


def test_blank_form_has_backend_defaults_and_dynamic_rows():
    form = RadioForm(existing=None, other_names=[])

    assert form.ptt_backend == "vox"
    assert form.backend_config["serial-line"] == {
        "port": "",
        "line": "rts",
        "baud": "",
        "active_high": True,
    }
    assert [row.key for row in form.rows()][-2:] == ["save", "cancel"]

    form.ptt_backend = "icom-civ"
    assert "usb_id" in [row.key for row in form.rows()]


def test_existing_radio_hydrates_editable_backend_values():
    radio = Radio(
        "base", "Base station", "Mic", "Speaker", "serial-line",
        frozenset({"fm", "hf"}),
        {"port": "COM7", "line": "dtr", "baud": 19200, "active_high": False},
        tx_level_db=-3.5,
    )

    form = RadioForm(existing=("inventory-key", radio), other_names=["other"])

    assert form.old_name == "inventory-key"
    assert form.backend_config["serial-line"] == {
        "port": "COM7",
        "line": "dtr",
        "baud": "19200",
        "active_high": False,
    }
    assert form.tx_level_db == -3.5


def test_build_radio_validates_without_tui_state():
    form = RadioForm(existing=None, other_names=["duplicate"])
    form.name = "duplicate"
    form.channel_fm = False

    radio, errors = form.build_radio()

    assert radio is None
    assert "a radio named 'duplicate' already exists" in errors
    assert "description is required" in errors
    assert "at least one channel (fm or hf) is required" in errors


def test_build_radio_serializes_backend_configuration():
    form = _valid_form()
    form.ptt_backend = "hamlib"
    form.backend_config["hamlib"].update({
        "model": "3081",
        "device": "COM4",
        "baud": "19200",
        "civaddr": "0x94",
        "timeout": "1.5",
        "retry": "3",
    })

    radio, errors = form.build_radio()

    assert errors == []
    assert radio is not None
    assert radio.ptt_config == {
        "model": 3081,
        "device": "COM4",
        "baud": 19200,
        "civaddr": "0x94",
        "timeout": 1.5,
        "retry": 3,
    }


def test_build_radio_vox_backend_has_empty_ptt_config():
    form = _valid_form()

    radio, errors = form.build_radio()

    assert errors == []
    assert radio is not None
    assert radio.ptt_backend == "vox"
    assert radio.ptt_config == {}


def test_build_radio_serial_line_requires_port():
    form = _valid_form()
    form.ptt_backend = "serial-line"

    radio, errors = form.build_radio()

    assert radio is None
    assert "port is required" in errors


def test_build_radio_serial_line_defaults_blank_baud_to_9600():
    form = _valid_form()
    form.ptt_backend = "serial-line"
    form.backend_config["serial-line"]["port"] = "COM3"

    radio, errors = form.build_radio()

    assert errors == []
    assert radio is not None
    assert radio.ptt_config["baud"] == 9600


def test_build_radio_icom_civ_rejects_non_integer_address():
    form = _valid_form()
    form.ptt_backend = "icom-civ"
    form.backend_config["icom-civ"]["usb_id"] = "10c4:ea60"
    form.backend_config["icom-civ"]["address"] = "not-a-number"

    radio, errors = form.build_radio()

    assert radio is None
    assert "address must be an integer, e.g. 164 or 0xA4" in errors


def test_build_radio_icom_civ_accepts_hex_address_and_omits_blank_one():
    form = _valid_form()
    form.ptt_backend = "icom-civ"
    form.backend_config["icom-civ"]["usb_id"] = "10c4:ea60"
    form.backend_config["icom-civ"]["address"] = "0xA4"

    radio, errors = form.build_radio()

    assert errors == []
    assert radio is not None
    assert radio.ptt_config == {"usb_id": "10c4:ea60", "address": 164}


def test_build_radio_hamlib_rejects_non_numeric_model():
    form = _valid_form()
    form.ptt_backend = "hamlib"
    form.backend_config["hamlib"]["model"] = "not-a-number"

    radio, errors = form.build_radio()

    assert radio is None
    assert "model must be a whole number" in errors


def test_cycle_backend_wraps_through_all_backends():
    form = RadioForm(existing=None, other_names=[])
    assert form.ptt_backend == "vox"

    form.cycle_backend()
    assert form.ptt_backend == "serial-line"
    form.cycle_backend()
    form.cycle_backend()
    assert form.ptt_backend == "hamlib"
    form.cycle_backend()
    assert form.ptt_backend == "vox"


def test_toggle_bool_and_cycle_selector():
    form = RadioForm(existing=None, other_names=[])
    form.ptt_backend = "serial-line"

    assert form.get_value("active_high") is True
    form.toggle_bool("active_high")
    assert form.get_value("active_high") is False

    assert form.get_value("line") == "rts"
    form.cycle_selector("line")
    assert form.get_value("line") == "dtr"
    form.cycle_selector("line")
    assert form.get_value("line") == "rts"
