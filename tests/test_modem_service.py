import queue
import socket
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


def test_service_propagates_failed_connect_event_and_returns_to_idle():
    class FailingLink(FakeLink):
        def connect(self, destination):
            self.calls.append(("connect", destination))
            self.state = "CONNECTING"
            self.state = "IDLE"
            self.on_event("CONNECT_FAILED")
            return False

    link = FailingLink()
    service = ModemService(link, poll_interval=0.01)
    events = []
    service.subscribe(lambda name, **details: events.append((name, details)))
    service.start()
    try:
        service.connect("MISSING", mycall="NEW1")
        wait_until(lambda: bool(events))

        assert link.calls[-1] == ("connect", "MISSING")
        assert link.mycall == "NEW1"
        assert service.state == "IDLE"
        assert events == [("CONNECT_FAILED", {})]
    finally:
        service.stop()


def test_service_disconnect_flushes_accepted_data_before_teardown():
    link = FakeLink()
    link.state = "CONNECTED"
    service = ModemService(link)
    service._outbound.put(b"one")
    service._outbound.put(b"two")
    service.disconnect()

    service._drain_commands()

    assert link.sent == [b"onetwo"]
    assert link.state == "IDLE"
    assert service._outbound.empty()


def test_service_abort_discards_accepted_data_before_teardown():
    link = FakeLink()
    link.state = "CONNECTED"
    service = ModemService(link)
    service._outbound.put(b"must not leak")
    service.abort()

    service._drain_commands()

    assert link.sent == []
    assert link.state == "IDLE"
    assert service._outbound.empty()


def test_service_teardown_keeps_received_bytes_readable():
    # Bytes already pulled off the air belong to the consumer, not to the
    # session: a DISCONNECT landing between the last inbound frame and the
    # reader's next poll must not truncate the transfer.
    for teardown in ("disconnect", "abort"):
        link = FakeLink()
        link.state = "CONNECTED"
        service = ModemService(link)
        service._inbound.put(b"tail")
        service._outbound.put(b"pending")
        getattr(service, teardown)()

        service._drain_commands()

        assert link.state == "IDLE"
        assert service.read(timeout=0) == b"tail"
        # ABORT still drops accepted outbound bytes: that asymmetry is about
        # what Whale refuses to transmit, not about what it already received.
        assert service._outbound.empty()


def test_service_drops_unread_bytes_when_an_outbound_session_starts():
    link = FakeLink()
    service = ModemService(link)
    service._inbound.put(b"previous session")
    service.connect("STA2")

    service._drain_commands()

    assert service.state == "CONNECTED"
    assert service.read(timeout=0) is None


def test_service_drops_unread_bytes_when_an_incoming_session_starts():
    class ArmedIncomingLink(FakeLink):
        def __init__(self):
            super().__init__()
            self.armed = threading.Event()

        def listen_once(self, timeout=None):
            if not self.armed.wait(min(timeout or 0, 0.01)):
                return None
            self.state = "CONNECTED"
            self.on_event("CONNECTED", mycall=self.mycall, peer="STA2")
            return "STA2"

    link = ArmedIncomingLink()
    service = ModemService(link, poll_interval=0.01)
    service.start()
    try:
        service._inbound.put(b"previous session")
        link.armed.set()
        wait_until(lambda: service.state == "CONNECTED")

        assert service.read(timeout=0.1) is None
    finally:
        service.stop()


class BlockingConn:
    """Data-port socket whose first ``sendall`` stalls until released."""

    def __init__(self):
        self.sent = []
        self.released = threading.Event()
        self.closed = False

    def sendall(self, data):
        self.sent.append(data)
        if len(self.sent) == 1:
            self.released.wait(5)

    def close(self):
        self.closed = True


def _stalled_writer(server, service, link):
    """Start a writer that is stuck sending ``b"head"`` with ``b"tail"`` queued."""
    conn = BlockingConn()
    server._data_conn = conn
    writer = threading.Thread(target=server._data_writer_loop, args=(conn,),
                              daemon=True)
    writer.start()
    link.received.put(b"head")
    link.received.put(b"tail")
    wait_until(lambda: conn.sent == [b"head"], timeout=5)
    wait_until(lambda: service._inbound.qsize() == 1, timeout=5)
    return conn, writer


