"""Live sound-card level meter and bounded radio transmit test tone."""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import ExitStack
import curses
from dataclasses import replace
import math
import os
import sys
import threading
import time

import numpy as np

from whale.hw import audio_io, ptt as ptt_mod
from whale.hw.radios import load_radios, save_radios


SAMPLE_RATE = audio_io.SAMPLE_RATE
TX_LIMIT_SECONDS = 15.0
TONE_AMPLITUDE = 0.35  # two tones sum to at most 0.7 full scale
UNCONFIGURED_TX_START_DB = -24.0


def dbfs(value: float) -> float:
    return 20 * math.log10(value) if value > 0 else float("-inf")


def scale_tx(audio, level_db: float):
    """Apply the same attenuation used by RadioTransport.send()."""
    return np.asarray(audio, dtype=np.float32) * (10 ** (level_db / 20))


def initial_tx_level_db(saved_level_db: float) -> float:
    """Begin an unconfigured radio well below full PC output."""
    return UNCONFIGURED_TX_START_DB if saved_level_db == 0 else saved_level_db


class LevelMeter:
    """Two-second raw input peak/RMS/clipping window; no post-ADC gain."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocks = deque()
        self.overflows = 0

    def callback(self, indata, frames, time_info, status) -> None:
        samples = indata[:, 0]
        peak = float(np.max(np.abs(samples))) if frames else 0.0
        power = float(np.sum(np.square(samples, dtype=np.float64)))
        clipped = int(np.count_nonzero(np.abs(samples) >= 0.99))
        now = time.monotonic()
        with self._lock:
            self._blocks.append((now, peak, power, frames, clipped))
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
        tone = TONE_AMPLITUDE * (
            np.sin(2 * np.pi * 1200 * indices / SAMPLE_RATE)
            + np.sin(2 * np.pi * 2200 * indices / SAMPLE_RATE)
        )
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


def _rx_hint(peak: float, clipping: float, overflows: int) -> str:
    if clipping or peak >= 0.98:
        return "CLIPPING: lower radio volume or OS recording gain; software attenuation cannot repair it."
    if overflows:
        return "Input overflow: audio was dropped. Close other audio apps or increase buffering."
    if peak >= 0.8:
        return "Near full scale: lower radio volume or OS recording gain."
    if peak < 0.02:
        return "Quiet/no signal: open squelch on an unused channel; raise HT volume slowly."
    if peak < 0.2:
        return "Low level: with squelch open, raise HT volume slowly. Noise is only a rough check."
    return "Input has headroom. Open-squelch noise is a rough check, not a data-signal test."


def run(screen, radio, config_path: str, inventory, tx_device: int, rx_device: int) -> None:
    sd = audio_io._load_sounddevice()
    meter = LevelMeter()
    level_db = initial_tx_level_db(radio.tx_level_db)
    tone = TestTone(radio, tx_device, level_db)
    saved_db = radio.tx_level_db
    message = ("TX starts at -24 dB; save only after choosing a level."
               if radio.tx_level_db == 0 else "")
    curses.curs_set(0)
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
                    message = "Transmit stopped at the 15-second limit."
                peak, rms, clipped, overflows = meter.snapshot()
                screen.erase()
                _line(screen, 0, f"Whale levels: {radio.id}  |  {config_path}", curses.A_BOLD)
                _line(screen, 2, f"RX peak  {_bar(peak)}  {dbfs(peak):6.1f} dBFS")
                _line(screen, 3, f"RX RMS   {_bar(rms)}  {dbfs(rms):6.1f} dBFS")
                _line(screen, 4, f"Clip samples: {clipped * 100:.2f}%   Input overflows: {overflows}")
                _line(screen, 6, _rx_hint(peak, clipped, overflows))
                _line(screen, 8, f"TX attenuation: {level_db:+.0f} dB  ({10 ** (level_db / 20):.3f}x)"
                      + ("  * unsaved" if level_db != saved_db else ""))
                state = "ON" if tone.active else "off"
                _line(screen, 9, f"Test tone: {state}; two tones at 1200/2200 Hz; max 15 s per keying")
                _line(screen, 10, f"Output underflows this keying: {tone.underflows}")
                _line(screen, 11, f"Test-tone PC digital peak: at most {dbfs(0.7 * 10 ** (level_db / 20)):.1f} dBFS")
                _line(screen, 12, "[+/-] TX level  [t] test tone on/off  [s] save TX level  [q] quit")
                _line(screen, 13, "Solo: TX mic distortion/deviation is unmeasured without radio feedback.")
                _line(screen, 15, message)
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
                        message = "Test tone stopped."
                    else:
                        tone.start()
                        message = "Transmitting test tone; press t to stop."
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
        tx_device, rx_device = radio.devices()
        curses.wrapper(run, radio, path, inventory, tx_device, rx_device)
    except Exception as exc:
        print(f"whale-levels: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
