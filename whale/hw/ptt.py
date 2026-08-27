"""Low-level serial PTT mechanisms used by the built-in backends.

IC-705:  keyed via CI-V ("Transmit ON/OFF", cmd 0x1C 0x00) over its USB CDC
         serial port. The IC-705 enumerates two COM ports (CI-V + GPS NMEA);
         discover_icom_civ() figures out which one is CI-V.

IC-7300: also CI-V, but over its built-in Silicon Labs CP210x USB-UART bridge
         (a single COM port) and with a different default radio address.

HT:      keyed via a Digirig-style interface that toggles RTS (or DTR) on a
         serial port to switch a transistor wired to the radio's PTT line.

COM port numbers move around between reboots and USB ports, so the radios are
located by USB VID:PID (see icom_civ_candidates()) rather than hardcoded.

Copied from radiomodem's shark/hw/ptt.py, since diverged:

  - IcomCivPtt._transact no longer blocks for a fixed serial timeout on every
    key() (see its docstring), because that time is transmitter-on dead air
    here.
  - key(False) no longer raises on a radio that has stopped answering; it
    retries and then writes transmit-off blind. See the un-keying notes below
    -- that is a hardware-safety property, not a tidy-up.
"""

import logging
import re
import threading
import time

import serial
from serial.tools import list_ports

_log = logging.getLogger(__name__)

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

# "Transmit ON/OFF" (CI-V cmd 0x1C 0x00); the data byte is 0x01 on, 0x00 off.
PTT_CMD = b"\x1C\x00"
PTT_ON = PTT_CMD + b"\x01"
PTT_OFF = PTT_CMD + b"\x00"

# A serial *write* must never be able to block the un-key path forever.
#
# pyserial defaults write_timeout to None, i.e. an unbounded blocking write.
# The read side of _transact has always had a deadline, but on the incident
# below it is the write that is exposed: a USB CDC port whose device has
# stopped answering can leave the driver's TX buffer full, and a write() into
# it with no timeout never returns at all. That is a hang with the
# transmitter keyed, which is strictly worse than a lost frame -- a raised
# exception can at least be retried and fallen back from, a blocked thread
# cannot.
#
# 0.25s is deliberately shorter than the 0.3s default reply timeout it
# precedes, so one dead transaction still costs about one self.timeout rather
# than two. A CI-V frame is 6 bytes: ~0.5ms on the wire at 115200 and ~6ms at
# the slowest baud discover_icom_civ tries, so anything past a few tens of ms
# is the port misbehaving rather than the frame being long.
WRITE_TIMEOUT = 0.25

# -- un-keying ---------------------------------------------------------
#
# The two constants below, and key()'s whole shape, exist because of one
# bench incident, and neither reads as proportionate without it.
#
# RF from a high-power transmission desensed the USB bus *mid-transmission*,
# and three things failed at once: the WASAPI OutputStream raised PaErrorCode
# -9996 ("Invalid device"); the CI-V un-key running in the resulting
# `finally` got no reply and raised TimeoutError *out of that finally*; and
# the radio's CI-V stayed unresponsive on both its COM ports, at every baud,
# afterwards. Net result: a radio commanded to transmit, an un-key nobody
# ever confirmed, and no code path left that would try again. On high power
# that is a stuck transmitter, which is the worst failure this codebase has.
#
# The asymmetry that follows, and which key() implements: keying *on* may
# raise, because the caller will stop and its `finally` still runs an un-key.
# Keying *off* may not, because at that point this function is the only thing
# left that can put the transmitter down.
#
# How many full request/reply transactions key(False) spends before it stops
# expecting an answer. Each costs up to self.timeout when the radio is
# silent, so at the 0.3s default this is ~0.9s of possibly-still-keyed
# transmitter -- against which every extra attempt is another chance that the
# bus recovered and the un-key comes back *confirmed* rather than hopeful.
#
# Not a measured figure, and it should not pretend to be: the bus whose
# recovery time would set it is the one that stopped answering, and
# reproducing the failure means deliberately transmitting high power into a
# desensed USB bus. Three is chosen against afsk.MAX_KEYING_SECONDS (3s) --
# the retries should not outlast the transmission they are ending.
UNKEY_ATTEMPTS = 3

