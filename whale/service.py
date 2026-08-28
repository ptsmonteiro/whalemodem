"""Transport-independent connection and byte-stream service.

Application adapters use :class:`ModemService`; only this module knows that
the current protocol implementation is ``Link`` or how its blocking calls
must be scheduled.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Protocol

from whale.link import Link, LinkError

logger = logging.getLogger(__name__)

EventHandler = Callable[..., None]


class LinkProtocol(Protocol):
    mycall: str
    state: str
    on_event: EventHandler

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def connect(self, destination: str) -> bool: ...
    def listen_once(self, timeout: float | None = None): ...
    def send_message(self, data: bytes) -> None: ...
    def recv_message(self, timeout: float | None = None): ...
    def service_while_idle(self) -> bool: ...
    def disconnect(self, retries: int = 3) -> bool: ...


class ModemService:
    """Thread-safe connection/stream API over a blocking link protocol.

    ``connect`` and ``listen`` initiate work and return immediately. Events
    report connection progress. ``write`` applies bounded backpressure;
    ``read`` returns received stream chunks, or ``None`` on timeout.
    """

    def __init__(self, link: LinkProtocol, *, queue_size: int = 64,
                 poll_interval: float = 0.5):
        self._link = link
        self._poll_interval = poll_interval
        self._outbound: queue.Queue[bytes] = queue.Queue(maxsize=queue_size)
        self._inbound: queue.Queue[bytes] = queue.Queue(maxsize=queue_size)
        self._commands: queue.Queue[tuple[str, object]] = queue.Queue()
        self._subscribers: list[EventHandler] = []
        self._subscriber_lock = threading.Lock()
        self._started = threading.Event()
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None
        self._listening = False
        self._link.on_event = self._on_link_event

    @classmethod
    def for_radio(cls, radio_name: str, mycall: str, radio_config=None, **kwargs) -> "ModemService":
        """Production composition root for the current radio/link stack."""
        from whale.transport import RadioTransport

        return cls(Link(RadioTransport(radio_name, radio_config), mycall), **kwargs)

    @property
    def state(self) -> str:
        return self._link.state

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        with self._subscriber_lock:
            self._subscribers.append(handler)

        def unsubscribe() -> None:
            with self._subscriber_lock:
                if handler in self._subscribers:
                    self._subscribers.remove(handler)
        return unsubscribe

    def _emit(self, name: str, **details) -> None:
        with self._subscriber_lock:
            subscribers = tuple(self._subscribers)
        for handler in subscribers:
            try:
                handler(name, **details)
            except Exception:
                logger.exception("modem event subscriber failed")

    def _on_link_event(self, name: str, **details) -> None:
        self._emit(name, **details)

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stopping.clear()
        self._worker = threading.Thread(target=self._run, name="modem-service", daemon=True)
        self._worker.start()
        self._started.wait()

    def stop(self) -> None:
        if self._worker is None:
            return
        self._stopping.set()
        self._commands.put(("stop", None))
        self._worker.join(timeout=max(2.0, self._poll_interval * 4))

    def set_callsign(self, callsign: str) -> None:
        self._commands.put(("callsign", callsign))

    def listen(self, enabled: bool = True) -> None:
        self._commands.put(("listen", enabled))

    def connect(self, destination: str, *, mycall: str | None = None) -> None:
        self._commands.put(("connect", (mycall, destination)))

    def disconnect(self) -> None:
        self._commands.put(("disconnect", None))

    def write(self, data: bytes, timeout: float | None = None) -> None:
        if not data:
            return
        if self.state != "CONNECTED":
            raise ConnectionError("modem is not connected")
        self._outbound.put(bytes(data), timeout=timeout)

    def read(self, timeout: float | None = None) -> bytes | None:
        try:
            return self._inbound.get(timeout=timeout)
        except queue.Empty:
            return None

    def _drain_commands(self) -> bool:
        while True:
            try:
                command, value = self._commands.get_nowait()
            except queue.Empty:
                return True
            if command == "stop":
                return False
            if command == "callsign":
                self._link.mycall = str(value)
            elif command == "listen":
                self._listening = bool(value)
            elif command == "connect":
                mycall, destination = value
                if mycall:
                    self._link.mycall = mycall
                self._listening = False
                self._link.connect(destination)
            elif command == "disconnect":
                self._listening = False
                self._link.disconnect()
        return True

    def _service_connected(self) -> None:
        try:
            first = self._outbound.get_nowait()
        except queue.Empty:
            first = None
        if first is not None:
            chunks = [first]
            while True:
                try:
                    chunks.append(self._outbound.get_nowait())
                except queue.Empty:
                    break
            self._link.send_message(b"".join(chunks))
            return
        message = self._link.recv_message(timeout=self._poll_interval)
        if message is not None:
            self._inbound.put(message)

    def _run(self) -> None:
        self._link.start()
        self._started.set()
        try:
            while not self._stopping.is_set():
                if not self._drain_commands():
                    break
                try:
                    if self._link.state == "CONNECTED":
                        self._service_connected()
                    elif self._listening:
                        if self._link.listen_once(timeout=self._poll_interval) is not None:
                            self._listening = False
                    else:
                        try:
                            command = self._commands.get(timeout=self._poll_interval)
                        except queue.Empty:
                            continue
                        self._commands.put(command)
                except LinkError as exc:
                    logger.warning("link operation failed: %s", exc)
                    if self._link.state == "CONNECTED":
                        self._link.disconnect(retries=1)
        finally:
            if self._link.state == "CONNECTED":
                self._link.disconnect(retries=1)
            self._link.stop()
            self._started.clear()
