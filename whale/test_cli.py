"""``whale-test``: an over-the-air check between two stations, with nothing
to configure but the station itself.

The point of this command is a helper who has never run Whale before. They
install it, run ``whale-configure``, and run ``whale-test``. There is no
server to start, no port to choose, and no VARA terminal application to
install and point at anything: this command starts the real modem
(``whale-server``) as its own process on loopback ports it picks, drives it
over TCP through the same client the acceptance test uses -- exactly as any
client application would -- and writes one report file the helper can send
back.

Running the shipped modem rather than an in-process copy of it is the point:
what the helper checks is the program the operator will actually run, with
its own process, its own radio and audio handles, and its own lifetime. The
report is therefore assembled from what a client can see, which is the
status lines on the command port (``whale/vara_server.py``'s module
docstring lists them).

    whale-test              wait for someone to call (the helper)
    whale-test CALLSIGN     call that station (whoever asked for the test)

There is a second exercise behind ``--sweep``, which measures the path one
waveform mode at a time instead of running a session, and which drives the
radio in this process rather than starting the modem. Its own rules -- what
it measures, what a failing mode means and what it exits with -- are in
``whale/sweep.py``.

    whale-test --sweep              wait to be swept
    whale-test --sweep CALLSIGN     sweep that station

The exercise is exactly the acceptance scenario in ``acceptance_test.py``:
connect, 4 KB one way, 4 KB back, disconnect, both payloads verified
byte-for-byte. Its payload and its budgets are imported from there rather
than restated, so the two drivers cannot drift apart.

Ctrl-C stops the run at any point: at the confirmation prompt nothing has
been transmitted and nothing is written, and once the station is up it ends
the session the polite way -- a parting DISC, so the far end is not left
waiting out its inactivity timeout -- then brings the modem process down
unkeyed and still writes the report for the part that ran. A second Ctrl-C,
or a goodbye the air does not carry within the grace period, skips the
courtesy and takes the radio down immediately.

Exit status: 0 the test passed, 1 it ran and failed on air, 2 the station
could not be brought up at all (configuration or devices) and nothing was
ever transmitted.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from acceptance_test import (CONNECT_TIMEOUT, PAYLOAD_TAG_AB, PAYLOAD_TAG_BA,
                             TRANSFER_TIMEOUT, StationClient, _transfer_summary,
                             payload)
from whale import policy, sweep
from whale.config import app_config, get_radio
from whale.hw import audio_io
from whale.hw.ptt_backends import available_backends
from whale.mode_qualification import registry

logger = logging.getLogger(__name__)

#: Teardown budget, matching acceptance_test.py's own DISCONNECTED waits.
DISCONNECT_TIMEOUT = 30.0

#: How long a graceful teardown waits for its parting DISC to be answered
#: before giving up on it. The operator has just asked for this to be over,
#: so it is a courtesy on a clock, not another transfer budget: when it
#: expires the modem is stopped the other way, unkeyed.
GRACE_TIMEOUT = DISCONNECT_TIMEOUT

#: How long the modem process is given to answer on its command port. Unlike
#: the in-process server this replaced, it is starting an interpreter and
#: opening the sound card and the PTT backend first, so it is given room.
MODEM_START_TIMEOUT = 30.0

#: How often the command port is retried while the modem is still starting.
MODEM_START_POLL = 0.2

#: How long the modem process is given to stop on its own, unkeying as it
#: goes, before it is killed outright. A stop that is waiting out a burst in
#: progress is exactly the one worth waiting for.
MODEM_STOP_TIMEOUT = 15.0

#: Lines of the modem's own stderr kept for a diagnostic. Enough to carry a
#: traceback, few enough that a report is not a log file.
MODEM_LOG_LINES = 40

#: How many of those go into a failure message -- the last words, not the log.
COMPLAINT_LINES = 3

#: How often the main thread comes up for air while the exercise runs, so
#: that a pending Ctrl-C is delivered. Short enough to feel immediate, long
#: enough to cost nothing over a multi-minute transfer.
INTERRUPT_POLL = 0.2

HARDWARE_DOC = "docs/HARDWARE.md"


class PreflightError(Exception):
    """One or more setup problems, each a plain sentence. Exit status 2."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class TestFailure(Exception):
    """The exercise ran and did not pass. Exit status 1."""