# Blind transmit-off frames written once the acked attempts are exhausted:
# written and never read back, so they cost the ~6 bytes on the wire and not
# a timeout each.
#
# This is the point of the whole change. An un-key that cannot be *confirmed*
# is still very much worth *sending*: a half-open USB pipe drops replies
# while carrying commands perfectly well, and in that case a blind frame
# stops the transmitter and nobody ever finds out it worked. It costs
# nothing, and it is strictly better than giving up -- which, before this,
# was exactly what happened.
#
# Three rather than one because the failure is intermittent by nature: the
# bus is being desensed by our own RF, which stops the instant one of these
# frames lands and the radio drops carrier.
UNKEY_BLIND_WRITES = 3

# Reply to a "read transceiver ID" query: FE FE <controller> <radio> 19 00 <radio> FD
_ID_REPLY_RE = re.compile(rb"\xFE\xFE" + bytes([CONTROLLER_ADDR]) + rb"(.)\x19\x00\1\xFD", re.DOTALL)
# Generic CI-V OK / NG replies: FE FE <controller> <radio> FB/FA FD
_OK_RE = re.compile(rb"\xFE\xFE" + bytes([CONTROLLER_ADDR]) + rb".\xFB\xFD", re.DOTALL)
_NG_RE = re.compile(rb"\xFE\xFE" + bytes([CONTROLLER_ADDR]) + rb".\xFA\xFD", re.DOTALL)


