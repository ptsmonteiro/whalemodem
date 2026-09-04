"""Query real operating state (freq/mode/RF power/AGC) from the IC-7300 and
IC-705 over CI-V, for hardware-test metadata capture.

Ad hoc, read-only diagnostic -- not part of any production module. Uses
whale.hw.ptt's low-level CI-V transaction machinery directly rather than the
PTT-only IcomCivPtt class, since PTT keying is not needed here.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from whale.hw import ptt as ptt_mod  # noqa: E402
from whale.hw.radios import RADIOS  # noqa: E402

CIV_START = ptt_mod.CIV_FRAME_START
CIV_END = ptt_mod.CIV_FRAME_END
CONTROLLER = ptt_mod.CONTROLLER_ADDR


def transact(ser, addr, cmd, timeout=0.3):
    import time
    frame = CIV_START + bytes([addr, CONTROLLER]) + cmd + CIV_END
    ser.reset_input_buffer()
    ser.write(frame)
    deadline = time.monotonic() + timeout
    reply = b""
    while time.monotonic() < deadline:
        chunk = ser.read_until(CIV_END)
        if not chunk:
            break
        reply += chunk
    return reply


def bcd_freq_hz(payload: bytes) -> int:
    # Icom sends frequency as 5 BCD bytes, little-endian by byte, each byte
    # is two decimal digits (low nibble = least significant digit).
    digits = []
    for b in payload:
        digits.append(b & 0x0F)
        digits.append((b >> 4) & 0x0F)
    value = 0
    for d in reversed(digits):
        value = value * 10 + d
    return value


def query_radio(name):
    radio = RADIOS[name]
    usb_id_str = radio.ptt_config["usb_id"]
    vid_s, pid_s = usb_id_str.split(":")
    usb_id = (int(vid_s, 16), int(pid_s, 16))
    addr = radio.ptt_config["address"]
    candidates = ptt_mod.icom_civ_candidates(usb_id)
    if not candidates:
        print(f"{name}: no serial port found for USB {usb_id}")
        return
    port = candidates[0]
    ser = ptt_mod._open_quiet(port, 115200, 0.3)
    try:
        # 0x03: read operating frequency
        freq_reply = transact(ser, addr, b"\x03")
        # 0x04: read operating mode
        mode_reply = transact(ser, addr, b"\x04")
        # 0x14 0x0A: read RF power setting (0-255 scale)
        power_reply = transact(ser, addr, b"\x14\x0a")
        # 0x16 0x02: read AGC on/off/state (varies by model; best effort)
        agc_reply = transact(ser, addr, b"\x16\x02")
        print(f"== {name} (port {port}, addr 0x{addr:02X}) ==")
        print(f"  freq raw:  {freq_reply!r}")
        print(f"  mode raw:  {mode_reply!r}")
        print(f"  power raw: {power_reply!r}")
        print(f"  agc raw:   {agc_reply!r}")
        # Attempt to decode frequency: frame is FE FE <to> <from> 03 <5 bytes> FD
        # With CI-V USB echo-back on, the buffer holds our own echoed command
        # first, then the radio's real reply -- distinguish by direction:
        # the real reply has source=radio addr, dest=controller (0xE0).
        fm = freq_reply
        reply_prefix = CIV_START + bytes([CONTROLLER, addr])
        start = fm.find(reply_prefix)
        if start >= 0 and len(fm) >= start + 11:
            payload = fm[start + 5:start + 10]
            try:
                hz = bcd_freq_hz(payload)
                print(f"  decoded freq: {hz} Hz ({hz/1e6:.6f} MHz)")
            except Exception as exc:
                print(f"  freq decode failed: {exc}")
    finally:
        ser.close()


if __name__ == "__main__":
    for radio_name in ("ic7300", "ic705"):
        query_radio(radio_name)
