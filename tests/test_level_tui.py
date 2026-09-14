import threading
from types import SimpleNamespace

import numpy as np
import pytest

from whale import transport
from whale.hw import audio_io
from whale.hw.radios import Radio, RadioInventory, load_radios, save_radios
from whale.level_tui import LevelMeter, TestTone as Tone, _rx_hint, dbfs, initial_tx_level_db


def _radio(level_db=-12.0):
    return Radio("ht", "HT", "Digirig", "Digirig", "vox", frozenset({"fm"}), {}, level_db)


def test_radio_tx_level_round_trip_and_validation(tmp_path):
    path = tmp_path / "radios.toml"
    save_radios(path, RadioInventory({"ht": _radio()}, "ht"))
    loaded = load_radios(path).radios["ht"]
    assert loaded.tx_level_db == -12
    assert loaded.tx_level_linear == pytest.approx(10 ** (-12 / 20))
    path.write_text(path.read_text().replace("-12", "4"))
    with pytest.raises(ValueError, match="audio.tx_level_db"):
        load_radios(path)


def test_meter_reports_raw_clipping_and_rms():
    meter = LevelMeter()
    audio = np.array([[0.0], [0.5], [-1.0], [0.5]], dtype=np.float32)
    meter.callback(audio, 4, None, SimpleNamespace(input_overflow=True))
    peak, rms, clipped, overflows = meter.snapshot()
    assert peak == 1.0
    assert rms == pytest.approx(np.sqrt(1.5 / 4))
    assert clipped == 0.25
    assert overflows == 1
    assert dbfs(0) == float("-inf")


def test_solo_tuning_starts_attenuated_and_guides_open_squelch():
    assert initial_tx_level_db(0) == -24
    assert initial_tx_level_db(-14) == -14
    assert "open squelch" in _rx_hint(0, 0, 0)
    assert "CLIPPING" in _rx_hint(1, 0.01, 0)


def test_production_send_applies_configured_tx_attenuation(monkeypatch):
    sent = []
    radio = _radio(-6)
    modem = object.__new__(transport.RadioTransport)
    modem.radio = radio
    modem.receive_only = False
    modem.out_device = 3
    modem.ptt = object()
    modem._tx_lock = threading.Lock()
    modem._transmitting = threading.Event()
    modem._tx_underflows = 0
    monkeypatch.setattr(modem, "_clear_buffer", lambda: None)
    monkeypatch.setattr(transport, "_ensure_com_initialized", lambda: None)
    monkeypatch.setattr(audio_io, "transmit", lambda samples, *args, **kwargs: sent.append(samples.copy()) or 1.0)

    assert modem.send(np.array([0.5, -0.5], dtype=np.float32)) == 1.0
    np.testing.assert_allclose(sent[0], [0.5 * radio.tx_level_linear, -0.5 * radio.tx_level_linear])


def test_tone_attempts_unkey_after_failed_key_on(monkeypatch):
    class FakePtt:
        def __init__(self):
            self.calls = []
            self.closed = False

        def key(self, on):
            self.calls.append(on)
            if on:
                raise RuntimeError("key-on failed")
            return True

        def close(self):
            self.closed = True

    ptt = FakePtt()
    tone = Tone(SimpleNamespace(ptt=lambda: ptt), 3, -12)
    monkeypatch.setattr(audio_io, "_load_sounddevice", lambda: SimpleNamespace())
    with pytest.raises(RuntimeError, match="key-on failed"):
        tone.start()
    assert ptt.calls == [True, False]
    assert ptt.closed
    assert not tone.active


def test_tone_level_change_affects_next_audio_block():
    tone = Tone(None, 3, -20)
    first = np.zeros((4800, 1), dtype=np.float32)
    tone._callback(first, len(first), None, None)
    tone.level_db = -14
    second = np.zeros_like(first)
    tone._callback(second, len(second), None, None)
    first_rms = np.sqrt(np.mean(first[-2400:] ** 2))
    second_rms = np.sqrt(np.mean(second[-2400:] ** 2))
    assert second_rms / first_rms == pytest.approx(10 ** (6 / 20), rel=0.01)