def _open_quiet(port, baud, timeout=None, write_timeout=WRITE_TIMEOUT):
    """Opens a serial port with RTS/DTR forced low before the lines can assert.

    pyserial defaults both control lines high, and some radios/interfaces
    treat RTS (or DTR) as a hardware PTT line -- so opening a port normally
    can key a transmitter before a single protocol byte is sent, independent
    of whatever commands follow. Build the port closed, force the lines down,
    then open.

    write_timeout is set for the same class of reason: pyserial's default is
    an unbounded blocking write, and the one write that must never block
    forever is the one that un-keys. See WRITE_TIMEOUT.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    if timeout is not None:
        ser.timeout = timeout
    ser.write_timeout = write_timeout
    ser.rts = False
    ser.dtr = False
    ser.open()
    return ser


class IcomCivPtt:
    """Keys PTT on an Icom radio over CI-V.

    key_state_unknown is the flag a caller must consult before doing anything
    that assumes the transmitter is down -- notably before keying again. It is
    set whenever we have commanded a state the radio did not acknowledge, and
    cleared whenever the radio answers at all (OK or NG: a radio that is
    talking is a radio whose state we know). transport.RadioTransport.send()
    reads it to decide whether its retry loop may key a second time, because
    keying on top of an unconfirmed key state is how one failed transmission
    becomes a transmitter nobody can account for.
    """

    def __init__(self, port, baud=115200, radio_addr=IC705_DEFAULT_ADDR, timeout=0.3):
        self.radio_addr = radio_addr
        self.timeout = timeout
        # key() is reachable from the TX thread and from close() on another
        # (RadioTransport.close(), a KeyboardInterrupt path, an atexit hook).
        # Two CI-V frames interleaved on the wire are not two commands, they
        # are one frame the radio cannot parse -- and the one that would be
        # lost is whichever arrived second, which is exactly the un-key.
        # Re-entrant because close() -> key() nests.
        self._lock = threading.RLock()
        self.key_state_unknown = False
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
        """Sends transmit ON/OFF. Returns True when the radio acknowledged it.

        The two directions are deliberately not symmetric; see the un-keying
        notes at the top of this module. key(True) raises when the radio stays
        silent, key(False) never raises at all.
        """
        with self._lock:
            return self._key_on() if on else self._key_off()

    def _key_on(self) -> bool:
        """Keys the transmitter. Raises TimeoutError if the radio stays silent.

        Raising is the safe direction here, but only because the caller treats
        it correctly: an unanswered ON is not a radio that stayed receiving,
        it is a radio whose reply we did not hear, and it may perfectly well
        be transmitting. So the caller must still un-key afterwards -- which
        is why audio_io.transmit() now keys *inside* its try block rather than
        in front of it, where a raise here skipped the un-key entirely.
        """
        reply = self._transact(PTT_ON)
        if _OK_RE.search(reply):
            self.key_state_unknown = False
            return True
        if _NG_RE.search(reply):
            # The radio answered and declined (already transmitting, or in a
            # state that will not key). It is talking to us, so the state is
            # known -- just not the one that was asked for.
            self.key_state_unknown = False
            return False
        self.key_state_unknown = True
        raise TimeoutError(f"no CI-V reply to PTT ON (got {reply!r})")

    def _key_off(self) -> bool:
        """Un-keys the transmitter. Never raises, whatever the port does.

        Returns True only when the radio acknowledged with OK. False means the
        transmitter may still be up, and key_state_unknown is set to say so;
        it is not a quiet "nothing happened" and callers must not read it as
        one. unkey() below is the wrapper that shouts about it.

        Order of escalation, cheapest first: retry the acked transaction
        UNKEY_ATTEMPTS times, then write UNKEY_BLIND_WRITES transmit-off
        frames with no reply expected.
        """
        for attempt in range(1, UNKEY_ATTEMPTS + 1):
            try:
                reply = self._transact(PTT_OFF)
            except Exception as exc:
                # A dead port raises out of reset_input_buffer() or write()
                # before any deadline is reached, so this is the *fast*
                # failure. Keep going: the next attempt may find the bus
                # back, and the blind writes below are still worth trying.
                _log.warning("PTT OFF attempt %d/%d failed on the port itself: %s",
                             attempt, UNKEY_ATTEMPTS, exc)
                continue
            if _OK_RE.search(reply):
                self.key_state_unknown = False
                return True
            if _NG_RE.search(reply):
                # Worse than silence: the radio is answering and refusing to
                # stop transmitting. This used to be returned as a plain
                # False and discarded by every caller. Retry it, then fall
                # through to the blind writes -- there is nothing else to try
                # from here, and a refused un-key is a keyed transmitter.
                _log.error("radio REFUSED PTT OFF (CI-V NG), attempt %d/%d",
                           attempt, UNKEY_ATTEMPTS)
                continue
            _log.warning("no CI-V reply to PTT OFF, attempt %d/%d (got %r)",
                         attempt, UNKEY_ATTEMPTS, reply)

        self._blind_unkey()
        self.key_state_unknown = True
        return False

    def _blind_unkey(self):
        """Writes transmit-off frames with no reply expected. Never raises.

        Two further steps were considered here and deliberately not taken,
        because both spend seconds of a possibly-keyed transmitter to buy
        nothing:

        Reopening the port. In the incident this exists for, the radio was
        unresponsive on *both* its COM ports at every baud afterwards, so a
        reopen would have discovered that slowly rather than not at all.

        Closing the port. That is the right last resort for LinePtt, where the
        control line falls with the port and the transmitter drops with it --
        see LinePtt.key. It does nothing for CI-V: an Icom keyed by command
        stays keyed with its serial port shut, so closing here would only
        destroy the one channel that might yet deliver an un-key.
        """
        frame = (CIV_FRAME_START + bytes([self.radio_addr, CONTROLLER_ADDR])
                 + PTT_OFF + CIV_FRAME_END)
        sent = 0
        for _ in range(UNKEY_BLIND_WRITES):
            try:
                self.ser.write(frame)
                sent += 1
            except Exception as exc:
                _log.error("blind PTT OFF write failed: %s", exc)
        _log.error("PTT OFF was never acknowledged; %d/%d blind transmit-off frame(s) "
                   "written. THE TRANSMITTER MAY STILL BE KEYED -- check the radio.",
                   sent, UNKEY_BLIND_WRITES)

    def close(self):
        """Un-keys, then closes the port. The port closes either way.

        key(False) no longer raises, so the try/finally is belt and braces --
        but it is the belt and braces around the last un-key a process ever
        performs, which is not the place to lean on an invariant holding.
        """
        try:
            self.key(False)
        finally:
            try:
                self.ser.close()
            except Exception as exc:
                _log.warning("closing the CI-V port failed: %s", exc)


def unkey(ptt) -> bool:
    """Drops PTT on `ptt` and never raises. True if the hardware confirmed it.

    This is what belongs in a `finally`. An exception raised *inside* a finally
    replaces whatever was already propagating and, worse, skips whatever else
    that finally would have done -- so `finally: ptt.key(False)` is exactly one
    TimeoutError away from being `finally: pass`. That is how the bench
    incident described above turned a failed transmission into an
    unaccounted-for key state, and it is why audio_io's transmit paths call
    this rather than key(False) directly.

    Works with any PTT object, including ones that predate key_state_unknown
    and the fakes in the tests, so the flag is set defensively.
    """
    try:
        confirmed = bool(ptt.key(False))
    except Exception as exc:
        _log.error("UN-KEY FAILED (%s: %s). THE TRANSMITTER MAY STILL BE KEYED.",
                   type(exc).__name__, exc)
        try:
            ptt.key_state_unknown = True
        except Exception:
            pass
        return False
    if not confirmed:
        _log.error("un-key was not confirmed by the radio; "
                   "THE TRANSMITTER MAY STILL BE KEYED.")
    return confirmed


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
    """Keys PTT by toggling a serial control line (RTS or DTR), e.g. via a Digirig.

    Same key_state_unknown contract as IcomCivPtt, with the caveat that a
    control line has no acknowledgement: "confirmed" here means only that
    pyserial accepted the write, not that a transistor moved.
    """

    def __init__(self, port, line="rts", baud=9600, active_high=True):
        if line not in ("rts", "dtr"):
            raise ValueError("line must be 'rts' or 'dtr'")
        self.line = line
        self.active_high = active_high
        self.key_state_unknown = False
        self.ser = _open_quiet(port, baud)

    def _set_line(self, level):
        if self.line == "rts":
            self.ser.rts = level
        else:
            self.ser.dtr = level

    def key(self, on: bool) -> bool:
        """Sets the PTT line. Returns True if it was set; never raises on OFF.

        Asymmetric for the same reason IcomCivPtt.key is -- see the un-keying
        notes at the top of this module. A USB serial bridge can be knocked
        off the bus by RF exactly like a radio's CDC port can, and setting a
        control line on a vanished port raises rather than timing out.
        """
        level = on if self.active_high else not on
        if on:
            try:
                self._set_line(level)
            except Exception:
                # We do not know whether the line moved before the failure,
                # so the transmitter may be up. Say so, then let it raise:
                # the caller stops and un-keys, which is what we want.
                self.key_state_unknown = True
                raise
            self.key_state_unknown = False
            return True

        for attempt in range(1, UNKEY_ATTEMPTS + 1):
            try:
                self._set_line(level)
                self.key_state_unknown = False
                return True
            except Exception as exc:
                _log.warning("dropping %s failed, attempt %d/%d: %s",
                             self.line.upper(), attempt, UNKEY_ATTEMPTS, exc)

        # Last resort, and it is a good one on this kind of interface: closing
        # the port de-asserts RTS/DTR, and a Digirig-style keying transistor
        # driven from an asserted line is therefore released by the port going
        # away -- including by the USB device disappearing entirely, which is
        # the failure being defended against. Nothing is left to key with
        # afterwards, but a transmitter that is off and a modem that needs
        # restarting beats the alternative.
        #
        # Only valid active-high: with active_high=False the *keyed* state is
        # the de-asserted line, so closing the port would key the transmitter
        # rather than release it. Nothing on this bench is wired that way, but
        # the flag exists, so the fallback has to check it rather than assume.
        if self.active_high:
            try:
                self.ser.close()
                _log.error("could not drop %s; closed the port instead, which "
                           "de-asserts it. PTT should be released, but this radio "
                           "cannot be keyed again without reopening the port.",
                           self.line.upper())
                self.key_state_unknown = True
                return False
            except Exception as exc:
                _log.error("could not drop %s and could not close the port either: %s",
                           self.line.upper(), exc)
        else:
            _log.error("could not drop %s on an active-low interface; closing the "
                       "port would ASSERT PTT, so it is not attempted.",
                       self.line.upper())
        _log.error("THE TRANSMITTER MAY STILL BE KEYED -- check the radio.")
        self.key_state_unknown = True
        return False

    def close(self):
        """Un-keys, then closes the port. The port closes either way.

        key(False) may already have closed it as its own last resort; pyserial
        treats close() on a closed port as a no-op, so that is harmless.
        """
        try:
            self.key(False)
        finally:
            try:
                self.ser.close()
            except Exception as exc:
                _log.warning("closing the PTT port failed: %s", exc)
