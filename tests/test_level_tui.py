import threading
from types import SimpleNamespace

import numpy as np
import pytest

from whale import transport
from whale.hw import audio_io
from whale.hw.radios import Radio, RadioInventory, load_radios, save_radios
from whale import level_tui
from whale.level_tui import (LevelMeter, TestTone as Tone, _rx_hint, dbfs,
                             _probe_lines, initial_tx_level_db, measure_probe,
                             ProbeResult)


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
    assert "headroom" in _rx_hint(10 ** (-17.1 / 20), 0, 0)
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


def test_probe_has_exact_peak_and_callback_repeats_without_boundary_jump():
    assert np.max(np.abs(level_tui.PROBE_AUDIO)) == pytest.approx(0.7)
    assert level_tui.PROBE_FREQUENCIES[[0, -1]].tolist() == [500.0, 3000.0]
    assert np.all((level_tui.PROBE_FREQUENCIES / 50).astype(int) ==
                  level_tui.PROBE_FREQUENCIES / 50)
    tone = Tone(None, 3, 0)
    tone.sample_pos = 2 * level_tui.PROBE_PERIOD - 10
    block = np.zeros((30, 1), dtype=np.float32)
    tone._callback(block, len(block), None, None)
    indices = np.arange(2 * level_tui.PROBE_PERIOD - 10,
                        2 * level_tui.PROBE_PERIOD + 20)
    np.testing.assert_array_equal(
        block[:, 0], level_tui.PROBE_AUDIO[indices % level_tui.PROBE_PERIOD])


def _probe(seconds=2.0):
    periods = round(seconds * level_tui.SAMPLE_RATE / level_tui.PROBE_PERIOD)
    return np.tile(level_tui.PROBE_AUDIO, periods)


def test_clean_probe_has_flat_carriers_low_leakage_and_evm():
    # Capture begins at an arbitrary point in the repeating waveform.
    result = measure_probe(np.roll(_probe(), 317))
    assert result.available
    assert result.flatness_db < 0.01
    assert result.leakage_db < -100
    assert result.evm_db < -100


def test_probe_reports_noise_and_clipping():
    rng = np.random.default_rng(4)
    clean = _probe()
    noisy = measure_probe(clean + rng.normal(0, 0.02, len(clean)))
    clipped = measure_probe(np.clip(clean * 3, -0.45, 0.45))
    assert noisy.available and clipped.available
    assert -60 < noisy.leakage_db < -35
    assert clipped.leakage_db > -35
    assert clipped.evm_db > -30


def test_probe_waits_when_probe_is_missing():
    rng = np.random.default_rng(8)
    result = measure_probe(rng.normal(0, 0.001, 96_000))
    assert not result.available


def test_probe_tracks_sound_card_clock_mismatch():
    source = _probe(2.1)
    output_length = int(len(source) / 1.00025)
    positions = np.arange(output_length) * 1.00025
    received = np.interp(positions, np.arange(len(source)), source)
    result = measure_probe(received)
    assert result.available
    assert result.clock_ppm == pytest.approx(250, abs=5)
    assert result.evm_db < -45


def test_probe_evm_removes_smooth_linear_audio_response():
    # A short FIR gives smooth passband amplitude and phase shaping.
    shaped = np.convolve(_probe(), np.array([1.0, -0.5]), mode="same")
    result = measure_probe(shaped)
    assert result.available
    assert result.flatness_db > 1.0
    assert result.evm_db < -45


def test_probe_gauges_are_full_when_good_and_empty_when_poor():
    good = _probe_lines(ProbeResult(True, 2.0, -40.0, -35.0, -50.0))
    poor = _probe_lines(ProbeResult(True, 14.0, -20.0, -10.0, 700.0))

    assert [status for _, status in good] == ["GOOD"] * 4
    assert all("[####################]" in line for line, _ in good)
    assert [status for _, status in poor] == ["POOR"] * 4
    assert all("[--------------------]" in line for line, _ in poor)
    assert "+700 ppm" in poor[3][0]


