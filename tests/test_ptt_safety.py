"""What happens to the transmitter when the bus stops answering.

Run: python tests/test_ptt_safety.py

Every test here is about one bench incident. RF from a high-power
transmission desensed the USB bus *mid-transmission*, and three things failed
together: the WASAPI OutputStream raised PaErrorCode -9996 ("Invalid
device"); the CI-V un-key in the resulting `finally` got no reply and raised
TimeoutError out of that finally; and the radio's CI-V stayed unresponsive on
both COM ports at every baud afterwards. The transmitter had been commanded
on, the un-key was never confirmed, and no code path was left that would try
again.

Nothing here touches a radio, and nothing here may be allowed to start doing
so: the fakes below stand in for the serial port and for the PTT object, and
audio_io's OutputStream is replaced by a stub that plays into an array. A
test that needs the bench to be plugged in is a test that never gets run
after the failure that made it necessary -- which is the situation this
suite was written in, with the IC-705's CI-V still unresponsive.

The distinction that runs through all of it: keying ON may raise, because the
caller stops and its finally still un-keys. Keying OFF may not, because at
that point it is the only thing that can put the transmitter down.
"""

import threading
import types

import numpy as np
import serial
import sounddevice as sd

from whale import transport
from whale.hw import audio_io
from whale.hw import ptt


# -- fakes -------------------------------------------------------------


def _reply(kind, radio_addr):
    """A CI-V reply frame from the radio to the controller: OK (FB) or NG (FA)."""
    code = {"ok": 0xFB, "ng": 0xFA}[kind]
    return ptt.CIV_FRAME_START + bytes([ptt.CONTROLLER_ADDR, radio_addr, code]) + ptt.CIV_FRAME_END


def _command(cmd, radio_addr):
    """A CI-V command frame as IcomCivPtt writes it."""
    return (ptt.CIV_FRAME_START + bytes([radio_addr, ptt.CONTROLLER_ADDR])
            + cmd + ptt.CIV_FRAME_END)


class FakeSerial:
    """Stand-in for serial.Serial, scripted one answer per write.

    `answers` is consumed left to right; each entry is "ok", "ng", "silent",
    or "raise" (the port itself failing, which is what a USB CDC device that
    has left the bus actually does -- it raises from write(), it does not
    politely time out). The last entry repeats forever, so a test says
    ["silent"] rather than counting how many attempts the code under test
    will make.
    """

    def __init__(self, answers=("ok",), radio_addr=ptt.IC705_DEFAULT_ADDR):
        self.answers = list(answers)
        self.radio_addr = radio_addr
        self.writes = []
        self.closed = False
        self.rts = None
        self.dtr = None
        self.timeout = None
        self.write_timeout = None
        self._pending = b""

    def _next_answer(self):
        return self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)

    def reset_input_buffer(self):
        self._pending = b""

    def write(self, data):
        answer = self._next_answer()
        if answer == "raise" or self.closed:
            raise serial.SerialException("fake port is not there")
        self.writes.append(bytes(data))
        self._pending = b"" if answer == "silent" else _reply(answer, self.radio_addr)
        return len(data)

    def read_until(self, terminator):
        out, self._pending = self._pending, b""
        return out

    def close(self):
        self.closed = True


class FakePtt:
    """Stand-in for a PTT object. Records every key() call in order."""

    def __init__(self, on_raises=False, off_raises=False, off_confirms=True):
        self.on_raises = on_raises
        self.off_raises = off_raises
        self.off_confirms = off_confirms
        self.calls = []
        self.key_state_unknown = False

    def key(self, on):
        self.calls.append(on)
        if on and self.on_raises:
            self.key_state_unknown = True
            raise TimeoutError("no CI-V reply to PTT ON")
        if not on and self.off_raises:
            raise TimeoutError("no CI-V reply to PTT OFF")
        if not on and not self.off_confirms:
            self.key_state_unknown = True
            return False
        self.key_state_unknown = False
        return True

    def close(self):
        self.key(False)


