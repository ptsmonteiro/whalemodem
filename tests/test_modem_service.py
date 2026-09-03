import queue
import threading
import time

from whale import vara_server
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

    def write(self, data):
        self.calls.append(("write", data))


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


def test_vara_adapter_acks_accepted_commands_with_ok():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)

    server._handle_command("MYCALL NEW1")
    server._handle_command("LISTEN ON")
    server._handle_command("CONNECT NEW1 STA2")
    server._handle_command("ABORT")

    # LISTEN is not acked with OK (unconfirmed in the real-VARA capture);
    # every other accepted command here is.
    assert sent == ["OK", "OK", "OK"]


def test_vara_adapter_abort_sends_ok_before_disconnect_completes():
    service = RecordingService()
    order = []
    service.disconnect = lambda: order.append("disconnect")
    server = StationServer(service, "STA1", 8300, 8301)
    server._send_status = lambda line: order.append(line)

    server._handle_command("ABORT")

    assert order == ["OK", "disconnect"]


def test_vara_adapter_chat_and_bandwidth_commands():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)

    server._handle_command("CHAT ON")
    assert server.chat_mode is True
    server._handle_command("CHAT OFF")
    assert server.chat_mode is False
    server._handle_command("BW2300")
    assert server.bandwidth_hz == 2300

    assert sent == ["OK", "OK", "OK"]


def test_vara_adapter_connected_status_has_mycall_peer_bandwidth():
    # No BW<n> sent: bandwidth falls back to 0.
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)

    server._on_modem_event("CONNECTED", peer="STA2", mycall="NEW1")

    assert sent == ["CONNECTED NEW1 STA2 0"]


def test_vara_adapter_connected_status_uses_bw_command_value():
    # BW<n> sent first: its value is reported as the CONNECTED bandwidth.
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    server._handle_command("BW2300")
    sent = []
    server._send_status = lambda line: sent.append(line)

    server._on_modem_event("CONNECTED", peer="STA2", mycall="NEW1")

    assert sent == ["CONNECTED NEW1 STA2 2300"]


def test_vara_adapter_sends_buffer_after_data_port_write():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)

    class FakeConn:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def recv(self, n):
            if self._chunks:
                return self._chunks.pop(0)
            return b""

    server._data_reader_loop(FakeConn([b"hello", b"world"]))

    assert service.calls == [("write", b"hello"), ("write", b"world")]
    assert sent == ["BUFFER 0", "BUFFER 0"]


def test_vara_adapter_sends_iamalive_periodically_without_connecting(monkeypatch):
    monkeypatch.setattr(vara_server, "IAMALIVE_INTERVAL", 0.02)
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)
    # Simulate an active command connection without opening a real socket.
    server._cmd_conn = object()

    thread = threading.Thread(target=server._iamalive_loop, daemon=True)
    thread.start()
    try:
        wait_until(lambda: sent.count("IAMALIVE") >= 3, timeout=1.0)
    finally:
        server._stopping.set()
        thread.join(timeout=1.0)

    assert not thread.is_alive()


def test_vara_adapter_iamalive_skips_send_without_cmd_connection(monkeypatch):
    monkeypatch.setattr(vara_server, "IAMALIVE_INTERVAL", 0.02)
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)
    assert server._cmd_conn is None

    thread = threading.Thread(target=server._iamalive_loop, daemon=True)
    thread.start()
    time.sleep(0.1)
    server._stopping.set()
    thread.join(timeout=1.0)

    assert sent == []


def test_vara_adapter_iamalive_loop_stops_promptly(monkeypatch):
    monkeypatch.setattr(vara_server, "IAMALIVE_INTERVAL", 60.0)
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)

    thread = threading.Thread(target=server._iamalive_loop, daemon=True)
    thread.start()
    time.sleep(0.05)
    start = time.monotonic()
    server._stopping.set()
    thread.join(timeout=1.0)
    elapsed = time.monotonic() - start

    assert not thread.is_alive()
    assert elapsed < 1.0