@dataclass(frozen=True)
class Setup:
    """Everything preflight resolved, ready to build a station from."""

    config_path: str | None
    mycall: str
    channel_name: str
    channel: object
    mode_registry: object
    radio: object
    output_device: int
    input_device: int


# -- transcript ------------------------------------------------------------

@dataclass
class Transcript:
    """The run's own record: progress steps plus a digest of modem events.

    Every step is printed as it happens and kept for the report file, which
    has to stand alone once it is mailed to someone who did not watch the
    run. Modem events are summarised rather than transcribed: a 4 KB
    transfer keys dozens of times, and a report full of PTT lines is harder
    to read than a count of them.
    """

    started: float = field(default_factory=time.monotonic)
    lines: list[str] = field(default_factory=list)
    snr_db: list[float] = field(default_factory=list)
    keyings: int = 0
    modes: dict[tuple[int, str], int] = field(default_factory=dict)
    connected: bool = False

    def step(self, text: str) -> None:
        print(text, flush=True)
        self.lines.append(f"[{time.monotonic() - self.started:7.1f}s] {text}")

    def on_status(self, line: str) -> None:
        """Subscriber on the modem's command port.

        The modem is another process now, so its internal events are not
        available here; what is available is every status line it pushes to
        a connected client, which is where each of this report's fields
        comes from (see whale/vara_server.py's module docstring for the
        wire shapes):

            PTT ON/OFF                  -> Keyings
            SN <x.x>                    -> Receive SNR
            BITRATE (<id>)  <n> bps <D> -> Modes used
            CONNECTED/DISCONNECTED      -> the transcript's session lines

        One distinction does not survive the wire: the modem reports a
        failed outbound attempt to a client as DISCONNECTED, exactly as
        real VARA does, so the transcript no longer says CONNECT_FAILED.
        This runs on the client's reader thread and must never raise into
        it: a status line it cannot parse costs a report field, not a run.
        """
        try:
            if line.startswith("PTT "):
                self.keyings += line[4:].strip() == "ON"
            elif line.startswith("SN "):
                self.snr_db.append(float(line[3:]))
            elif line.startswith("BITRATE ("):
                mode_id, _, rest = line[len("BITRATE ("):].partition(")")
                key = (int(mode_id), rest.split()[-1])
                self.modes[key] = self.modes.get(key, 0) + 1
            elif line.startswith("CONNECTED"):
                # CONNECTED <mycall> <peer> <bandwidth>
                fields = line.split()
                self.connected = True
                self._session("CONNECTED", fields[2] if len(fields) > 2 else None)
            elif line.startswith("DISCONNECTED"):
                self.connected = False
                self._session("DISCONNECTED", None)
        except Exception:
            logger.debug("transcript status %r ignored", line, exc_info=True)

    def _session(self, name: str, peer: str | None) -> None:
        self.lines.append(f"[{time.monotonic() - self.started:7.1f}s] modem: {name}"
                          + (f" peer={peer}" if peer else ""))

    def snr_summary(self) -> str:
        if not self.snr_db:
            return "no decoded DATA bursts reported an SNR"
        return (f"{len(self.snr_db)} observation(s): "
                f"min {min(self.snr_db):.1f} dB, "
                f"mean {sum(self.snr_db) / len(self.snr_db):.1f} dB, "
                f"max {max(self.snr_db):.1f} dB")


# -- preflight -------------------------------------------------------------

