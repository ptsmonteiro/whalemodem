"""Read-only CI-V state dump for ic705 and ic7300 -- no PTT, no transmit.

Reads, for each radio:
  - operating frequency (cmd 0x03)
  - operating mode + filter (cmd 0x04)
  - RF power setting (cmd 0x14 0x0A)
  - data-mode status (cmd 0x1A 0x06), best-effort

This file must never call .key(), .send(), or anything that asserts PTT.
It opens the CI-V ports directly and only ever writes read-request frames.

Usage:
    python experiments/hf16_mfsk_lowsnr/radio_state.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from whale.hw import ptt as civ  # noqa: E402

# CI-V read commands used here (all read-only; none of these key the radio).
CMD_READ_FREQ = b"\x03"
CMD_READ_MODE = b"\x04"
CMD_READ_RF_POWER = b"\x14\x0A"
CMD_READ_DATA_MODE = b"\x1A\x06"

MODE_NAMES = {
    0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY",
    0x05: "FM", 0x06: "WFM", 0x07: "CW-R", 0x08: "RTTY-R", 0x17: "DV",
}


def _open(usb_id, name, expected_addr):
    """Finds and opens the radio's CI-V port read-only (no key() calls)."""
    candidates = civ.icom_civ_candidates(usb_id)
    if not candidates:
        return None, f"no serial port found for {name} (USB {usb_id[0]:04X}:{usb_id[1]:04X})"
    addrs = (civ.BROADCAST_ADDR, expected_addr)
    problems = []
    result = civ.discover_icom_civ(candidates, addrs=addrs, problems=problems)
    if result is None:
        if problems:
            detail = "; ".join(f"{port}: {exc.strerror or exc}" for port, exc in problems)
            return None, f"{name}: port busy / could not open ({detail}) -- likely held by WSJT-X or another CAT program"
        return None, f"{name}: no CI-V reply on {candidates} (radio off, wrong baud, or wrong address)"
    port, baud, addr = result
    ser = civ._open_quiet(port, baud, timeout=0.3)
    return (ser, port, baud, addr), None


def _transact(ser, radio_addr, cmd: bytes) -> bytes:
    """Sends one read-only CI-V command frame and returns the raw reply bytes.

    Keeps reading frames until a real reply from the radio (FE FE <E0> <radio_addr> ...)
    shows up, an NG is seen, or the deadline passes -- a single frame is not enough
    because "CI-V USB Echo Back" makes the first frame back our own outgoing command.
    """
    frame = civ.CIV_FRAME_START + bytes([radio_addr, civ.CONTROLLER_ADDR]) + cmd + civ.CIV_FRAME_END
    reply_marker = civ.CIV_FRAME_START + bytes([civ.CONTROLLER_ADDR, radio_addr])
    ser.reset_input_buffer()
    ser.write(frame)
    deadline = time.monotonic() + 0.5
    reply = b""
    while time.monotonic() < deadline:
        chunk = ser.read_until(civ.CIV_FRAME_END)
        if not chunk:
            break
        reply += chunk
        if civ._NG_RE.search(reply):
            break
        if reply_marker in reply:
            # Have we got the whole terminated reply frame yet?
            idx = reply.rfind(reply_marker)
            if civ.CIV_FRAME_END in reply[idx:]:
                break
    return reply


def _strip_echo(reply: bytes, radio_addr: int) -> bytes:
    """If echo-back is on, the first frame is our own outgoing command; return
    the frame addressed FE FE <E0> <radio_addr> ... FD (i.e. the radio's reply
    to the controller), preferring the last frame present."""
    marker = civ.CIV_FRAME_START + bytes([civ.CONTROLLER_ADDR, radio_addr])
    idx = reply.rfind(marker)
    if idx == -1:
        return reply
    end = reply.find(civ.CIV_FRAME_END, idx)
    return reply[idx:end + 1] if end != -1 else reply[idx:]


def _bcd_freq(payload: bytes) -> int | None:
    """Decodes the 5-byte little-endian BCD frequency payload of cmd 0x03 into Hz."""
    if len(payload) < 5:
        return None
    digits = []
    for byte in payload[:5]:
        digits.append(byte & 0x0F)
        digits.append((byte >> 4) & 0x0F)
    # digits[0] is least-significant decimal digit (1 Hz place)
    hz = 0
    for power, d in enumerate(digits):
        hz += d * (10 ** power)
    return hz


def read_radio(usb_id, name, expected_addr):
    opened, err = _open(usb_id, name, expected_addr)
    result = {"name": name, "error": err}
    if err:
        return result
    ser, port, baud, addr = opened
    result["port"] = port
    result["baud"] = baud
    result["addr"] = addr
    try:
        for label, cmd in (
            ("freq_raw", CMD_READ_FREQ),
            ("mode_raw", CMD_READ_MODE),
            ("power_raw", CMD_READ_RF_POWER),
            ("datamode_raw", CMD_READ_DATA_MODE),
        ):
            raw = _transact(ser, addr, cmd)
            result[label] = raw
    finally:
        ser.close()
    return result