def test_vara_adapter_delivers_inbound_bytes_queued_at_teardown():
    for teardown in ("DISCONNECT", "ABORT"):
        link = FakeLink()
        service = ModemService(link, poll_interval=0.01)
        server = StationServer(service, "STA1", 8300, 8301)
        server._send_status = lambda line: None
        service.start()
        try:
            service.connect("STA2", mycall="STA1")
            wait_until(lambda: service.state == "CONNECTED")
            conn, writer = _stalled_writer(server, service, link)

            server._handle_command(teardown)
            wait_until(lambda: service.state == "IDLE", timeout=5)
            conn.released.set()

            # The writer outlives the session (see the data-port lifetime
            # note), so the tail still reaches the client.
            wait_until(lambda: conn.sent == [b"head", b"tail"], timeout=5)
        finally:
            server._stopping.set()
            service.stop()


def test_vara_adapter_does_not_deliver_previous_session_bytes_to_the_next():
    for teardown in ("DISCONNECT", "ABORT"):
        link = FakeLink()
        service = ModemService(link, poll_interval=0.01)
        server = StationServer(service, "STA1", 8300, 8301)
        server._send_status = lambda line: None
        service.start()
        try:
            service.connect("STA2", mycall="STA1")
            wait_until(lambda: service.state == "CONNECTED")
            conn, writer = _stalled_writer(server, service, link)

            server._handle_command(teardown)
            wait_until(lambda: service.state == "IDLE", timeout=5)

            # The next session starts before the stalled writer ever asks for
            # the tail, so those bytes are dropped rather than delivered as
            # part of the new connection.
            service.connect("STA2", mycall="STA1")
            wait_until(lambda: service.state == "CONNECTED", timeout=5)
            conn.released.set()

            link.received.put(b"next session")
            wait_until(lambda: conn.sent[-1:] == [b"next session"], timeout=5)
            assert conn.sent == [b"head", b"next session"]
        finally:
            server._stopping.set()
            service.stop()


def test_service_listen_accepts_one_incoming_connection_and_emits_identity():
    class IncomingLink(FakeLink):
        def listen_once(self, timeout=None):
            self.calls.append(("listen_once", timeout))
            self.peer_call = "STA2"
            self.state = "CONNECTED"
            self.on_event("CONNECTED", mycall=self.mycall, peer=self.peer_call)
            return self.peer_call

    link = IncomingLink()
    service = ModemService(link, poll_interval=0.01)
    events = []
    service.subscribe(lambda name, **details: events.append((name, details)))
    service.start()
    try:
        service.listen(True)
        wait_until(lambda: service.state == "CONNECTED")

        assert events == [("CONNECTED", {"mycall": "STA1", "peer": "STA2"})]
        assert sum(call[0] == "listen_once" for call in link.calls
                   if isinstance(call, tuple)) == 1
    finally:
        service.stop()


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

    def abort(self):
        self.calls.append(("abort",))

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
        ("abort",),
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
    service.abort = lambda: order.append("abort")
    server = StationServer(service, "STA1", 8300, 8301)
    server._send_status = lambda line: order.append(line)

    server._handle_command("ABORT")

    assert order == ["OK", "abort"]


def test_vara_adapter_disconnect_and_abort_are_distinct_service_operations():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)

    server._handle_command("DISCONNECT")
    server._handle_command("ABORT")

    assert sent == ["OK", "OK"]
    assert service.calls == [("disconnect",), ("abort",)]


def test_vara_adapter_reports_failed_connect_as_disconnected():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)

    class FakeConn:
        def close(self):
            raise AssertionError("data connection must survive CONNECT_FAILED")

    conn = FakeConn()
    server._data_conn = conn

    server._on_modem_event("CONNECT_FAILED")

    # capture-conn-fail.log contains no CONNECT FAILED line: after exhausting
    # the outbound attempts, real VARA reports DISCONNECTED directly.
    assert sent == ["DISCONNECTED"]
    # The data connection outlives sessions, failed attempts included.
    assert server._data_conn is conn


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