class FakeOutputStream:
    """Stand-in for sd.OutputStream: drains the whole signal on __enter__.

    `fail` makes the stream raise PortAudioError the way a device that left
    the bus does. -9996 is the code the bench actually produced.
    """

    fail = False
    opened_devices = []

    def __init__(self, device=None, callback=None, **kwargs):
        FakeOutputStream.opened_devices.append(device)
        if FakeOutputStream.fail:
            raise sd.PortAudioError("Invalid device [PaErrorCode -9996]")
        self.device = device
        self.callback = callback
        self.latency = 0.0

    def __enter__(self):
        status = types.SimpleNamespace(output_underflow=False, input_overflow=False)
        # One oversized block, so the callback hits its short-chunk branch and
        # sets the "signal queued" event exactly as it does on hardware.
        outdata = np.zeros((4096, 1), dtype=np.float32)
        self.callback(outdata, 4096, None, status)
        return self

    def __exit__(self, *exc):
        return False

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


class _Patch:
    """Minimal setattr/restore, so the tests can run under plain python too."""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self.value

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.old)
        return False


def _icom(fake):
    """An IcomCivPtt wired to `fake` instead of a real port."""
    with _Patch(ptt, "_open_quiet", lambda *a, **k: fake):
        # A short reply timeout keeps a silent-radio test fast; the fake
        # returns an empty read immediately anyway, which is what breaks
        # _transact's loop, so this only bounds the pathological case.
        return ptt.IcomCivPtt("COM_FAKE", 115200, fake.radio_addr, timeout=0.05)


def _line(fake, active_high=True, line="rts"):
    with _Patch(ptt, "_open_quiet", lambda *a, **k: fake):
        return ptt.LinePtt("COM_FAKE", line=line, active_high=active_high)


def _fake_transport(ptt_obj, out_device=7):
    """A RadioTransport with no hardware behind it.

    Built field by field rather than through __init__, which opens a sound
    card and a serial port. Only the transmit path is exercised here.
    """
    t = object.__new__(transport.RadioTransport)
    t.radio = types.SimpleNamespace(name="fake", audio_name="Fake Audio CODEC")
    t.out_device = out_device
    t.in_device = None
    t.ptt = ptt_obj
    t._chunks = __import__("collections").deque()
    t._chunks_len = 0
    t._buf_lock = threading.Lock()
    t._stream = None
    t._tx_lock = threading.Lock()
    t._transmitting = threading.Event()
    return t


# -- un-keying over CI-V -----------------------------------------------


def test_unkey_retries_until_the_radio_answers():
    fake = FakeSerial(["silent", "silent", "ok"])
    radio = _icom(fake)
    assert radio.key(False) is True
    assert len(fake.writes) == 3, fake.writes
    assert radio.key_state_unknown is False
    print("test_unkey_retries_until_the_radio_answers OK")


def test_unkey_falls_back_to_a_blind_write_when_nothing_answers():
    """The whole point: a silent bus gets the transmit-off frame anyway.

    A half-open USB pipe drops replies while still carrying commands, so the
    blind frames may well be what stops the transmitter -- and nobody will
    ever find out that they did. Before this, key(False) raised here and the
    radio kept transmitting.
    """
    fake = FakeSerial(["silent"])
    radio = _icom(fake)
    assert radio.key(False) is False           # unconfirmed, and says so
    assert radio.key_state_unknown is True
    assert len(fake.writes) == ptt.UNKEY_ATTEMPTS + ptt.UNKEY_BLIND_WRITES
    off = _command(ptt.PTT_OFF, fake.radio_addr)
    assert all(w == off for w in fake.writes), fake.writes
    print("test_unkey_falls_back_to_a_blind_write_when_nothing_answers OK")


def test_a_refused_unkey_is_treated_as_a_failure_not_as_an_answer():
    """CI-V NG to transmit-off means the radio is declining to stop.

    That used to be returned as a bare False and discarded by every caller,
    which is the quietest possible way to report a stuck transmitter.
    """
    fake = FakeSerial(["ng"])
    radio = _icom(fake)
    assert radio.key(False) is False
    assert radio.key_state_unknown is True
    assert len(fake.writes) == ptt.UNKEY_ATTEMPTS + ptt.UNKEY_BLIND_WRITES
    print("test_a_refused_unkey_is_treated_as_a_failure_not_as_an_answer OK")


