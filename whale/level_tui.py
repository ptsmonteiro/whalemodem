"""Live sound-card level meter and bounded radio multicarrier probe."""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import ExitStack
import curses
from dataclasses import dataclass, replace
import math
import os
import sys
import threading
import time
import numpy as np

from whale.hw import audio_io, ptt as ptt_mod
from whale.hw.radios import load_radios, save_radios


SAMPLE_RATE = audio_io.SAMPLE_RATE
TX_LIMIT_SECONDS = 60.0
PROBE_SPACING_HZ = 50.0
PROBE_LOW_HZ = 500.0
PROBE_HIGH_HZ = 3000.0
PROBE_PERIOD = int(SAMPLE_RATE / PROBE_SPACING_HZ)
PROBE_PEAK = 0.7
PROBE_MIN_RMS = 0.004
UNCONFIGURED_TX_START_DB = -24.0

COLOR_GOOD = 1
COLOR_WARN = 2
COLOR_BAD = 3


PROBE_FREQUENCIES = np.arange(PROBE_LOW_HZ, PROBE_HIGH_HZ + 1,
                              PROBE_SPACING_HZ)
# Regularly interleave nulls without the odd/even symmetry that can make odd
# clipping products land only on occupied bins.
PROBE_OCCUPIED = np.arange(len(PROBE_FREQUENCIES)) % 3 != 1
PROBE_NULL = ~PROBE_OCCUPIED


def _make_probe() -> tuple[np.ndarray, np.ndarray]:
    """Return one repeatable 20 ms real multicarrier period and its phasors."""
    rng = np.random.default_rng(0x5748414C45)
    phases = rng.choice((0.0, np.pi / 2, np.pi, 3 * np.pi / 2),
                        np.count_nonzero(PROBE_OCCUPIED))
    n = np.arange(PROBE_PERIOD)
    raw = np.sum(np.cos(2 * np.pi * PROBE_FREQUENCIES[PROBE_OCCUPIED, None]
                        * n / SAMPLE_RATE + phases[:, None]), axis=0)
    raw *= PROBE_PEAK / np.max(np.abs(raw))
    return raw.astype(np.float32), np.exp(1j * phases)


PROBE_AUDIO, PROBE_SYMBOLS = _make_probe()


def dbfs(value: float) -> float:
    return 20 * math.log10(value) if value > 0 else float("-inf")


def scale_tx(audio, level_db: float):
    """Apply the same attenuation used by RadioTransport.send()."""
    return np.asarray(audio, dtype=np.float32) * (10 ** (level_db / 20))


def initial_tx_level_db(saved_level_db: float) -> float:
    """Begin an unconfigured radio well below full PC output."""
    return UNCONFIGURED_TX_START_DB if saved_level_db == 0 else saved_level_db


@dataclass(frozen=True)
class ProbeResult:
    available: bool
    flatness_db: float = float("nan")
    leakage_db: float = float("nan")
    evm_db: float = float("nan")
    clock_ppm: float = float("nan")