def decode(result):
    out = dict(result)
    if result.get("error"):
        return out
    addr = result["addr"]

    freq_reply = _strip_echo(result["freq_raw"], addr)
    if civ._NG_RE.search(freq_reply):
        out["freq_hz"] = "NG (radio refused -- check CI-V USB port set to link with REMOTE?)"
    else:
        m = freq_reply
        # frame: FE FE <E0> <addr> 03 <5 bytes BCD> FD
        body_start = m.find(bytes([civ.CONTROLLER_ADDR, addr])) if m else -1
        payload = None
        if len(m) >= 11 and m[:2] == civ.CIV_FRAME_START:
            payload = m[5:10]
        out["freq_hz"] = _bcd_freq(payload) if payload else None

    mode_reply = _strip_echo(result["mode_raw"], addr)
    if civ._NG_RE.search(mode_reply):
        out["mode_name"] = "NG (radio refused)"
    elif len(mode_reply) >= 7 and mode_reply[:2] == civ.CIV_FRAME_START:
        mode_byte = mode_reply[5]
        filt_byte = mode_reply[6] if len(mode_reply) > 6 else None
        out["mode_name"] = MODE_NAMES.get(mode_byte, f"0x{mode_byte:02X}")
        out["filter"] = filt_byte
    else:
        out["mode_name"] = None

    power_reply = _strip_echo(result["power_raw"], addr)
    if civ._NG_RE.search(power_reply):
        out["power_pct"] = "NG (radio refused)"
    elif len(power_reply) >= 9 and power_reply[:2] == civ.CIV_FRAME_START:
        # FE FE <E0> <addr> 14 0A <2-byte BCD 0000-0255> FD
        bcd = power_reply[6:8]
        if len(bcd) == 2:
            val = (bcd[0] & 0x0F) + ((bcd[0] >> 4) & 0x0F) * 10 + (bcd[1] & 0x0F) * 100
            out["power_pct"] = round(val / 255 * 100, 1)
        else:
            out["power_pct"] = None
    else:
        out["power_pct"] = None

    dm_reply = _strip_echo(result["datamode_raw"], addr)
    if civ._NG_RE.search(dm_reply):
        out["data_mode"] = "NG (radio refused)"
    elif len(dm_reply) >= 7 and dm_reply[:2] == civ.CIV_FRAME_START:
        out["data_mode"] = "ON" if dm_reply[6] == 0x01 else "OFF"
    else:
        out["data_mode"] = None

    return out


def fmt_bytes(b):
    return b.hex(" ") if isinstance(b, (bytes, bytearray)) else str(b)


def main():
    radios = [
        (civ.IC705_USB_ID, "ic705", civ.IC705_DEFAULT_ADDR),
        (civ.IC7300_USB_ID, "ic7300", civ.IC7300_DEFAULT_ADDR),
    ]
    results = {}
    for usb_id, name, addr in radios:
        raw = read_radio(usb_id, name, addr)
        results[name] = decode(raw)

    print("=" * 78)
    print("CI-V READ-ONLY STATE DUMP (no PTT, no transmit)")
    print("=" * 78)
    for name, r in results.items():
        print(f"\n--- {name} ---")
        if r.get("error"):
            print(f"  FAILED: {r['error']}")
            continue
        print(f"  port={r['port']} baud={r['baud']} civ_addr=0x{r['addr']:02X}")
        print(f"  freq_raw   = {fmt_bytes(r['freq_raw'])}")
        print(f"  freq_hz    = {r.get('freq_hz')}")
        print(f"  mode_raw   = {fmt_bytes(r['mode_raw'])}")
        print(f"  mode       = {r.get('mode_name')}  filter={r.get('filter')}")
        print(f"  power_raw  = {fmt_bytes(r['power_raw'])}")
        print(f"  power_pct  = {r.get('power_pct')}")
        print(f"  datamode_raw = {fmt_bytes(r['datamode_raw'])}")
        print(f"  data_mode  = {r.get('data_mode')}")

    print("\n" + "=" * 78)
    r705, r7300 = results.get("ic705", {}), results.get("ic7300", {})
    if r705.get("error") or r7300.get("error"):
        print("VERDICT: cannot compare -- at least one radio failed to answer (see above).")
    else:
        f_match = r705.get("freq_hz") == r7300.get("freq_hz")
        m_match = r705.get("mode_name") == r7300.get("mode_name")
        print(f"VERDICT: frequency match = {f_match}  ({r705.get('freq_hz')} vs {r7300.get('freq_hz')})")
        print(f"         mode match      = {m_match}  ({r705.get('mode_name')} vs {r7300.get('mode_name')})")


if __name__ == "__main__":
    main()