def test_unkey_survives_a_port_that_raises():
    """A vanished USB device raises from write(); it does not time out."""
    fake = FakeSerial(["raise"])
    radio = _icom(fake)
    assert radio.key(False) is False           # no exception escapes
    assert radio.key_state_unknown is True
    assert fake.writes == []                   # nothing got through, but we tried
    print("test_unkey_survives_a_port_that_raises OK")


def test_key_on_still_raises_when_the_radio_is_silent():
    """The asymmetry, asserted. An unconfirmed key-on must stop the caller."""
    fake = FakeSerial(["silent"])
    radio = _icom(fake)
    raised = False
    try:
        radio.key(True)
    except TimeoutError:
        raised = True
    assert raised
    assert radio.key_state_unknown is True
    # One attempt only: retrying a key-on would be more keying, not less.
    assert len(fake.writes) == 1
    print("test_key_on_still_raises_when_the_radio_is_silent OK")


def test_a_radio_that_answers_clears_the_unknown_flag():
    """Even NG clears it: a radio that is talking is a radio we can read."""
    fake = FakeSerial(["silent"])
    radio = _icom(fake)
    radio.key(False)
    assert radio.key_state_unknown is True
    fake.answers = ["ng"]
    assert radio.key(True) is False
    assert radio.key_state_unknown is False
    fake.answers = ["ok"]
    assert radio.key(False) is True
    assert radio.key_state_unknown is False
    print("test_a_radio_that_answers_clears_the_unknown_flag OK")


def test_close_closes_the_port_even_when_the_unkey_fails():
    fake = FakeSerial(["raise"])
    radio = _icom(fake)
    radio.close()
    assert fake.closed
    print("test_close_closes_the_port_even_when_the_unkey_fails OK")


def test_the_unkey_helper_never_raises():
    """ptt.unkey() is what goes in a finally, so it has to swallow anything."""
    broken = FakePtt(off_raises=True)
    assert ptt.unkey(broken) is False
    assert broken.calls == [False]
    assert broken.key_state_unknown is True    # flagged even though key() blew up
    good = FakePtt()
    assert ptt.unkey(good) is True
    print("test_the_unkey_helper_never_raises OK")


def test_open_quiet_bounds_the_write_that_unkeys():
    """pyserial's default write_timeout is None: an unbounded blocking write.

    A blocked write on a wedged port is a hang with the transmitter keyed,
    which no retry or fallback can reach. Regression guard, because the
    default is invisible until the day it matters.
    """
    class RecordingPort:
        def __init__(self):
            self.opened = False

        def open(self):
            self.opened = True

    with _Patch(serial, "Serial", RecordingPort):
        port = ptt._open_quiet("COM_FAKE", 115200, timeout=0.3)
    assert port.write_timeout == ptt.WRITE_TIMEOUT
    assert port.rts is False and port.dtr is False   # still opened quiet
    assert port.opened
    print("test_open_quiet_bounds_the_write_that_unkeys OK")


# -- un-keying on a control line ---------------------------------------


class BrokenLineSerial(FakeSerial):
    """A control-line port whose RTS/DTR setter fails, as a gone USB bridge does."""

    def __setattr__(self, name, value):
        if name in ("rts", "dtr") and getattr(self, "_live", False):
            raise serial.SerialException("fake bridge is not there")
        object.__setattr__(self, name, value)


def test_line_ptt_unkey_falls_back_to_closing_the_port():
    """Closing the port de-asserts the line, which releases the transistor."""
    fake = BrokenLineSerial()
    pttobj = _line(fake)
    fake._live = True                       # break it only after construction
    assert pttobj.key(False) is False
    assert fake.closed                      # the fallback that actually un-keys
    assert pttobj.key_state_unknown is True
    print("test_line_ptt_unkey_falls_back_to_closing_the_port OK")


def test_line_ptt_does_not_close_an_active_low_port():
    """On an active-low interface, closing the port would KEY the radio."""
    fake = BrokenLineSerial()
    pttobj = _line(fake, active_high=False)
    fake._live = True
    assert pttobj.key(False) is False
    assert not fake.closed
    assert pttobj.key_state_unknown is True
    print("test_line_ptt_does_not_close_an_active_low_port OK")


