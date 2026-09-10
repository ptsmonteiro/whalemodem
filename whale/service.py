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
        # Messages already placed on _inbound by the link's RX_MESSAGE event,
        # which recv_message() is still going to return. Only the service
        # worker thread touches it -- the event fires synchronously inside
        # recv_message() on that same thread -- so a plain int is enough.
        self._delivered_early = 0
        self._subscribers: list[EventHandler] = []
        self._subscriber_lock = threading.Lock()
        self._started = threading.Event()
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None
        # VARA Chat does not send a LISTEN ON command.  An idle VARA
        # endpoint accepts incoming CONNECT requests by default; only an
        # outbound CONNECT or an explicit LISTEN OFF should disable that.
        self._listening = True
        self._link.on_event = self._on_link_event

    @classmethod
    def for_radio(cls, radio_name: str, mycall: str, radio_config=None,
                  policy=None, mode_registry=None, **kwargs) -> "ModemService":
        """Production composition root for the current radio/link stack.

        ``policy`` is the :class:`whale.policy.ChannelPolicy` this station
        runs -- its timeouts, its retry budget and, through
        ``mode_ladder``, the waveforms it offers. Defaults to the VHF FM
        bench the modem was built against.
        """
        from whale.policy import VHF_FM
        from whale.transport import RadioTransport

        link = Link(RadioTransport(radio_name, radio_config), mycall,
                    policy=policy or VHF_FM, mode_registry=mode_registry)
        return cls(link, **kwargs)

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
        if name == "RX_MESSAGE":
            # Stream bytes, not a status event: deliver them to the reader
            # immediately, while the link is still keying the ACK, and
            # remember that recv_message() will hand back the same message.
            self._inbound.put(details["data"])
            self._delivered_early += 1
            return
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

    def abort(self) -> None:
        """End the session without transmitting queued application data."""
        self._commands.put(("abort", None))

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
                self._begin_session()
                self._link.connect(destination)
            elif command == "disconnect":
                self._listening = False
                self._flush_outbound()
                self._link.disconnect()
                self._end_session()
            elif command == "abort":
                self._listening = False
                self._discard_queue(self._outbound)
                self._link.disconnect()
                self._end_session()
        return True

    @staticmethod
    def _discard_queue(items: queue.Queue) -> None:
        while True:
            try:
                items.get_nowait()
            except queue.Empty:
                return

    def _end_session(self) -> None:
        """Drop untransmitted bytes once the link is down.

        Only ``_outbound`` is cleared: those bytes can no longer be keyed,
        and a write racing the teardown can still have landed here while the
        link was closing. Received bytes are left for the consumer -- see
        :meth:`_begin_session`.
        """
        self._discard_queue(self._outbound)

    def _begin_session(self) -> None:
        """Drop whatever the previous session left unread in ``_inbound``.

        Received bytes survive teardown -- ``disconnect`` and ``abort`` leave
        ``_inbound`` alone -- so a reader that polls has time to collect the
        tail of a transfer that landed just before the session ended.
        Session isolation is enforced here instead, at the moment the next
        session starts: this is the service worker thread, the only producer
        of ``_inbound``, so once this drain returns no byte from an earlier
        session can be handed to a reader as part of this one.
        """
        self._discard_queue(self._inbound)
        self._delivered_early = 0

    def _flush_outbound(self) -> None:
        chunks = []
        while True:
            try:
                chunks.append(self._outbound.get_nowait())
            except queue.Empty:
                break
        if chunks and self._link.state == "CONNECTED":
            self._send_outbound(b"".join(chunks))

    def _send_outbound(self, data: bytes) -> None:
        """Complete one link send and report when the application queue drains.

        The event deliberately carries no byte/frame count.  The VARA adapter
        can map a confirmed empty boundary to the only captured status value,
        ``BUFFER 0``, without inventing semantics for nonzero values.
        """
        self._link.send_message(data)
        if self._outbound.empty():
            self._emit("OUTBOUND_DRAINED")

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
            self._send_outbound(b"".join(chunks))
            return
        message = self._link.recv_message(timeout=self._poll_interval)
        if message is not None:
            if self._delivered_early:
                self._delivered_early -= 1
            else:
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
                            self._begin_session()
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