def measure_probe(audio, sample_rate: float = SAMPLE_RATE) -> ProbeResult:
    """Measure the known multicarrier probe after clock and channel fitting.

    Flatness is occupied-carrier peak-to-peak level.  Leakage compares empty
    and occupied grid-bin powers.  EVM removes a smooth linear channel (gain,
    phase, delay and audio response), leaving non-smooth occupied-bin error.
    """
    samples = np.asarray(audio, dtype=np.float64)
    if samples.ndim != 1 or len(samples) < sample_rate / 2:
        return ProbeResult(False)
    samples = samples - np.mean(samples)
    if np.sqrt(np.mean(samples * samples)) < PROBE_MIN_RMS:
        return ProbeResult(False)

    # Find each occupied peak, then regress its observed frequency against its
    # nominal frequency.  This estimates independent sound-card clock error.
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window))
    fft_hz = np.fft.rfftfreq(len(samples), 1.0 / sample_rate)
    observed = []
    nominal = PROBE_FREQUENCIES[PROBE_OCCUPIED]
    for frequency in nominal:
        bins = np.flatnonzero((fft_hz >= frequency - 2.0) &
                              (fft_hz <= frequency + 2.0))
        k = int(bins[np.argmax(spectrum[bins])])
        delta = 0.0
        if 0 < k < len(spectrum) - 1:
            a, b, c = np.log(np.maximum(spectrum[k - 1:k + 2], 1e-30))
            denominator = a - 2 * b + c
            if denominator:
                delta = float(np.clip(0.5 * (a - c) / denominator, -0.5, 0.5))
        observed.append((k + delta) * sample_rate / len(samples))
    scale = float(np.dot(nominal, observed) / np.dot(nominal, nominal))
    if not 0.998 <= scale <= 1.002:
        return ProbeResult(False)

    # Resolve the arbitrary capture boundary over one probe period.  Keeping
    # this explicit avoids asking phase unwrapping to infer a potentially
    # hundreds-of-samples delay across the interleaved (nonuniform) tones.
    first_period = samples[:PROBE_PERIOD]
    circular = np.fft.irfft(np.fft.rfft(first_period) *
                            np.conj(np.fft.rfft(PROBE_AUDIO)))
    capture_lag = int(np.argmax(circular))
    captured_symbols = PROBE_SYMBOLS * np.exp(
        -2j * np.pi * nominal * capture_lag / sample_rate)

    # Complex correlation at clock-corrected frequencies is independent of
    # the arbitrary capture boundary and avoids FFT-bin scalloping.
    n = np.arange(len(samples), dtype=np.float64)
    coefficients = np.asarray([
        2.0 / len(samples) * np.dot(samples,
            np.exp(-2j * np.pi * frequency * scale * n / sample_rate))
        for frequency in PROBE_FREQUENCIES
    ])
    occupied = coefficients[PROBE_OCCUPIED]
    levels = np.abs(occupied)
    null_levels = np.abs(coefficients[PROBE_NULL])
    if np.median(levels) < PROBE_MIN_RMS or np.median(levels) < 3 * np.median(null_levels):
        return ProbeResult(False)

    flatness_db = float(20 * np.log10(np.max(levels) / np.min(levels)))
    leakage_db = float(10 * np.log10(
        np.mean(null_levels ** 2) / np.mean(levels ** 2)))

    channel = occupied / captured_symbols
    x = np.linspace(-1.0, 1.0, len(channel))
    # A low-order smooth fit removes gain, delay/phase and ordinary radio
    # passband shaping without fitting away carrier-to-carrier products.
    log_amplitude = np.log(np.maximum(np.abs(channel), 1e-30))
    phase = np.unwrap(np.angle(channel))
    degree = min(5, len(channel) - 1)
    model = np.exp(np.polyval(np.polyfit(x, log_amplitude, degree), x)
                   + 1j * np.polyval(np.polyfit(x, phase, degree), x))
    expected = captured_symbols * model
    evm = math.sqrt(float(np.mean(np.abs(occupied - expected) ** 2)
                          / np.mean(np.abs(expected) ** 2)))
    return ProbeResult(True, flatness_db, leakage_db, dbfs(evm),
                       (scale - 1.0) * 1e6)