def test_line_ptt_key_on_raises_and_flags_the_state():
    fake = BrokenLineSerial()
    pttobj = _line(fake)
    fake._live = True
    raised = False
    try:
        pttobj.key(True)
    except serial.SerialException:
        raised = True
    assert raised
    assert pttobj.key_state_unknown is True
    print("test_line_ptt_key_on_raises_and_flags_the_state OK")


# -- audio_io.transmit() -----------------------------------------------


def _transmit(pttobj, fail_stream=False, device=3):
    FakeOutputStream.fail = fail_stream
    FakeOutputStream.opened_devices = []
    signal = np.zeros(480, dtype=np.float32)
    with _Patch(sd, "OutputStream", FakeOutputStream):
        try:
            return audio_io.transmit(signal, device, pttobj, ptt_lead=0.0, ptt_tail=0.0), None
        except Exception as exc:
            return None, exc
        finally:
            FakeOutputStream.fail = False


def test_transmit_unkeys_after_a_failed_key_on():
    """key(True) used to sit in front of the try, so a raise skipped the un-key.

    An unanswered key-on is exactly the case where the radio may be keyed:
    the command reached it and only the reply was lost.
    """
    pttobj = FakePtt(on_raises=True)
    result, exc = _transmit(pttobj)
    assert isinstance(exc, TimeoutError), exc
    assert pttobj.calls == [True, False], pttobj.calls
    print("test_transmit_unkeys_after_a_failed_key_on OK")


def test_transmit_unkeys_when_the_stream_dies():
    pttobj = FakePtt()
    result, exc = _transmit(pttobj, fail_stream=True)
    assert isinstance(exc, sd.PortAudioError), exc
    assert pttobj.calls == [True, False], pttobj.calls
    print("test_transmit_unkeys_when_the_stream_dies OK")


def test_a_failing_unkey_does_not_replace_the_exception_in_flight():
    """The incident, reproduced whole.

    The stream fails with -9996, and the un-key in the finally then fails
    too. What used to come out was the TimeoutError from the finally -- the
    -9996 was lost, and with it the only clue about what had actually
    happened. What must come out is the PortAudioError, with the un-key
    failure reported separately by ptt.unkey().
    """
    pttobj = FakePtt(off_raises=True)
    result, exc = _transmit(pttobj, fail_stream=True)
    assert isinstance(exc, sd.PortAudioError), exc
    assert pttobj.calls == [True, False], pttobj.calls
    print("test_a_failing_unkey_does_not_replace_the_exception_in_flight OK")


def test_transmit_reaches_the_unkey_on_the_happy_path():
    pttobj = FakePtt()
    keyed_seconds, exc = _transmit(pttobj)
    assert exc is None, exc
    assert keyed_seconds is not None and keyed_seconds >= 0.0
    assert pttobj.calls == [True, False]
    print("test_transmit_reaches_the_unkey_on_the_happy_path OK")


# -- transport.RadioTransport.send() -----------------------------------


def test_send_does_not_rekey_on_an_unconfirmed_key_state():
    """The retry loop's instinct is to key again; over a bus that just
    stopped answering, that stacks a second transmission on a state nobody
    can read."""
    attempts = []

    def failing_transmit(audio, device, pttobj, **kwargs):
        attempts.append(device)
        pttobj.key(True)
        pttobj.key(False)                      # un-key attempted, not confirmed
        raise sd.PortAudioError("Invalid device [PaErrorCode -9996]")

    pttobj = FakePtt(off_confirms=False)
    t = _fake_transport(pttobj)
    raised = None
    with _Patch(transport, "_ensure_com_initialized", lambda: None), \
            _Patch(audio_io, "transmit", failing_transmit):
        try:
            t.send(np.zeros(480, dtype=np.float32), retries=5)
        except sd.PortAudioError as exc:
            raised = exc
    assert raised is not None
    assert len(attempts) == 1, attempts
    print("test_send_does_not_rekey_on_an_unconfirmed_key_state OK")