def preflight(config_path: str | None, channel_name: str) -> Setup:
    """Resolve the whole station without touching the transmitter.

    Anything that would only fail once the radio is keyed belongs in the
    exercise, not here: this stage exists so that a misconfigured station
    says so in a sentence instead of transmitting and then failing. It runs
    before the modem process is started, so a station that cannot work never
    starts one.

    Nothing here takes a device the modem is about to want: audio_io.check_device
    is Pa_IsFormatSupported, a query about the format, and never opens a
    stream (see its docstring), so the card is free for the child.
    """
    try:
        config = app_config(config_path)
    except (OSError, ValueError) as exc:
        # Nothing further can be resolved without the configuration, so this
        # is the one problem reported on its own.
        raise PreflightError([f"Configuration could not be loaded: {exc}. "
                              "Run whale-configure."]) from None

    problems: list[str] = []
    mycall = ""
    try:
        mycall = config.station_callsign
    except Exception as exc:  # pragma: no cover - load_config validates this
        problems.append(f"The configured callsign is not usable: {exc}.")
    if not mycall:
        problems.append("No station callsign is configured. Run whale-configure.")

    radio = None
    try:
        radio = get_radio(None, channel_name, config_path)
    except (OSError, ValueError) as exc:
        problems.append(f"No radio is available for the {channel_name} channel: {exc}.")

    output_device = input_device = -1
    if radio is not None:
        if radio.ptt_backend not in available_backends():
            problems.append(
                f"Radio {radio.id!r} uses PTT backend {radio.ptt_backend!r}, "
                "which this build does not have.")
        try:
            output_device, input_device = radio.devices()
        except (LookupError, OSError) as exc:
            problems.append(f"The audio devices for radio {radio.id!r} were not found: {exc}.")
        else:
            for device, kind, name in ((output_device, "output", radio.audio_output_name),
                                       (input_device, "input", radio.audio_input_name)):
                try:
                    audio_io.check_device(device, kind)
                except Exception as exc:
                    problems.append(
                        f"The {kind} device {name!r} will not accept whale's audio "
                        f"format: {exc}.")

    if problems:
        raise PreflightError(problems)

    channel = policy.by_name(channel_name)
    return Setup(config_path, mycall, channel_name, channel,
                 registry(channel_name, "default", channel.max_useful_frame_seconds),
                 radio, output_device, input_device)


def confirm(setup: Setup, read=None) -> None:
    """Show what is about to go on air and wait for one Enter."""
    print(f"Callsign:    {setup.mycall}")
    print(f"Channel:     {setup.channel_name} ({setup.channel.name})")
    print(f"Radio:       {setup.radio.id} ({setup.radio.name})")
    print(f"PTT backend: {setup.radio.ptt_backend}")
    print(f"Audio out:   {setup.radio.audio_output_name}")
    print(f"Audio in:    {setup.radio.audio_input_name}")
    print()
    print(f"This will transmit. Check your radio, power and antenna first -- see {HARDWARE_DOC}.")
    # Resolved here rather than as a default argument so that a test (or an
    # embedder) replacing builtins.input is actually honoured.
    (read or input)("Press Enter to start, or Ctrl-C to stop: ")


# -- the station -----------------------------------------------------------

@dataclass
class Station:
    """The modem process this run started, and how to reach and read it."""

    process: subprocess.Popen
    cmd_port: int
    data_port: int
    log: deque = field(default_factory=lambda: deque(maxlen=MODEM_LOG_LINES))

    def complaint(self) -> str:
        """The end of the modem's own stderr, for a failure message.

        Short on purpose: this becomes the report's Result line, and what a
        helper needs from it is the sentence the modem stopped on, not the
        usage block or the log that came before it.
        """
        tail = list(self.log)[-COMPLAINT_LINES:]
        return " Its last words were: " + " / ".join(tail) if tail else ""