def test_vara_adapter_rejects_malformed_chat_commands_without_changing_mode():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)

    server._handle_command("CHAT ON")
    server._handle_command("CHAT MAYBE")
    server._handle_command("CHAT OFF EXTRA")

    assert server.chat_mode is True
    assert sent == ["OK"]


def test_vara_adapter_never_interprets_chat_record_framing_on_write():
    record = b"12 Hello Alice!"

    for command in (None, "CHAT ON", "CHAT OFF"):
        service = RecordingService()
        server = StationServer(service, "STA1", 8300, 8301)
        server._send_status = lambda line: None
        if command is not None:
            server._handle_command(command)

        class FakeConn:
            def __init__(self):
                self.chunks = [record, b""]
                self.closed = False

            def recv(self, _size):
                return self.chunks.pop(0)

            def close(self):
                self.closed = True

        server._data_reader_loop(FakeConn())

        assert service.calls == [("write", record)]


def test_vara_adapter_never_synthesizes_or_strips_chat_framing_on_read():
    record = b"12 Hello Alice!"

    for command in (None, "CHAT ON", "CHAT OFF"):
        class ReadService(RecordingService):
            state = "CONNECTED"

            def __init__(self):
                super().__init__()
                self.reads = [record]
                self.on_drain = lambda: None

            def read(self, timeout=None):
                if self.reads:
                    return self.reads.pop(0)
                self.on_drain()
                return None

        class FakeConn:
            def __init__(self):
                self.sent = []
                self.closed = False

            def sendall(self, data):
                self.sent.append(data)

            def close(self):
                self.closed = True

        service = ReadService()
        server = StationServer(service, "STA1", 8300, 8301)
        server._send_status = lambda line: None
        if command is not None:
            server._handle_command(command)
        conn = FakeConn()
        server._data_conn = conn
        # The writer now ends only with the server or the connection, so end
        # the server once the service has nothing left to hand it.
        service.on_drain = server._stopping.set

        server._data_writer_loop(conn)

        assert conn.sent == [record]


def test_vara_adapter_does_not_ack_or_apply_unverified_extensions():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)

    # No available capture establishes compression or WINLINK-session command
    # syntax or semantics. An OK would falsely tell a client that an unknown
    # session mode is active.
    server._handle_command("COMPRESSION ON")
    # Deliberately not a proposed real-VARA token. It represents the
    # unobserved WINLINK-session command family without turning a guess about
    # its spelling into a compatibility promise.
    server._handle_command("UNOBSERVED-WINLINK-EXTENSION")

    assert sent == []
    assert service.calls == []


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


def test_vara_adapter_incoming_connected_status_uses_local_then_caller():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    server._handle_command("BW2300")
    sent = []
    server._send_status = lambda line: sent.append(line)

    # Link emits the accepting station as mycall and the incoming caller as
    # peer. Until a LISTEN-side VARA capture exists, the adapter deliberately
    # uses the same observed local/peer ordering as an outbound connection.
    server._on_modem_event("CONNECTED", mycall="STA1", peer="STA2")

    assert sent == ["CONNECTED STA1 STA2 2300"]


def test_vara_adapter_does_not_report_buffer_before_link_send():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)

    class FakeConn:
        def __init__(self, chunks):
            self._chunks = list(chunks)
            self.closed = False

        def recv(self, n):
            if self._chunks:
                return self._chunks.pop(0)
            return b""

        def close(self):
            self.closed = True

    server._data_reader_loop(FakeConn([b"hello", b"world"]))

    assert service.calls == [("write", b"hello"), ("write", b"world")]
    assert sent == []


def test_service_reports_outbound_drained_after_successful_link_send():
    link = FakeLink()
    link.state = "CONNECTED"
    service = ModemService(link)
    events = []
    service.subscribe(lambda name, **details: events.append((name, details)))
    service._outbound.put(b"one")
    service._outbound.put(b"two")

    service._service_connected()

    assert link.sent == [b"onetwo"]
    assert events == [("OUTBOUND_DRAINED", {})]


def test_service_does_not_report_drained_when_data_arrives_during_send():
    link = FakeLink()
    link.state = "CONNECTED"
    service = ModemService(link)
    events = []
    service.subscribe(lambda name, **details: events.append((name, details)))

    def send_and_enqueue(data):
        link.sent.append(data)
        service.write(b"later")

    link.send_message = send_and_enqueue
    service._send_outbound(b"first")

    assert link.sent == [b"first"]
    assert events == []
    assert service._outbound.get_nowait() == b"later"


