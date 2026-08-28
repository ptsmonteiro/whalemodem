import queue
import time

from whale.service import ModemService
from whale.vara_server import StationServer


class FakeLink:
    def __init__(self):
        self.mycall = "STA1"
        self.state = "IDLE"
        self.on_event = lambda name, **details: None
        self.sent = []
        self.received = queue.Queue()
        self.calls = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def connect(self, destination):
        self.calls.append(("connect", destination))
        self.state = "CONNECTED"
        self.on_event("CONNECTED", peer=destination, mycall=self.mycall)
        return True

    def listen_once(self, timeout=None):
        time.sleep(min(timeout or 0, 0.01))
        return None

    def send_message(self, data):
        self.sent.append(data)

    def recv_message(self, timeout=None):
        try:
            return self.received.get(timeout=min(timeout or 0, 0.01))
        except queue.Empty:
            return None

    def service_while_idle(self):
        return self.state == "CONNECTED"

    def disconnect(self, retries=3):
        if self.state == "CONNECTED":
            self.state = "IDLE"
            self.on_event("DISCONNECTED")


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


def test_service_owns_link_worker_and_stream_conversion():
    link = FakeLink()
    service = ModemService(link, poll_interval=0.01)
    events = []
    service.subscribe(lambda name, **details: events.append((name, details)))
    service.start()
    try:
        service.connect("STA2", mycall="NEW1")
        wait_until(lambda: service.state == "CONNECTED")
        service.write(b"one")
        service.write(b"two")
        wait_until(lambda: bool(link.sent))
        assert b"".join(link.sent) == b"onetwo"

        link.received.put(b"reply")
        assert service.read(timeout=1) == b"reply"
        assert events[0] == ("CONNECTED", {"peer": "STA2", "mycall": "NEW1"})

        service.disconnect()
        wait_until(lambda: service.state == "IDLE")
        assert events[-1][0] == "DISCONNECTED"
    finally:
        service.stop()
    assert link.calls[0] == "start"
    assert link.calls[-1] == "stop"


class RecordingService:
    state = "IDLE"

    def __init__(self):
        self.calls = []

    def subscribe(self, handler):
        self.handler = handler

    def set_callsign(self, callsign):
        self.calls.append(("callsign", callsign))

    def listen(self, enabled=True):
        self.calls.append(("listen", enabled))

    def connect(self, destination, *, mycall=None):
        self.calls.append(("connect", destination, mycall))

    def disconnect(self):
        self.calls.append(("disconnect",))


def test_vara_adapter_only_translates_commands_to_service_calls():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)

    server._handle_command("MYCALL NEW1")
    server._handle_command("LISTEN ON")
    server._handle_command("LISTEN OFF")
    server._handle_command("CONNECT NEW1 STA2")
    server._handle_command("ABORT")

    assert service.calls == [
        ("callsign", "NEW1"),
        ("listen", True),
        ("listen", False),
        ("connect", "STA2", "NEW1"),
        ("disconnect",),
    ]