def modem_argv(setup: Setup, cmd_port: int, data_port: int,
               verbose: bool = False) -> list[str]:
    """The shipped modem command, with this run's station on the front.

    A frozen bundle has no interpreter to re-enter, but it does ship
    whale-server next to whale-test in the same folder (see
    packaging/pyinstaller/whale.spec), so that is where it is looked up.
    From a checkout or an install, ``-m`` runs the same entry point without
    depending on the console script being on PATH.
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name(
            "whale-server" + (".exe" if os.name == "nt" else ""))
        if not exe.exists():
            raise TestFailure(f"the modem program is missing from this build: {exe}")
        argv = [str(exe)]
    else:
        argv = [sys.executable, "-m", "whale.vara_server"]
    argv += ["--radio", setup.radio.id, "--channel", setup.channel_name,
             "--host", "127.0.0.1",
             "--cmd-port", str(cmd_port), "--data-port", str(data_port)]
    if setup.config_path:
        argv += ["--config", setup.config_path]
    if verbose:
        argv.append("-v")
    return argv


def reserve_ports() -> tuple[int, int]:
    """Two free loopback ports for the modem process to bind.

    The in-process server this replaced was handed port 0 and wrote back
    what the OS gave it; across a process boundary the number has to be
    known before the child exists. Of the two ways to get there -- ask the
    OS here and pass the numbers down, or have the child report what it
    bound -- this is the first, because the second means parsing the
    modem's log for its own startup line and so makes the report depend on
    a log format rather than on an API.

    Both sockets are held open at once and released only as the child is
    spawned, so the pair is distinct from each other and from every port
    bound anywhere on this machine at that moment: two whale-test runs
    cannot be given the same pair, which is the property the old port-0
    scheme had. What is left is a race, not a collision -- between the
    release here and the bind in the child, some unrelated process could
    take one. The child then fails to bind and exits, and start_station
    says so rather than hanging.
    """
    with socket.socket() as cmd, socket.socket() as data:
        cmd.bind(("127.0.0.1", 0))
        data.bind(("127.0.0.1", 0))
        return cmd.getsockname()[1], data.getsockname()[1]


def _drain_log(station: Station, echo: bool) -> None:
    """Keep the modem's stderr moving, and keep the last of it.

    An undrained pipe fills and blocks the modem mid-session, so this
    thread has to exist whether or not anyone reads what it collects.
    """
    for line in station.process.stderr:
        line = line.rstrip()
        if not line:
            continue
        station.log.append(line)
        if echo:
            print(line, file=sys.stderr, flush=True)


def start_station(setup: Setup, verbose: bool = False) -> Station:
    """Start the shipped modem as its own process on loopback ports.

    It is deliberately put in its own process group (its own session on
    POSIX): a console Ctrl-C is delivered to every process in the group, and
    a modem that takes the interrupt at the same moment as this command
    would be tearing itself down while the teardown here is still trying to
    talk to it. The order matters -- client first, then the modem -- so the
    interrupt must arrive in one place only.
    """
    cmd_port, data_port = reserve_ports()
    argv = modem_argv(setup, cmd_port, data_port, verbose)
    logger.debug("starting the modem: %s", " ".join(argv))
    if os.name == "nt":
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group = {"start_new_session": True}
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, env=_modem_env(), text=True,
            errors="replace", bufsize=1, **group)
    except OSError as exc:
        raise TestFailure(f"the modem program could not be started: {exc}") from None
    station = Station(process, cmd_port, data_port)
    threading.Thread(target=_drain_log, args=(station, verbose),
                     name="whale-test-modem-log", daemon=True).start()
    return station


def _modem_env() -> dict:
    """The child's environment: this process's, plus where whale was found.

    Run from a checkout, ``whale`` is importable here because of where this
    command was started from, which the child does not inherit.
    """
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        import whale

        root = str(Path(whale.__file__).resolve().parent.parent)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = root + os.pathsep + existing if existing else root
    return env


def connect_station(station: Station, transcript: Transcript, mycall: str,
                    timeout: float = MODEM_START_TIMEOUT):
    """Connect to the modem's command port the way any client application does.

    There is no readiness signal to wait on across a process boundary, so
    readiness *is* the command port answering. A modem that died on the way
    up -- a radio it cannot open, a port taken between the reservation and
    its bind -- is noticed here as an exited process rather than waited out.
    """
    deadline = time.monotonic() + timeout
    while True:
        status = station.process.poll()
        if status is not None:
            raise TestFailure(f"the modem stopped with status {status} before it "
                              f"answered.{station.complaint()}")
        try:
            return StationClient(mycall, "127.0.0.1", station.cmd_port,
                                 station.data_port, on_status=transcript.on_status)
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise TestFailure(
                    f"the modem did not answer on 127.0.0.1:{station.cmd_port} "
                    f"within {timeout:.0f}s: {exc}.{station.complaint()}") from None
            time.sleep(MODEM_START_POLL)


def close_client(client) -> None:
    if client is None:
        return
    if client.data is not None:
        client.data.close()
    try:
        client.cmd.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    client.cmd.close()


def stop_modem(station: Station, timeout: float = MODEM_STOP_TIMEOUT, *,
               force: bool = False) -> None:
    """Ask the modem process to stop, and only kill it if it will not.

    Asking is the whole point: the modem answers SIGTERM (Ctrl-Break on
    Windows) by stopping its service, which takes PTT down and closes the
    radio. A kill does none of that and can leave a transmitter keyed, so it
    is what happens after the timeout, not instead of it.

    ``force`` is how "stop without saying goodbye" crosses the process
    boundary. The modem runs the same escalation this command does -- see
    vara_server.stop_on_signals -- where the first signal is the polite one
    and a second means now. So a forced stop is two signals, and the child
    resolves it with its own ladder rather than with a flag only whale-test
    would know how to set. Without this, a second Ctrl-C here would drop our
    parting DISC only for the child to key one of its own.
    """
    process = station.process
    if process.poll() is not None:
        return
    for _ in range(2 if force else 1):
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt"
                                else signal.SIGTERM)
        except (OSError, ValueError):  # already gone
            break
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        logger.warning("the modem did not stop within %.0fs; killing it", timeout)
    process.kill()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def _say_goodbye(client, transcript: Transcript) -> None:
    """Send the parting DISC and wait for the modem to say it went out."""
    client.send_cmd("DISCONNECT")
    client.wait_for("DISCONNECTED", GRACE_TIMEOUT)


def stop_station(station: Station, client, transcript: Transcript, *,
                 graceful: bool = True) -> None:
    """Take the radio out of the modem's hands, then end its process.

    ``graceful`` is the same distinction ModemService.stop() draws: whether
    a session still up at this point is ended with a parting DISC -- a
    courtesy that saves the peer waiting out its inactivity timeout, but one
    more keying -- or simply abandoned. It is asked for here, over the
    command port, because it is the only step that transmits; the process is
    stopped afterwards either way, and the modem's own signal handler always
    stops non-gracefully, so nothing goes on air after this line.

    The goodbye is bounded twice over, because it is the one step that can
    hang on a channel that has already stopped working. `GRACE_TIMEOUT`
    bounds it in time, and `run_interruptibly` bounds it in patience: the
    wait for DISCONNECTED is a blocking socket read, which on Windows a
    Ctrl-C does not break at all (see that function), so without it a second
    interrupt during the grace period would be ignored for as long as the
    first one's courtesy took -- the opposite of what pressing it means.
    """
    if graceful and client is not None and transcript.connected:
        try:
            run_interruptibly(_say_goodbye, client, transcript)
        except (OSError, TimeoutError):
            pass
    close_client(client)
    stop_modem(station, force=not graceful)


def stop_station_uninterrupted(station: Station, client, transcript: Transcript, *,
                               graceful: bool = True) -> None:
    """`stop_station`, finished even if Ctrl-C is pressed again during it.

    This is the teardown that takes the transmitter out of the modem's
    hands, so a second, impatient Ctrl-C landing in the middle of it is
    exactly the one that must not be honoured: it would leave the radio
    keyed -- and with the modem in its own process, it would leave it keyed
    after this command has exited. Every step of `stop_station` can be
    repeated harmlessly -- the sockets are already closing, and a stop
    signal to a process that is already stopping changes nothing -- so the
    interrupt is absorbed and the teardown restarted. Two attempts, because
    a teardown that is still being interrupted after that is one the
    operator is holding the key down on, and the process is about to exit
    regardless.

    What the second interrupt does change is the courtesy: it is dropped and
    not retried. The first Ctrl-C asked for the run to end and was willing
    to spend a keying saying so; the second asked for it to end now.
    """
    for last in (False, True):
        try:
            stop_station(station, client, transcript, graceful=graceful)
            return
        except KeyboardInterrupt:
            # The parting DISC is not retried: the operator has now asked
            # twice for this to be over, and what is left is the part that
            # unkeys.
            graceful = False
            if last:
                return


def run_interruptibly(target, *args, poll: float = INTERRUPT_POLL) -> None:
    """Run `target(*args)` so that Ctrl-C always reaches this thread.

    The exercise spends its time blocked on sockets, and on Windows a
    blocking socket call does not wake up for Ctrl-C at all: the console
    handler sets a flag that Python only acts on between bytecodes, and a
    receive waiting out a 300-second transfer budget executes none. Pressing
    Ctrl-C there did nothing until the transfer's own timeout expired.

    So the blocking is done on a daemon thread and this one waits on an
    event, which is a wait the interrupt does break. The exception then
    propagates to the caller, whose teardown closes the client sockets and
    stops the server -- which is what unblocks the worker. It is a daemon,
    so even a worker stuck somewhere the close cannot reach cannot keep the
    process alive.
    """
    done = threading.Event()
    raised: list[BaseException] = []

    def body():
        try:
            target(*args)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            raised.append(exc)
        finally:
            done.set()

    threading.Thread(target=body, name="whale-test-exercise", daemon=True).start()
    while not done.wait(poll):
        pass
    if raised:
        raise raised[0]


# -- the exercise ----------------------------------------------------------

def _send(client, transcript: Transcript, data: bytes) -> None:
    transcript.step(f"Sending {len(data)} bytes...")
    client.send_data(data)


def _receive(client, transcript: Transcript, expected: bytes, timeout: float) -> None:
    transcript.step(f"Waiting for {len(expected)} bytes...")
    started = time.perf_counter()
    got = client.recv_data(len(expected), timeout)
    elapsed = time.perf_counter() - started
    if len(got) != len(expected):
        raise TestFailure(f"received {len(got)} of {len(expected)} bytes "
                          f"in {elapsed:.0f}s")
    if got != expected:
        wrong = sum(1 for a, b in zip(got, expected) if a != b)
        raise TestFailure(f"payload mismatch: {wrong} of {len(expected)} bytes differ")
    transcript.step(_transfer_summary("We", len(got), elapsed).strip())
    transcript.step("Payload verified byte-for-byte.")


def run_exercise(client, mycall: str, peer: str | None, transcript: Transcript,
                 connect_timeout: float = CONNECT_TIMEOUT,
                 transfer_timeout: float = TRANSFER_TIMEOUT) -> None:
    """Drive one side of the acceptance scenario over an open client.

    ``peer`` names the station to call; ``None`` waits to be called. The
    caller is the acceptance test's station A and sends first.
    """
    client.send_cmd(f"MYCALL {mycall}")
    if peer is None:
        transcript.step(f"Listening as {mycall}. Ask the other station to run: "
                        f"whale-test {mycall}")
        client.send_cmd("LISTEN ON")
    else:
        transcript.step(f"Calling {peer} as {mycall}...")
        client.send_cmd(f"CONNECT {mycall} {peer}")
    transcript.step(client.wait_for("CONNECTED", connect_timeout))
    client.open_data()

    if peer is None:
        _receive(client, transcript, payload(PAYLOAD_TAG_AB), transfer_timeout)
        _send(client, transcript, payload(PAYLOAD_TAG_BA))
        # _send only hands the bytes to the data socket; the transmission
        # itself is still ahead of us, and the caller will not send its
        # DISCONNECT until the whole 4 KB has arrived. So the answering side
        # is waiting out a full transfer here, not a teardown -- 30s was a
        # budget for the disconnect alone and expired mid-transmission.
        disconnect_timeout = transfer_timeout + DISCONNECT_TIMEOUT
    else:
        _send(client, transcript, payload(PAYLOAD_TAG_AB))
        _receive(client, transcript, payload(PAYLOAD_TAG_BA), transfer_timeout)
        transcript.step("Disconnecting.")
        client.send_cmd("DISCONNECT")
        # The calling side has already seen the transfer land and is waiting
        # only for the teardown it just asked for.
        disconnect_timeout = DISCONNECT_TIMEOUT
    client.wait_for("DISCONNECTED", disconnect_timeout)
    transcript.step("Disconnected.")


# -- the report ------------------------------------------------------------

def _whale_version() -> str:
    """The installed version, or a sentence saying why it is not known.

    A standalone bundle and a plain checkout both run without distribution
    metadata, so no version here is normal and must never end the run.
    """
    from importlib.metadata import version

    try:
        return version("whale")
    except Exception:
        return "unknown (no package metadata: standalone build or checkout)"


def _role(peer: str | None, sweeping: bool) -> str:
    if not sweeping:
        return "calling " + peer if peer else "answering"
    return "sweeping " + peer if peer else "swept (listening)"


def report_name(mycall: str, when: datetime) -> str:
    return f"whale-report-{mycall}-{when.strftime('%Y%m%dT%H%M%SZ')}.txt"


def write_report(path: Path, setup: Setup, peer: str | None,
                 transcript: Transcript, outcome: str,
                 sweep_results=None) -> Path:
    """Write the one file a helper sends back. It must stand on its own.

    ``sweep_results`` adds the per-mode table a frame sweep produces. Both
    ends of a sweep write one, and the two are not interchangeable: each
    station's RX rows are what only it could see.
    """
    modes = ", ".join(f"{mode_id} {direction} x{count}"
                      for (mode_id, direction), count in sorted(transcript.modes.items()))
    lines = [
        "whale-test report",
        "",
        f"Result:      {outcome}",
        f"Run at:      {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
        f"Whale:       {_whale_version()}",
        f"Platform:    {platform.platform()} / Python {platform.python_version()}",
        f"Role:        {_role(peer, sweep_results is not None)}",
        f"Callsign:    {setup.mycall}",
        f"Channel:     {setup.channel_name} ({setup.channel.name})",
        f"Radio:       {setup.radio.id} ({setup.radio.name})",
        f"PTT backend: {setup.radio.ptt_backend}",
        f"Audio out:   {setup.radio.audio_output_name} (device {setup.output_device})",
        f"Audio in:    {setup.radio.audio_input_name} (device {setup.input_device})",
        f"Modes offered: {', '.join(str(i) for i in setup.mode_registry.supported_ids)}",
        f"Modes used:  {modes or 'none: no DATA burst was keyed or decoded'}",
        f"Keyings:     {transcript.keyings}",
        f"Receive SNR: {transcript.snr_summary()}",
        "",
    ]
    if sweep_results is not None:
        lines += ["Per-mode frame sweep",
                  "--------------------"]
        lines += sweep.result_table(sweep_results)
        lines += ["",
                  "Dir TX: frames this station sent, as the other station "
                  "decoded them.",
                  "Dir RX: frames this station received. Only this end saw them.",
                  sweep.SNR_NOTE,
                  ""]
    lines += [
        "Session transcript",
        "------------------",
    ]
    lines.extend(transcript.lines)
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# -- the frame sweep -------------------------------------------------------

def open_sweep(setup: Setup) -> "sweep.SweepEngine":
    """Build the sweep's own station: the radio, in this process.

    No modem process is started here. The session test runs the shipped
    modem because a session is what it checks; the sweep measures frames
    below the ARQ layer and needs the raw receive stream, which the modem
    would be holding the sound card for.
    """
    from whale.transport import RadioTransport

    transport = RadioTransport(setup.radio.id, setup.config_path)
    return sweep.SweepEngine(transport, setup.mycall, setup.mode_registry,
                             setup.channel)


def stop_sweep_uninterrupted(engine) -> None:
    """Stop the sweep, finished even if Ctrl-C is pressed again during it.

    Same rule as `stop_station_uninterrupted`, for the same reason: this is
    the teardown that un-keys -- `SweepEngine.stop` closes the transport --
    so a second, impatient interrupt landing inside it is exactly the one
    that must not be honoured. There is no courtesy to drop here: the sweep
    has no session to say goodbye to, so the stop is simply repeated.
    """
    for last in (False, True):
        try:
            engine.stop()
            return
        except KeyboardInterrupt:
            if last:
                return


def _sweep_fields(transcript: Transcript, engine) -> None:
    """Fill the report's shared fields from what the sweep observed."""
    transcript.keyings = engine.keyings
    transcript.snr_db.extend(engine.receiver.snr_db)
    for row in engine.results:
        key = (row.mode_id, row.direction)
        transcript.modes[key] = transcript.modes.get(key, 0) + (
            row.sent if row.direction == "TX" else row.decoded)


