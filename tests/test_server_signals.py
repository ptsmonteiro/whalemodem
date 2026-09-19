"""whale-server's stop ladder: polite first, now on the second ask.

The escalation itself is whale-test's (see its teardown), and the two have
to agree, because whale-test stops its modem by signalling it. These
exercise the server end on its own, with a stand-in for the service so that
no radio, no threads and no real waiting are involved.
"""

import signal

import pytest

from whale.vara_server import stop_on_signals

SIGNALS = [name for name in ("SIGTERM", "SIGINT", "SIGBREAK")
           if hasattr(signal, name)]


class _Server:
    """A StationServer's stop(), and nothing else.

    ``finishes`` is whether the graceful stop completes inside its grace
    period -- the one thing the ladder branches on.
    """

    def __init__(self, finishes=True):
        self.finishes = finishes
        self.stops = []

    def stop(self, *, graceful=True, timeout=None):
        self.stops.append((graceful, timeout))
        return self.finishes or not graceful


def _raise(name="SIGINT"):
    signal.raise_signal(getattr(signal, name))


@pytest.fixture
def handlers():
    """Put every handler back, whatever the test did to them."""
    previous = {name: signal.getsignal(getattr(signal, name)) for name in SIGNALS}
    yield
    for name, handler in previous.items():
        signal.signal(getattr(signal, name), handler)


def test_the_first_signal_is_the_polite_one(handlers):
    """A session still up is worth a parting DISC, on a clock."""
    server = _Server()
    stop_on_signals(server, grace=12.0)

    with pytest.raises(KeyboardInterrupt):
        _raise()

    assert server.stops == [(True, 12.0)], "the first signal was not graceful"


def test_a_goodbye_that_runs_out_of_grace_stops_anyway(handlers):
    """The courtesy is bounded: when it expires the radio still comes down."""
    server = _Server(finishes=False)
    stop_on_signals(server, grace=0.0)

    with pytest.raises(KeyboardInterrupt):
        _raise()

    assert [graceful for graceful, _ in server.stops] == [True, False], (
        "a graceful stop that never finished was left on the air")


def test_a_second_signal_drops_the_courtesy(handlers):
    """Pressed again means now -- the second stop keys nothing."""
    server = _Server()
    stop_on_signals(server)

    # The first signal's graceful stop is where the second one lands, so the
    # stand-in raises it from inside that call rather than after it.
    def interrupt_during_the_goodbye(*, graceful=True, timeout=None):
        server.stops.append((graceful, timeout))
        if len(server.stops) == 1:
            _raise("SIGTERM" if "SIGTERM" in SIGNALS else "SIGINT")
        return True

    server.stop = interrupt_during_the_goodbye
    with pytest.raises(KeyboardInterrupt):
        _raise()

    assert [graceful for graceful, _ in server.stops] == [True, False]


def test_the_second_signal_hands_the_process_back_to_the_default_handler(handlers):
    """Rung three: a third press must not have to be asked for politely.

    Nothing is left keyed by it -- RadioTransport registers an atexit
    un-key, and the default SIGINT handler unwinds through it.
    """
    server = _Server()
    stop_on_signals(server)
    ours = signal.getsignal(signal.SIGINT)

    with pytest.raises(KeyboardInterrupt):
        _raise()
    assert signal.getsignal(signal.SIGINT) is ours, (
        "the first signal gave up the handler; a second press would have "
        "ended the process mid-goodbye")

    with pytest.raises(KeyboardInterrupt):
        _raise()
    assert signal.getsignal(signal.SIGINT) is not ours


def test_every_signal_a_parent_can_send_is_answered(handlers):
    """whale-test sends SIGTERM, or CTRL_BREAK on Windows; operators send
    SIGINT. All of them have to reach the ladder."""
    for name in SIGNALS:
        server = _Server()
        stop_on_signals(server)
        with pytest.raises(KeyboardInterrupt):
            _raise(name)
        assert server.stops == [(True, 30.0)], f"{name} was not answered"