def test_vara_adapter_maps_only_confirmed_empty_boundary_to_buffer_zero():
    service = RecordingService()
    server = StationServer(service, "STA1", 8300, 8301)
    sent = []
    server._send_status = lambda line: sent.append(line)

    server._on_modem_event("OUTBOUND_DRAINED")

    assert sent == ["BUFFER 0"]


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


def _serving_station(link):
    """Run a real StationServer with real sockets over ``link``."""
    service = ModemService(link, poll_interval=0.01)
    server = StationServer(service, "STA1", 0, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert server.ready.wait(5)
    return server, service, thread


def _open_data(server):
    conn = socket.create_connection(("127.0.0.1", server.data_port), timeout=5)
    wait_until(lambda: server._data_conn is not None, timeout=5)
    return conn


def test_vara_adapter_accepts_data_connection_before_any_session():
    # Real VARA's client opens the data port once, at start-up, not per
    # session, so the accept loop runs from the moment the server serves.
    link = FakeLink()
    server, service, thread = _serving_station(link)
    try:
        conn = _open_data(server)
        conn.close()
    finally:
        server.stop()
        thread.join(timeout=5)


def test_vara_adapter_data_connection_survives_a_session_and_serves_the_next():
    link = FakeLink()
    server, service, thread = _serving_station(link)
    try:
        conn = _open_data(server)
        conn.settimeout(5)

        service.connect("STA2", mycall="STA1")
        wait_until(lambda: service.state == "CONNECTED")
        conn.sendall(b"first")
        wait_until(lambda: link.sent == [b"first"])
        link.received.put(b"down1")
        assert conn.recv(64) == b"down1"

        service.disconnect()
        wait_until(lambda: service.state == "IDLE")

        # The socket must still be open: a real VARA client keeps it for the
        # life of its command connection and treats a reset as a modem
        # restart. Bytes written with no session up are dropped, not queued,
        # and must not end the reader.
        conn.sendall(b"between sessions")
        conn.settimeout(0.3)
        try:
            assert conn.recv(64) != b"", "data connection was closed"
        except socket.timeout:
            pass
        conn.settimeout(5)

        service.connect("STA2", mycall="STA1")
        wait_until(lambda: service.state == "CONNECTED")
        conn.sendall(b"second")
        wait_until(lambda: link.sent[-1:] == [b"second"], timeout=5)
        assert b"between sessions" not in b"".join(link.sent)
        link.received.put(b"down2")
        assert conn.recv(64) == b"down2"

        conn.close()
    finally:
        server.stop()
        thread.join(timeout=5)


def test_vara_adapter_reaccepts_after_the_client_closes_its_data_socket():
    link = FakeLink()
    server, service, thread = _serving_station(link)
    try:
        first = _open_data(server)
        first.close()
        wait_until(lambda: server._data_conn is None, timeout=5)

        second = socket.create_connection(("127.0.0.1", server.data_port), timeout=5)
        wait_until(lambda: server._data_conn is not None, timeout=5)
        second.settimeout(5)
        service.connect("STA2", mycall="STA1")
        wait_until(lambda: service.state == "CONNECTED")
        second.sendall(b"after reconnect")
        wait_until(lambda: link.sent == [b"after reconnect"])
        second.close()
    finally:
        server.stop()
        thread.join(timeout=5)


def test_vara_adapter_data_reader_survives_a_write_racing_teardown():
    # ModemService.write() raises ConnectionError for every write that is not
    # inside a session; one such race must not kill the reader for good.
    class RacingService(RecordingService):
        def __init__(self):
            super().__init__()
            self.fail_next = True

        def write(self, data):
            if self.fail_next:
                self.fail_next = False
                raise ConnectionError("modem is not connected")
            super().write(data)

    class FakeConn:
        def __init__(self):
            self.chunks = [b"lost", b"kept", b""]
            self.closed = False

        def recv(self, _size):
            return self.chunks.pop(0)

        def close(self):
            self.closed = True

    service = RacingService()
    server = StationServer(service, "STA1", 8300, 8301)
    conn = FakeConn()

    server._data_reader_loop(conn)

    assert service.calls == [("write", b"kept")]
    assert conn.closed is True


# -- receive-burst reporting (SN / BITRATE) and delivery ordering ----------


class _AckingLink(FakeLink):
    """A FakeLink whose recv_message() reproduces Link._handle_data's shape.

    The real link reports the burst, keys the ACK, hands the message up
    while still keyed, and only then unkeys. What matters to a VARA client
    is that event order, so the fake reproduces it exactly rather than the
    modulation underneath it.
    """

    def __init__(self, mode_id=14, bits_per_second=7401.25, snr_db=13.9):
        super().__init__()
        self.mode_id = mode_id
        self.bits_per_second = bits_per_second
        self.snr_db = snr_db

    def recv_message(self, timeout=None):
        message = super().recv_message(timeout=timeout)
        if message is None:
            return None
        self.on_event("SNR", snr_db=self.snr_db)
        self.on_event("BITRATE", direction="RX", mode_id=self.mode_id,
                      bits_per_second=self.bits_per_second)
        self.on_event("PTT", on=True)
        self.on_event("RX_MESSAGE", data=message)
        self.on_event("PTT", off=True)
        return message


def test_net_bits_per_second_matches_the_documented_per_mode_figure():
    from whale.link import net_bits_per_second
    from whale.modes.hf7_mode import HF7
    from whale.modes.hf8_mode import HF8

    for mode in (HF7, HF8):
        expected = mode.chunk_size * 8.0 / mode.airtime(mode.chunk_size)
        assert net_bits_per_second(mode) == expected
    # Ordering sanity: the faster mode must report the higher rate.
    assert net_bits_per_second(HF7) > net_bits_per_second(HF8)


def test_vara_adapter_formats_sn_and_bitrate_like_the_capture():
    """Captured verbatim: 'SN 13.9', 'BITRATE (4)  175 bps TX' and
    'BITRATE (3)  82 bps TX' -- two literal spaces after the index in both,
    so the gap is fixed rather than a right-aligned field."""
    sent = []
    server = StationServer.__new__(StationServer)
    server._send_status = sent.append

    server._on_modem_event("SNR", snr_db=13.94)
    server._on_modem_event("BITRATE", direction="TX", mode_id=4,
                           bits_per_second=175.0)
    server._on_modem_event("BITRATE", direction="RX", mode_id=14,
                           bits_per_second=7401.25)

    assert sent == ["SN 13.9",
                    "BITRATE (4)  175 bps TX",
                    "BITRATE (14)  7401 bps RX"]


def test_vara_adapter_delivers_inbound_between_ptt_on_and_ptt_off():
    """Real VARA writes a decoded payload to the data port while it is
    keying the ACK: SN/BITRATE RX/PTT ON, then the bytes, then PTT OFF.

    Recording the inbound depth alongside each status line is what makes
    this a real ordering assertion rather than a status-line assertion: it
    shows the payload was already deliverable when PTT ON was reported and
    still before PTT OFF, which is exactly the window the capture shows.
    """
    link = _AckingLink()
    service = ModemService(link)
    server = StationServer(service, "STA1", 0, 0)

    trace = []
    server._send_status = lambda line: trace.append((line, service._inbound.qsize()))

    service.start()
    try:
        service.connect("STA2")
        wait_until(lambda: service.state == "CONNECTED")
        link.received.put(b"payload")
        wait_until(lambda: any(line == "PTT OFF" for line, _ in trace), timeout=2.0)

        lines = [line for line, _ in trace]
        burst = trace[lines.index("SN 13.9"):]
        assert [line for line, _ in burst[:4]] == [
            "SN 13.9", "BITRATE (14)  7401 bps RX", "PTT ON", "PTT OFF"]
        # Nothing queued when the burst is reported or when the ACK is keyed;
        # the payload is there by the time the keying ends.
        assert [depth for _, depth in burst[:4]] == [0, 0, 0, 1]

        assert service.read(timeout=0.5) == b"payload"
        # The RX_MESSAGE put and the recv_message() return are one message.
        assert service.read(timeout=0.3) is None
    finally:
        service.stop()