def run_sweep(setup: Setup, peer: str | None, transcript: Transcript,
              engine=None) -> tuple[str, int, list]:
    """Drive one side of a frame sweep. Returns (outcome, status, results).

    The status meanings are ``whale/sweep.py``'s: a sweep that ran is a
    measurement whatever it measured, so modes that decoded nothing still
    exit 0. Only a sweep that could not measure at all -- nothing answered
    at the control mode -- or one the operator stopped part way is a 1. The
    listener's own Ctrl-C is not a failure: being stopped is how it ends.
    """
    outcome, status = "FAIL: the sweep ended before it measured anything", 1
    try:
        if engine is None:
            engine = open_sweep(setup)
    except (OSError, ValueError, LookupError) as exc:
        return f"FAIL: the radio could not be opened: {exc}", 1, []
    engine.step = transcript.step
    try:
        engine.start()
        print("Press Ctrl-C to stop the sweep; it takes the radio down unkeyed.",
              flush=True)
        run_interruptibly(engine.run_caller if peer else engine.run_listener,
                          *([peer] if peer else []))
    except sweep.SweepError as exc:
        outcome = f"FAIL: {exc}"
    except (OSError, TimeoutError) as exc:
        outcome = f"FAIL: {exc}"
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
        if peer is None:
            # The listener has no other ending: it sits until it is stopped.
            outcome = f"Stopped by the operator after {engine.sweeps} sweep(s)."
            status = 0
        else:
            outcome = "FAIL: stopped by the operator part way through the sweep"
    else:
        outcome, status = f"MEASURED: {len(engine.results)} mode result(s)", 0
    finally:
        stop_sweep_uninterrupted(engine)
        _sweep_fields(transcript, engine)
    return outcome, status, list(engine.results)