def test_probe_gauges_have_an_amber_transition_band():
    gauges = _probe_lines(ProbeResult(True, 10.0, -30.0, -15.0, -300.0))
    assert [status for _, status in gauges] == ["CHECK"] * 4
    assert all("[##########----------]" in line for line, _ in gauges)


def test_probe_gauges_accept_known_all_mode_radio_setting():
    gauges = _probe_lines(ProbeResult(True, 6.9, -51.8, -18.9, -10.0))
    assert [status for _, status in gauges] == ["GOOD"] * 4


class _TunerScreen:
    def __init__(self, keys):
        self.keys = iter(keys)

    def timeout(self, _milliseconds):
        pass

    def erase(self):
        pass

    def getmaxyx(self):
        return (24, 100)

    def addnstr(self, *_args):
        pass

    def refresh(self):
        pass

    def getch(self):
        key = next(self.keys)
        if isinstance(key, Exception):
            raise key
        return key


class _InputStream:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class _TunerTone:
    instances = []

    def __init__(self, _radio, _device, level_db):
        self.level_db = level_db
        self.active = False
        self.underflows = 0
        self.stop_calls = 0
        self.instances.append(self)

    def expired(self):
        return False

    def stop(self):
        self.active = False
        self.stop_calls += 1

    def start(self):
        self.active = True


def _stub_tuner_hardware(monkeypatch):
    _TunerTone.instances.clear()
    sounddevice = SimpleNamespace(InputStream=lambda **_kwargs: _InputStream())
    monkeypatch.setattr(audio_io, "_load_sounddevice", lambda: sounddevice)
    monkeypatch.setattr(level_tui, "TestTone", _TunerTone)
    monkeypatch.setattr(level_tui.curses, "curs_set", lambda _value: None)
    monkeypatch.setattr(level_tui, "_init_colors", lambda: {})


def test_embedded_tuner_applies_adjusted_level_only_through_callback(monkeypatch):
    _stub_tuner_hardware(monkeypatch)
    applied = []

    level_tui.run_level_tuner(
        _TunerScreen([ord("+"), ord("s"), ord("q")]),
        _radio(-12),
        Radio("monitor", "Monitor", "RX", "unused", "vox", frozenset({"fm"}), {}),
        "working configuration",
        3,
        6,
        applied.append,
    )

    assert applied == [-11]
    assert _TunerTone.instances[0].stop_calls >= 1


def test_embedded_tuner_quit_cancels_unapplied_adjustment(monkeypatch):
    _stub_tuner_hardware(monkeypatch)
    applied = []

    level_tui.run_level_tuner(
        _TunerScreen([ord("-"), 27]), _radio(-12), _radio(), "config", 3, 6,
        applied.append,
    )

    assert applied == []


def test_local_tuner_keeps_the_selected_radio_input_open(monkeypatch):
    _stub_tuner_hardware(monkeypatch)
    streams = []
    sounddevice = SimpleNamespace(
        InputStream=lambda **kwargs: streams.append(kwargs) or _InputStream())
    monkeypatch.setattr(audio_io, "_load_sounddevice", lambda: sounddevice)

    radio = _radio()
    level_tui.run_level_tuner(
        _TunerScreen([ord("q")]), radio, radio, "config", 3, 4,
        lambda _level: None,
    )

    assert streams[0]["device"] == 4


def test_tuner_can_keep_input_metering_while_disabling_probe_analysis(monkeypatch):
    _stub_tuner_hardware(monkeypatch)
    monkeypatch.setattr(
        LevelMeter, "probe_snapshot",
        lambda _self: pytest.fail("probe analysis must be unavailable"),
    )

    radio = _radio()
    level_tui.run_level_tuner(
        _TunerScreen([ord("d"), ord("q")]), radio, radio, "config", 3, 4,
        lambda _level: None, True, probe_analysis_available=False,
    )


def test_embedded_tuner_stops_transmit_when_ui_fails(monkeypatch):
    _stub_tuner_hardware(monkeypatch)

    with pytest.raises(RuntimeError, match="terminal failed"):
        level_tui.run_level_tuner(
            _TunerScreen([RuntimeError("terminal failed")]),
            _radio(), _radio(), "config", 3, 6, lambda _level: None,
        )

    assert _TunerTone.instances[0].stop_calls >= 1
