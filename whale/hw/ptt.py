"""PTT control mechanisms. Which radio uses which is set in radios.py.

IC-705:  keyed via CI-V ("Transmit ON/OFF", cmd 0x1C 0x00) over its USB CDC
         serial port. The IC-705 enumerates two COM ports (CI-V + GPS NMEA);
         discover_icom_civ() figures out which one is CI-V.

IC-7300: also CI-V, but over its built-in Silicon Labs CP210x USB-UART bridge
         (a single COM port) and with a different default radio address.

HT:      keyed via a Digirig-style interface that toggles RTS (or DTR) on a
         serial port to switch a transistor wired to the radio's PTT line.

COM port numbers move around between reboots and USB ports, so the radios are
located by USB VID:PID (see icom_civ_candidates()) rather than hardcoded.

Copied from radiomodem's shark/hw/ptt.py, since diverged: IcomCivPtt._transact
no longer blocks for a fixed serial timeout on every key() (see its docstring),
because that time is transmitter-on dead air here.
"""

import re
import time

import serial
from serial.tools import list_ports

CIV_FRAME_START = b"\xFE\xFE"
CIV_FRAME_END = b"\xFD"
BROADCAST_ADDR = 0x00
CONTROLLER_ADDR = 0xE0
IC705_DEFAULT_ADDR = 0xA4
IC7300_DEFAULT_ADDR = 0x94

# USB IDs of each radio's CAT interface: the IC-705 speaks CDC from its own
# Icom-branded chip, the IC-7300 through a Silicon Labs CP210x bridge.
IC705_USB_ID = (0x0C26, 0x0036)
IC7300_USB_ID = (0x10C4, 0xEA60)

# Reply to a "read transceiver ID" query: FE FE <controller> <radio> 19 00 <radio> FD
_ID_REPLY_RE = re.compile(rb"\xFE\xFE" + bytes([CONTROLLER_ADDR]) + rb"(.)\x19\x00\1\xFD", re.DOTALL)
# Generic CI-V OK / NG replies: FE FE <controller> <radio> FB/FA FD
_OK_RE = re.compile(rb"\xFE\xFE" + bytes([CONTROLLER_ADDR]) + rb".\xFB\xFD", re.DOTALL)
_NG_RE = re.compile(rb"\xFE\xFE" + bytes([CONTROLLER_ADDR]) + rb".\xFA\xFD", re.DOTALL)