# -- entry point -----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="whale-test",
        description="Run an over-the-air check against another Whale station. "
                    "With no callsign, wait to be called.")
    ap.add_argument("callsign", nargs="?",
                    help="the station to call; omit to wait for a call")
    ap.add_argument("--sweep", action="store_true",
                    help="measure the path mode by mode instead of running a "
                         "session; both stations need it")
    ap.add_argument("--channel", default="fm", choices=sorted(policy.CHANNELS),
                    help="which channel this station is on (default: fm)")
    ap.add_argument("--config", help="application configuration TOML (or set WHALE_CONFIG)")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr)

    peer = args.callsign.upper() if args.callsign else None
    try:
        setup = preflight(args.config, args.channel)
    except PreflightError as exc:
        for problem in exc.problems:
            print(problem, file=sys.stderr)
        return 2

    try:
        # Both roles of both exercises are confirmed, the sweep's listener
        # included. It transmits too -- short control-mode frames, but
        # keyings -- and this prompt is where the operator confirms that the
        # radio, the power and the antenna are the ones they meant. A
        # station about to sit unattended for hours is the last one that
        # should key without having been looked at first.
        confirm(setup)
    except KeyboardInterrupt:
        # Nothing has been transmitted and there is no session to describe,
        # so there is nothing worth writing a report about either.
        print("\nStopped before transmitting.")
        return 1

    if args.sweep:
        transcript = Transcript()
        outcome, status, results = run_sweep(setup, peer, transcript)
        transcript.step(outcome)
        path = write_report(
            Path.cwd() / report_name(setup.mycall, datetime.now(timezone.utc)),
            setup, peer, transcript, outcome, sweep_results=results)
        print(path)
        return status

    transcript = Transcript()
    outcome = "FAIL: the run ended before a verdict"
    status = 1
    interrupted = False
    station = client = None
    try:
        station = start_station(setup, args.verbose)
        client = connect_station(station, transcript, setup.mycall)
        print("Press Ctrl-C to stop the test; again to stop it without "
              "saying goodbye.", flush=True)
        run_interruptibly(run_exercise, client, setup.mycall, peer, transcript)
    except TestFailure as exc:
        outcome = f"FAIL: {exc}"
    except TimeoutError as exc:
        outcome = f"FAIL: {exc}"
    except OSError as exc:
        outcome = f"FAIL: {exc}"
    except KeyboardInterrupt:
        # Printed rather than stepped, because the teardown below can take a
        # few seconds and the operator has just asked for it to be over.
        print("\nStopping...", flush=True)
        outcome = "FAIL: stopped by the operator"
        interrupted = True
    else:
        outcome, status = "PASS", 0
    finally:
        if station is not None:
            # A run that reached its verdict and one the operator stopped are
            # both ended politely: a session still up is worth a parting DISC,
            # which saves the far end waiting out its inactivity timeout. A
            # run that failed on air is not -- whatever went wrong there is
            # unlikely to carry a DISC either, and the grace period would be
            # spent finding that out. Either way the courtesy is bounded in
            # time, and a second Ctrl-C drops it (see
            # stop_station_uninterrupted).
            stop_station_uninterrupted(station, client, transcript,
                                       graceful=status == 0 or interrupted)

    transcript.step(outcome)
    path = write_report(Path.cwd() / report_name(setup.mycall, datetime.now(timezone.utc)),
                        setup, peer, transcript, outcome)
    print(path)
    return status


if __name__ == "__main__":
    sys.exit(main())