class LevelMeter:
    """Two-second raw input peak/RMS/clipping window; no post-ADC gain."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocks = deque()
        self.overflows = 0
        self._last_probe_at = 0.0
        self._last_probe = ProbeResult(False)

    def callback(self, indata, frames, time_info, status) -> None:
        samples = indata[:, 0]
        peak = float(np.max(np.abs(samples))) if frames else 0.0
        power = float(np.sum(np.square(samples, dtype=np.float64)))
        clipped = int(np.count_nonzero(np.abs(samples) >= 0.99))
        now = time.monotonic()
        with self._lock:
            self._blocks.append((now, peak, power, frames, clipped,
                                 np.array(samples, dtype=np.float32, copy=True)))
            self._prune(now)
            if status and status.input_overflow:
                self.overflows += 1

    def _prune(self, now: float) -> None:
        while self._blocks and self._blocks[0][0] < now - 2.0:
            self._blocks.popleft()

    def snapshot(self) -> tuple[float, float, float, int]:
        with self._lock:
            self._prune(time.monotonic())
            blocks = list(self._blocks)
            overflows = self.overflows
        if not blocks:
            return 0.0, 0.0, 0.0, overflows
        frames = sum(block[3] for block in blocks)
        peak = max(block[1] for block in blocks)
        rms = math.sqrt(sum(block[2] for block in blocks) / frames) if frames else 0.0
        clip_fraction = sum(block[4] for block in blocks) / frames if frames else 0.0
        return peak, rms, clip_fraction, overflows

    def probe_snapshot(self) -> ProbeResult:
        """Analyze a copied bounded window outside PortAudio's callback."""
        now = time.monotonic()
        if now - self._last_probe_at < 0.5:
            return self._last_probe
        with self._lock:
            self._prune(now)
            audio = np.concatenate([block[5] for block in self._blocks]) \
                if self._blocks else np.zeros(0, dtype=np.float32)
        self._last_probe = measure_probe(audio)
        self._last_probe_at = now
        return self._last_probe


class TestTone:
    """Key only on request, with a hard time limit and guaranteed un-key attempt."""

    def __init__(self, radio, device: int, level_db: float) -> None:
        self.radio = radio
        self.device = device
        self.level_db = level_db
        self.ptt = None
        self.stream = None
        self.started_at = None
        self.sample_pos = 0
        self.underflows = 0

    @property
    def active(self) -> bool:
        return self.started_at is not None

    def _callback(self, outdata, frames, time_info, status) -> None:
        if status and status.output_underflow:
            self.underflows += 1
        indices = np.arange(self.sample_pos, self.sample_pos + frames)
        tone = PROBE_AUDIO[indices % PROBE_PERIOD]
        ramp = np.minimum(1.0, indices / (0.02 * SAMPLE_RATE))
        outdata[:, 0] = scale_tx(tone * ramp, self.level_db)
        self.sample_pos += frames

    def start(self) -> None:
        if self.active:
            return
        sd = audio_io._load_sounddevice()
        self.sample_pos = 0
        self.underflows = 0
        try:
            self.ptt = self.radio.ptt()
            # Key-on belongs inside the protected span: it can fail after
            # asserting PTT, in which case an un-key must still be attempted.
            self.ptt.key(True)
            time.sleep(0.2)
            self.stream = sd.OutputStream(
                device=self.device, samplerate=SAMPLE_RATE, channels=1,
                dtype="float32", latency=0.1, callback=self._callback,
            )
            self.stream.start()
            self.started_at = time.monotonic()
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        stream, ptt = self.stream, self.ptt
        self.stream = None
        self.ptt = None
        self.started_at = None
        try:
            if stream is not None:
                try:
                    stream.stop()
                finally:
                    stream.close()
        finally:
            if ptt is not None:
                try:
                    ptt_mod.unkey(ptt)
                finally:
                    ptt.close()

    def expired(self) -> bool:
        return self.active and time.monotonic() - self.started_at >= TX_LIMIT_SECONDS