def test_send_still_retries_when_the_key_state_is_known():
    """Ordinary WASAPI flakiness must still be retried; the guard above is
    about an unreadable radio, not about giving up on a fussy sound card."""
    attempts = []

    def flaky_transmit(audio, device, pttobj, **kwargs):
        attempts.append(device)
        pttobj.key(True)
        pttobj.key(False)                      # confirmed OFF: state is known
        if len(attempts) < 3:
            raise sd.PortAudioError("Unanticipated host error [PaErrorCode -9999]")
        return 1.23

    pttobj = FakePtt()
    t = _fake_transport(pttobj)
    with _Patch(transport, "_ensure_com_initialized", lambda: None), \
            _Patch(audio_io, "transmit", flaky_transmit), \
            _Patch(audio_io, "find_device", lambda *a: t.out_device):
        assert t.send(np.zeros(480, dtype=np.float32), retries=5) == 1.23
    assert len(attempts) == 3, attempts
    print("test_send_still_retries_when_the_key_state_is_known OK")


def test_send_reresolves_a_moved_output_device_between_attempts():
    """A card that left the bus can come back at a different index.

    Not the diagnosis for the observed incident -- the indices did not move
    there -- but a retry aimed at a stale index fails forever for a reason
    that has nothing to do with the radio.
    """
    attempts = []

    def flaky_transmit(audio, device, pttobj, **kwargs):
        attempts.append(device)
        pttobj.key(True)
        pttobj.key(False)
        if len(attempts) < 2:
            raise sd.PortAudioError("Invalid device [PaErrorCode -9996]")
        return 0.5

    pttobj = FakePtt()
    t = _fake_transport(pttobj, out_device=7)
    with _Patch(transport, "_ensure_com_initialized", lambda: None), \
            _Patch(audio_io, "transmit", flaky_transmit), \
            _Patch(audio_io, "find_device", lambda name, kind: 11):
        t.send(np.zeros(480, dtype=np.float32), retries=5)
    assert attempts == [7, 11], attempts
    assert t.out_device == 11
    print("test_send_reresolves_a_moved_output_device_between_attempts OK")


def test_reresolving_a_vanished_device_keeps_the_old_index():
    """find_device raises when the card is gone or newly ambiguous; the
    recovery path must not turn that into the exception the caller sees."""
    pttobj = FakePtt()
    t = _fake_transport(pttobj, out_device=7)

    def gone(name, kind):
        raise LookupError("no WASAPI output device matching 'Fake Audio CODEC'")

    with _Patch(audio_io, "find_device", gone):
        t._reresolve_out_device()
    assert t.out_device == 7
    print("test_reresolving_a_vanished_device_keeps_the_old_index OK")


def test_transport_close_survives_a_ptt_that_cannot_unkey():
    pttobj = FakePtt(off_raises=True)
    t = _fake_transport(pttobj)
    t.close()                                  # must not raise out of shutdown
    assert pttobj.calls == [False]
    print("test_transport_close_survives_a_ptt_that_cannot_unkey OK")


if __name__ == "__main__":
    test_unkey_retries_until_the_radio_answers()
    test_unkey_falls_back_to_a_blind_write_when_nothing_answers()
    test_a_refused_unkey_is_treated_as_a_failure_not_as_an_answer()
    test_unkey_survives_a_port_that_raises()
    test_key_on_still_raises_when_the_radio_is_silent()
    test_a_radio_that_answers_clears_the_unknown_flag()
    test_close_closes_the_port_even_when_the_unkey_fails()
    test_the_unkey_helper_never_raises()
    test_open_quiet_bounds_the_write_that_unkeys()
    test_line_ptt_unkey_falls_back_to_closing_the_port()
    test_line_ptt_does_not_close_an_active_low_port()
    test_line_ptt_key_on_raises_and_flags_the_state()
    test_transmit_unkeys_after_a_failed_key_on()
    test_transmit_unkeys_when_the_stream_dies()
    test_a_failing_unkey_does_not_replace_the_exception_in_flight()
    test_transmit_reaches_the_unkey_on_the_happy_path()
    test_send_does_not_rekey_on_an_unconfirmed_key_state()
    test_send_still_retries_when_the_key_state_is_known()
    test_send_reresolves_a_moved_output_device_between_attempts()
    test_reresolving_a_vanished_device_keeps_the_old_index()
    test_transport_close_survives_a_ptt_that_cannot_unkey()
    print("all PTT safety tests OK")