def _open_quiet(port, baud, timeout=None):
    """Opens a serial port with RTS/DTR forced low before the lines can assert.

    pyserial defaults both control lines high, and some radios/interfaces
    treat RTS (or DTR) as a hardware PTT line -- so opening a port normally
    can key a transmitter before a single protocol byte is sent, independent
    of whatever commands follow. Build the port closed, force the lines down,
    then open.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    if timeout is not None:
        ser.timeout = timeout
    ser.rts = False
    ser.dtr = False
    ser.open()
    return ser


class IcomCivPtt:
    """Keys PTT on an Icom radio over CI-V."""

    def __init__(self, port, baud=115200, radio_addr=IC705_DEFAULT_ADDR, timeout=0.3):
        self.radio_addr = radio_addr
        self.timeout = timeout
        self.ser = _open_quiet(port, baud, timeout)

    def _transact(self, cmd: bytes) -> bytes:
        """Sends one CI-V command and returns whatever came back.

        Reads frame-at-a-time up to self.timeout rather than "sleep, then
        read a fixed 64 bytes": a CI-V reply is 6 bytes (12 with echo-back
        on), so read(64) never fills and always blocks for the full serial
        timeout. Combined with the fixed sleep that made every key() call
        take timeout + 0.1s -- 0.4s by default -- and the key(True) half of
        that is spent with the transmitter already keyed, so it was 0.4s of
        dead air on the front of every single frame. Reading until the CI-V
        frame terminator instead returns as soon as the radio has answered
        (a few ms), which is the whole point of asking for an ack.
        """
        frame = CIV_FRAME_START + bytes([self.radio_addr, CONTROLLER_ADDR]) + cmd + CIV_FRAME_END
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        deadline = time.monotonic() + self.timeout
        reply = b""
        while time.monotonic() < deadline:
            # One CI-V frame per read; with "CI-V USB Echo Back" enabled the
            # first one back is our own command, so keep reading until the
            # real ack shows up (or the deadline does).
            chunk = self.ser.read_until(CIV_FRAME_END)
            if not chunk:
                break
            reply += chunk
            if _OK_RE.search(reply) or _NG_RE.search(reply):
                break
        return reply

    def key(self, on: bool) -> bool:
        """Sends transmit ON/OFF. Returns True on CI-V 'OK', False on 'NG'."""
        reply = self._transact(b"\x1C\x00" + (b"\x01" if on else b"\x00"))
        if _OK_RE.search(reply):
            return True
        if _NG_RE.search(reply):
            return False
        raise TimeoutError(f"no CI-V reply to PTT {'ON' if on else 'OFF'} (got {reply!r})")

    def close(self):
        try:
            self.key(False)
        finally:
            self.ser.close()


def discover_icom_civ(ports, bauds=(115200, 19200, 9600), addrs=(BROADCAST_ADDR,), timeout=0.3,
                      problems=None):
    """Probes candidate ports/bauds with a CI-V transceiver-ID query.

    `addrs` are the destination addresses to try. The IC-705 answers a query
    sent to the broadcast address 0x00, but the IC-7300 ignores broadcasts and
    only replies when addressed directly -- so a radio's own address has to be
    among the candidates for it to be found at all.

    Radios with "CI-V USB Echo Back" enabled echo the query before replying, so
    the reply is searched for within the buffer rather than matched against it.

    Returns (port, baud, radio_addr) for the first port that answers as a CI-V
    radio, or None if none of them do.

    A port that cannot be *opened* is a different failure from one that opens
    and stays silent, and the difference is the whole diagnosis: a radio that is
    off or misaddressed gives silence, while a port already held by WSJT-X or a
    rig-control program refuses to open at all. Both used to arrive as "no CI-V
    response". Pass a list as `problems` to collect the open failures, so a
    caller can say which happened.
    """
    for port in ports:
        for baud in bauds:
            try:
                ser = _open_quiet(port, baud, timeout)
            except serial.SerialException as exc:
                if problems is not None:
                    problems.append((port, exc))
                # Every baud on this port will fail the same way; the port
                # itself is the problem, not the rate.
                break
            try:
                for addr in addrs:
                    query = (
                        CIV_FRAME_START + bytes([addr, CONTROLLER_ADDR])
                        + b"\x19\x00" + CIV_FRAME_END
                    )
                    ser.reset_input_buffer()
                    ser.write(query)
                    time.sleep(0.15)
                    match = _ID_REPLY_RE.search(ser.read(64))
                    if match:
                        return port, baud, match.group(1)[0]
            finally:
                ser.close()
    return None


def icom_civ_candidates(usb_id):
    """Lists COM ports belonging to a given USB (vid, pid), lowest port first."""
    vid, pid = usb_id
    ports = [p for p in list_ports.comports() if p.vid == vid and p.pid == pid]
    return [p.device for p in sorted(ports, key=lambda p: p.device)]


def open_icom_ptt(usb_id, name, expected_addr=None):
    """Finds the CI-V port of the radio on `usb_id` and returns an IcomCivPtt.

    The CP210x USB ID in particular is generic (any Silicon Labs bridge shares
    it), so the port is confirmed by an actual CI-V ID reply rather than by USB
    ID alone. `expected_addr` is probed alongside the broadcast address, since
    some radios (the IC-7300) only answer when addressed directly.
    """
    candidates = icom_civ_candidates(usb_id)
    if not candidates:
        raise RuntimeError(f"no serial port for {name} (USB {usb_id[0]:04X}:{usb_id[1]:04X})")
    addrs = (BROADCAST_ADDR,) if expected_addr is None else (BROADCAST_ADDR, expected_addr)
    problems = []
    result = discover_icom_civ(candidates, addrs=addrs, problems=problems)
    if result is None:
        if problems:
            detail = "; ".join(f"{port}: {exc.strerror or exc}" for port, exc in problems)
            raise RuntimeError(
                f"{name}'s serial port could not be opened ({detail}). Another program -- "
                "WSJT-X, a rig-control or CAT program, a serial terminal -- is holding it; "
                "close that and retry."
            )
        hint = "" if expected_addr is None else f" or its CI-V address is not 0x{expected_addr:02X}"
        raise RuntimeError(
            f"{name} did not answer a CI-V query on any of {candidates}; the port opened, so "
            f"the radio may be off or set to a different CI-V baud rate{hint}"
        )
    port, baud, addr = result
    if expected_addr is not None and addr != expected_addr:
        print(f"ptt: {name} on {port} answered as CI-V address 0x{addr:02X} "
              f"(expected 0x{expected_addr:02X}); using the reported address")
    return IcomCivPtt(port, baud, addr)


class LinePtt:
    """Keys PTT by toggling a serial control line (RTS or DTR), e.g. via a Digirig."""

    def __init__(self, port, line="rts", baud=9600, active_high=True):
        if line not in ("rts", "dtr"):
            raise ValueError("line must be 'rts' or 'dtr'")
        self.line = line
        self.active_high = active_high
        self.ser = _open_quiet(port, baud)

    def key(self, on: bool):
        level = on if self.active_high else not on
        if self.line == "rts":
            self.ser.rts = level
        else:
            self.ser.dtr = level

    def close(self):
        self.key(False)
        self.ser.close()