def _line(screen, row: int, message: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if row >= height or width < 2:
        return
    try:
        screen.addnstr(row, 0, message, width - 1, attr)
    except curses.error:
        pass


def _bar(value: float, width: int = 34) -> str:
    filled = max(0, min(width, round(value * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _probe_gauge(label: str, value: float, unit: str, good: float,
                 bad: float, width: int = 20, *, absolute: bool = False,
                 decimals: int = 1) -> tuple[str, str]:
    """Format a lower-is-better probe metric as a full-is-good gauge."""
    judged = abs(value) if absolute else value
    quality = (bad - judged) / (bad - good)
    status = "GOOD" if judged <= good else "CHECK" if judged <= bad else "POOR"
    number = f"{value:+.{decimals}f}" if absolute else f"{value:.{decimals}f}"
    return (f"{label:<12} {_bar(quality, width)} {number:>7} {unit:<6} {status}",
            status)


def _probe_lines(result: ProbeResult) -> list[tuple[str, str]]:
    """Return calibrated gauges for the receiver probe measurements."""
    return [
        _probe_gauge("RX flatness", result.flatness_db, "dB p-p", 8.0, 12.0),
        _probe_gauge("Null leakage", result.leakage_db, "dBc", -35.0, -25.0),
        _probe_gauge("EVM", result.evm_db, "dB", -18.0, -12.0),
        _probe_gauge("Clock", result.clock_ppm, "ppm", 100.0, 500.0,
                     absolute=True, decimals=0),
    ]


def _init_colors() -> dict[str, int]:
    """Create readable status colors, or plain attributes when unavailable."""
    attrs = {"GOOD": 0, "CHECK": curses.A_BOLD, "POOR": curses.A_BOLD}
    try:
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(COLOR_GOOD, curses.COLOR_GREEN, -1)
            curses.init_pair(COLOR_WARN, curses.COLOR_YELLOW, -1)
            curses.init_pair(COLOR_BAD, curses.COLOR_RED, -1)
            attrs = {
                "GOOD": curses.color_pair(COLOR_GOOD),
                "CHECK": curses.color_pair(COLOR_WARN) | curses.A_BOLD,
                "POOR": curses.color_pair(COLOR_BAD) | curses.A_BOLD,
            }
    except curses.error:
        pass
    return attrs


def _rx_hint(peak: float, clipping: float, overflows: int) -> str:
    if clipping or peak >= 0.98:
        return "CLIPPING: lower radio volume or OS recording gain; software attenuation cannot repair it."
    if overflows:
        return "Input overflow: audio was dropped. Close other audio apps or increase buffering."
    if peak >= 0.8:
        return "Near full scale: lower radio volume or OS recording gain."
    if peak < 0.02:
        return "Quiet/no signal: open squelch on an unused channel; raise HT volume slowly."
    if peak < 0.1:
        return "Low level: with squelch open, raise HT volume slowly. Noise is only a rough check."
    return "Input has headroom. Open-squelch noise is a rough check, not a data-signal test."


def run(screen, radio, receive_radio, config_path: str, inventory,
        tx_device: int, rx_device: int, distortion_enabled: bool = False) -> None:
    sd = audio_io._load_sounddevice()
    meter = LevelMeter()
    level_db = initial_tx_level_db(radio.tx_level_db)
    tone = TestTone(radio, tx_device, level_db)
    saved_db = radio.tx_level_db
    message = ("TX starts at -24 dB; save only after choosing a level."
               if radio.tx_level_db == 0 else "")
    curses.curs_set(0)
    status_attrs = _init_colors()
    screen.timeout(100)
    input_stream = sd.InputStream(
        device=rx_device, samplerate=SAMPLE_RATE, channels=1,
        dtype="float32", latency=0.1, callback=meter.callback,
    )
    try:
        with ExitStack() as stack:
            stack.enter_context(input_stream)
            # Stop TX before the RX device closes, including on exceptions.
            stack.callback(tone.stop)
            while True:
                if tone.expired():
                    tone.stop()
                    message = "Transmit stopped at the 60-second limit."
                peak, rms, clipped, overflows = meter.snapshot()
                distortion = meter.probe_snapshot() if distortion_enabled else None
                screen.erase()
                _line(screen, 0, f"Whale levels: {radio.id}  |  {config_path}", curses.A_BOLD)
                _line(screen, 2, f"RX peak  {_bar(peak)}  {dbfs(peak):6.1f} dBFS")
                _line(screen, 3, f"RX RMS   {_bar(rms)}  {dbfs(rms):6.1f} dBFS")
                _line(screen, 4, f"Clip samples: {clipped * 100:.2f}%   Input overflows: {overflows}")
                _line(screen, 6, _rx_hint(peak, clipped, overflows))
                _line(screen, 8, f"TX attenuation: {level_db:+.0f} dB  ({10 ** (level_db / 20):.3f}x)"
                      + ("  * unsaved" if level_db != saved_db else ""))
                state = "ON" if tone.active else "off"
                _line(screen, 9, f"Test probe: {state}; {PROBE_LOW_HZ:g}-{PROBE_HIGH_HZ:g} Hz / "
                      f"{PROBE_SPACING_HZ:g} Hz grid; max 60 s")
                _line(screen, 10, f"Output underflows this keying: {tone.underflows}")
                _line(screen, 11, f"Probe PC digital peak: {dbfs(PROBE_PEAK * 10 ** (level_db / 20)):.1f} dBFS")
                _line(screen, 12, "[+/-] TX level  [t] probe  [d] RX analysis  [s] save  [q] quit")
                if distortion is None:
                    _line(screen, 13, "RX probe analysis: off (press d to enable)")
                elif not distortion.available:
                    _line(screen, 13, "RX probe analysis: waiting for the multicarrier probe")
                else:
                    _line(screen, 13, "RX probe quality (full bar is better):", curses.A_BOLD)
                    for row, (gauge, status) in enumerate(_probe_lines(distortion), 14):
                        _line(screen, row, gauge, status_attrs[status])
                _line(screen, 18, f"Receiver: {receive_radio.id} | whole audio path; includes noise")
                _line(screen, 19, "Targets: flat <=8 dB | leakage <=-35 dBc | EVM <=-18 dB | clock <=100 ppm")
                _line(screen, 21, message)
                screen.refresh()
                key = screen.getch()
                if key in (ord("q"), 27):
                    break
                if key in (ord("+"), ord("=")):
                    level_db = min(0.0, level_db + 1.0)
                    tone.level_db = level_db
                elif key in (ord("-"), ord("_")):
                    level_db = max(-60.0, level_db - 1.0)
                    tone.level_db = level_db
                elif key == ord("t"):
                    if tone.active:
                        tone.stop()
                        message = "Test probe stopped."
                    else:
                        tone.start()
                        message = "Transmitting test probe; press t to stop."
                elif key == ord("d"):
                    distortion_enabled = not distortion_enabled
                    message = f"RX probe analysis {'enabled' if distortion_enabled else 'disabled'}."
                elif key == ord("s"):
                    updated = replace(radio, tx_level_db=level_db)
                    inventory.radios[radio.id] = updated
                    save_radios(config_path, inventory)
                    saved_db = level_db
                    message = f"Saved audio.tx_level_db = {level_db:g} to {config_path}."
    finally:
        tone.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live Digirig/radio audio level tuner")
    parser.add_argument("--radio-config", help="Radio inventory TOML (default: WHALE_RADIO_CONFIG or radios.toml)")
    parser.add_argument("--radio", help="Radio inventory key (default: default_radio)")
    parser.add_argument("--receive-radio", help="Optional receiving radio inventory key (default: --radio)")
    args = parser.parse_args(argv)
    path = args.radio_config or os.environ.get("WHALE_RADIO_CONFIG") or "radios.toml"
    try:
        inventory = load_radios(path)
        name = args.radio or inventory.default
        if name is None:
            raise ValueError("choose a radio with --radio (no default_radio is configured)")
        if name not in inventory.radios:
            raise ValueError(f"unknown radio {name!r}; have {sorted(inventory.radios)}")
        radio = inventory.radios[name]
        receive_name = args.receive_radio or name
        if receive_name not in inventory.radios:
            raise ValueError(f"unknown receiving radio {receive_name!r}; have {sorted(inventory.radios)}")
        receive_radio = inventory.radios[receive_name]
        tx_device, _ = radio.devices()
        _, rx_device = receive_radio.devices()
        curses.wrapper(run, radio, receive_radio, path, inventory, tx_device, rx_device,
                       args.receive_radio is not None)
    except Exception as exc:
        print(f"whale-levels: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
