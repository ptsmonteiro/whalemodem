"""``whale-test``: argument surface, preflight, report, and a full pass.

Two tests matter most. The last one is the exercise itself: two stations,
the real command and data ports, the real link and modulation, and
``run_exercise`` driving both sides exactly as the shipped command does --
only the sound cards are replaced, the same way ``tests/test_audio_e2e.py``
replaces them. The other is the modem's lifetime: whale-test runs the
shipped modem as a child process, so starting it, reading its report fields
off its command port and getting it to stop unkeyed are tested against a
real child process, standing in for the modem with a script rather than a
radio.
"""

from __future__ import annotations

import _thread
import socket
import subprocess
import sys
import threading
import time

import pytest

from support.audio_link import DirectionalAudioLink, _close_client, _server
from whale import link, policy
from whale.config import Config, save_config
from whale.hw import audio_io
from whale.hw.radios import Radio
from whale.mode_qualification import registry
from whale import sweep
from whale.test_cli import (PreflightError, Setup, Station, Transcript,
                            DEFAULT_PAYLOAD_SIZE, build_parser, connect_station, main,
                            modem_argv, preflight, report_name, reserve_ports,
                            run_exercise, run_interruptibly, run_listener, run_sweep,
                            start_station, stop_modem, stop_station,
                            stop_station_uninterrupted, write_report)

from acceptance_test import StationClient

CALL = "F4JAW"


def _config(tmp_path, **kwargs):
    path = tmp_path / "config.toml"
    radio = Radio("bench", "Bench Radio", "Mic In", "Speakers", "vox",
                  frozenset({"fm", "hf"}))
    save_config(path, Config(CALL, None, {"bench": radio},
                             default_fm_radio="bench", default_hf_radio="bench",
                             **kwargs))
    return path


def _fake_devices(monkeypatch, *, find=None, check=None):
    """Stand in for the sound cards, which a test host does not have."""
    monkeypatch.setattr(audio_io, "find_device",
                        find or (lambda name, kind: 0 if kind == "output" else 1))
    monkeypatch.setattr(audio_io, "check_device",
                        check or (lambda device, kind, samplerate=None: None))


# -- argument surface ------------------------------------------------------

def test_defaults_wait_to_be_called_on_the_fm_channel():
    args = build_parser().parse_args([])
    assert args.callsign is None
    assert args.channel == "fm"
    assert args.config is None
    assert args.verbose is False
    assert args.size == 10 * 1024 == DEFAULT_PAYLOAD_SIZE


def test_a_callsign_selects_the_calling_side():
    args = build_parser().parse_args(
        ["STA2", "--channel", "hf", "--size", "2048", "-v"])
    assert (args.callsign, args.channel, args.size, args.verbose) == (
        "STA2", "hf", 2048, True)


def test_the_channel_choices_are_the_configured_channels():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--channel", "moon"])


def test_payload_size_cannot_be_negative():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--size", "-1"])


def test_exercise_uses_the_selected_payload_size():
    class Client:
        def __init__(self):
            self.sent = []
            self.received = []

        def send_cmd(self, line):
            self.sent.append(line)

        def wait_for(self, prefix, timeout):
            return prefix

        def open_data(self):
            pass

        def send_data(self, data):
            self.sent.append(data)

        def recv_data(self, size, timeout):
            self.received.append((size, timeout))
            from acceptance_test import PAYLOAD_TAG_BA, payload
            return payload(PAYLOAD_TAG_BA, size)

    client = Client()
    run_exercise(client, "STA1", "STA2", Transcript(), payload_size=37)

    assert [len(item) for item in client.sent if isinstance(item, bytes)] == [37]
    assert client.received[0][0] == 37


def test_exercise_reports_receive_progress(capsys):
    class Client:
        def send_cmd(self, line):
            pass

        def wait_for(self, prefix, timeout):
            return prefix

        def open_data(self):
            pass

        def send_data(self, data):
            pass

        def recv_data_progress(self, size, timeout, on_progress):
            from acceptance_test import PAYLOAD_TAG_BA, payload
            on_progress(size // 2)
            on_progress(size)
            return payload(PAYLOAD_TAG_BA, size)

    run_exercise(Client(), "STA1", "STA2", Transcript(), payload_size=100)

    output = capsys.readouterr().out
    assert "Return transfer started: receiving 100 bytes." in output
    assert "50 of 100 bytes received." in output
    assert "100 of 100 bytes received." in output


def test_listener_returns_after_failed_and_successful_tests(monkeypatch):
    import whale.test_cli as test_cli

    transcript = Transcript()
    attempts = []

    class Client:
        data = None

        def __init__(self):
            self.commands = []

        def send_cmd(self, line):
            self.commands.append(line)

        def wait_for(self, prefix, timeout):
            transcript.on_status("DISCONNECTED")
            return "DISCONNECTED"

    def exercise(client, mycall, peer, transcript_, connect_timeout,
                 transfer_timeout, payload_size):
        attempts.append(payload_size)
        if len(attempts) == 1:
            transcript_.on_status(f"CONNECTED {mycall} STA2 0")
            raise test_cli.TestFailure("payload mismatch")
        if len(attempts) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(test_cli, "run_exercise", exercise)
    client = Client()
    with pytest.raises(KeyboardInterrupt):
        run_listener(client, "STA1", transcript, payload_size=2048)

    assert attempts == [2048, 2048, 2048]
    assert client.commands == ["ABORT"]
    assert sum("Returning to listening" in line for line in transcript.lines) == 2
    assert any("Test failed: payload mismatch" in line for line in transcript.lines)
    assert any("Test passed." in line for line in transcript.lines)


def test_the_sweep_is_one_flag_on_both_roles():
    """`--sweep` alone listens; with a callsign it drives."""
    assert build_parser().parse_args(["--sweep"]).sweep is True
    args = build_parser().parse_args(["--sweep", "STA2"])
    assert (args.sweep, args.callsign) == (True, "STA2")
    assert build_parser().parse_args([]).sweep is False


@pytest.mark.parametrize("extra", [["--cmd-port", "8300"],
                                   ["--chat"], ["STA2", "STA3"]])
def test_no_other_flags_are_accepted(extra):
    """The small surface is the feature; keep new knobs out by test."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(extra)


# -- preflight -------------------------------------------------------------

def test_preflight_resolves_the_whole_station(tmp_path, monkeypatch):
    _fake_devices(monkeypatch)
    setup = preflight(str(_config(tmp_path)), "fm")
    assert setup.mycall == CALL
    assert setup.radio.id == "bench"
    assert setup.channel is policy.FM
    assert setup.mode_registry.supported_ids
    assert (setup.output_device, setup.input_device) == (0, 1)


def test_missing_configuration_is_one_sentence(tmp_path):
    with pytest.raises(PreflightError) as caught:
        preflight(str(tmp_path / "absent.toml"), "fm")
    assert len(caught.value.problems) == 1
    assert "whale-configure" in caught.value.problems[0]


def test_missing_audio_device_is_reported(tmp_path, monkeypatch):
    def missing(name, kind):
        raise LookupError(f"no {kind} device matching {name!r}")

    _fake_devices(monkeypatch, find=missing)
    with pytest.raises(PreflightError) as caught:
        preflight(str(_config(tmp_path)), "fm")
    assert "audio devices" in caught.value.problems[0]


def test_unusable_audio_format_is_reported(tmp_path, monkeypatch):
    def unusable(device, kind, samplerate=None):
        raise OSError("Invalid sample rate")

    _fake_devices(monkeypatch, check=unusable)
    with pytest.raises(PreflightError) as caught:
        preflight(str(_config(tmp_path)), "fm")
    assert len(caught.value.problems) == 2  # one per direction
    assert "Invalid sample rate" in caught.value.problems[0]


def test_a_preflight_failure_exits_2_before_transmitting(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # A station that never starts must not be confirmed, either: reaching
    # input() here would hang the run rather than fail it.
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(
        "confirmation was asked for after a failed preflight"))
    assert main(["--config", str(tmp_path / "absent.toml")]) == 2
    assert "whale-configure" in capsys.readouterr().err
    assert not list(tmp_path.glob("whale-report-*.txt"))


# -- report ----------------------------------------------------------------

def _setup(tmp_path):
    return Setup(str(tmp_path / "config.toml"), CALL, "fm", policy.FM,
                 registry("fm", "default", policy.FM.max_useful_frame_seconds),
                 Radio("bench", "Bench Radio", "Mic In", "Speakers", "vox",
                       frozenset({"fm"})),
                 0, 1)


def test_report_name_carries_the_callsign_and_a_utc_stamp():
    from datetime import datetime, timezone

    when = datetime(2026, 9, 18, 7, 5, 3, tzinfo=timezone.utc)
    assert report_name("F4JAW-2", when) == "whale-report-F4JAW-2-20260918T070503Z.txt"


def test_the_report_stands_alone(tmp_path):
    """Every field comes off the modem's command port, verbatim as sent."""
    transcript = Transcript()
    transcript.step("Calling STA2 as F4JAW...")
    transcript.on_status("PTT ON")
    transcript.on_status("PTT OFF")
    transcript.on_status("SN 11.2")
    transcript.on_status("SN 13.8")
    transcript.on_status("BITRATE (23)  700 bps TX")
    transcript.on_status(f"CONNECTED {CALL} STA2 0")

    path = write_report(tmp_path / "report.txt", _setup(tmp_path), "STA2",
                        transcript, "PASS")
    text = path.read_text(encoding="utf-8")
    assert "Result:      PASS" in text
    assert "calling STA2" in text
    assert "Bench Radio" in text and "Mic In" in text and "Speakers" in text
    assert "vox" in text
    assert "23 TX x1" in text
    assert "Keyings:     1" in text
    assert "min 11.2 dB, mean 12.5 dB, max 13.8 dB" in text
    assert "Calling STA2 as F4JAW..." in text
    assert "modem: CONNECTED peer=STA2" in text


def test_the_report_says_so_when_there_is_nothing_to_report(tmp_path):
    text = write_report(tmp_path / "report.txt", _setup(tmp_path), None,
                        Transcript(), "FAIL: no link").read_text(encoding="utf-8")
    assert "Result:      FAIL: no link" in text
    assert "Role:        answering" in text
    assert "no decoded DATA bursts reported an SNR" in text
    assert "none: no DATA burst was keyed or decoded" in text


def test_a_status_line_it_cannot_read_costs_a_field_not_the_run():
    """on_status runs on the client's reader thread; raising there is fatal."""
    transcript = Transcript()
    for line in ("IAMALIVE", "OK", "BUFFER 0", "SN", "SN not-a-number",
                 "BITRATE (x)  700 bps TX", "BITRATE", "", "PTT"):
        transcript.on_status(line)
    assert (transcript.keyings, transcript.snr_db, transcript.modes) == (0, [], {})
    assert transcript.lines == []


def test_the_session_state_follows_the_command_port():
    """Whether a parting DISC is owed is read off the wire, not assumed."""
    transcript = Transcript()
    assert transcript.connected is False
    transcript.on_status(f"CONNECTED {CALL} STA2 0")
    assert transcript.connected is True
    transcript.on_status("DISCONNECTED")
    assert transcript.connected is False
    assert [line.split("modem: ")[1] for line in transcript.lines
            if "modem: " in line] == [
        "CONNECTED peer=STA2", "DISCONNECTED"]
    assert any("Connected to STA2." in line for line in transcript.lines)


def test_status_events_become_concise_progress_messages(capsys):
    transcript = Transcript(mode_registry=registry(
        "fm", "default", policy.FM.max_useful_frame_seconds))
    mode = transcript.mode_registry.modes[0]

    transcript.on_status(f"BITRATE ({mode.mode_id})  700 bps TX")
    transcript.on_status(f"BITRATE ({mode.mode_id})  700 bps TX")
    transcript.outbound_bytes = 10_240
    transcript.inbound_bytes = 10_240
    transcript.on_status("WHALE PROGRESS TX 2048 10240")
    transcript.on_status("WHALE PROGRESS RX 3072")
    transcript.on_status("WHALE PROGRESS TX 10240 10240")
    transcript.on_status("BUFFER 0")

    output = capsys.readouterr().out
    assert output.count("TX: switched to") == 1
    assert mode.name in output and "700 bit/s" in output
    assert "2,048 of 10,240 bytes sent." in output
    assert "Return transfer started: receiving 10,240 bytes." in output
    assert "3,072 of 10,240 bytes received." in output
    assert "10,240 of 10,240 bytes sent." in output
    assert output.count("10,240 of 10,240 bytes sent.") == 1


def test_an_on_air_failure_exits_1_with_a_report(tmp_path, monkeypatch, capsys):
    """A run that got past preflight always leaves a file to send back."""
    import whale.test_cli as test_cli

    monkeypatch.chdir(tmp_path)
    _fake_devices(monkeypatch)
    prompts = []
    monkeypatch.setattr("builtins.input", prompts.append)

    def no_radio(setup, verbose=False):
        raise test_cli.TestFailure("the radio never came up")

    monkeypatch.setattr(test_cli, "start_station", no_radio)
    assert main(["--config", str(_config(tmp_path))]) == 1

    assert prompts, "the operator was not asked to confirm before transmitting"
    out = capsys.readouterr().out
    assert "FAIL: the radio never came up" in out
    reports = list(tmp_path.glob(f"whale-report-{CALL}-*.txt"))
    assert len(reports) == 1
    # The report's path is the last thing printed, so it is the one line a
    # helper has to notice.
    assert out.strip().splitlines()[-1] == str(reports[0])
    assert "FAIL: the radio never came up" in reports[0].read_text(encoding="utf-8")


# -- the frame sweep -------------------------------------------------------
#
# What the sweep measures is tested in tests/test_frame_sweep.py, against
# real waveforms. These are about the command around it: that the radio is
# never opened before preflight and the confirmation, what the run exits
# with, and that the report carries the per-mode table.

class _StubEngine:
    """A sweep that has already happened, so the command can be tested."""

    def __init__(self, results=(), raises=None):
        self.results = list(results)
        self.keyings = 3
        self.sweeps = 1
        self.receiver = type("R", (), {"snr_db": [5.0, 7.0]})()
        self.step = lambda text: None
        self.stopped = 0
        self._raises = raises

    def start(self):
        pass

    def run_caller(self, peer):
        if self._raises is not None:
            raise self._raises

    def run_listener(self):
        if self._raises is not None:
            raise self._raises

    def stop(self):
        self.stopped += 1


def _sweep_main(tmp_path, monkeypatch, engine, argv):
    import whale.test_cli as test_cli

    monkeypatch.chdir(tmp_path)
    _fake_devices(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(test_cli, "open_sweep", lambda setup: engine)
    return main(["--config", str(_config(tmp_path))] + argv)


DEAD_MODE = sweep.ModeResult("TX", 18, "vf12", 5, 0, 1, 4)
LIVE_MODE = sweep.ModeResult("TX", 23, "vf14-4", 5, 5, 0, 0, 3.0, 6.5, 9.0)


def test_a_sweep_that_found_a_dead_mode_still_measured_the_path(tmp_path, monkeypatch,
                                                                capsys):
    """A mode that carries nothing is a result, not a failed run."""
    engine = _StubEngine([LIVE_MODE, DEAD_MODE])
    assert _sweep_main(tmp_path, monkeypatch, engine, ["--sweep", "STA2"]) == 0
    assert engine.stopped, "the radio was left up"

    report = list(tmp_path.glob("whale-report-*.txt"))[0].read_text(encoding="utf-8")
    assert "Per-mode frame sweep" in report
    assert "vf14-4" in report and "vf12" in report
    assert "sweeping STA2" in report
    assert "not a lab measurement" in report
    assert "MEASURED: 2 mode result(s)" in report


def test_a_sweep_nobody_answered_is_not_a_measurement(tmp_path, monkeypatch, capsys):
    engine = _StubEngine(raises=sweep.SweepError("STA2 did not answer"))
    assert _sweep_main(tmp_path, monkeypatch, engine, ["--sweep", "STA2"]) == 1
    assert "did not answer" in capsys.readouterr().out


def test_stopping_the_listener_is_how_it_ends(tmp_path, monkeypatch):
    """Ctrl-C is the idle listener's only ending, so it is not a failure."""
    engine = _StubEngine(raises=KeyboardInterrupt())
    assert _sweep_main(tmp_path, monkeypatch, engine, ["--sweep"]) == 0
    report = list(tmp_path.glob("whale-report-*.txt"))[0].read_text(encoding="utf-8")
    assert "Stopped by the operator after 1 sweep(s)." in report
    assert "Role:        swept (listening)" in report


def test_stopping_a_sweep_part_way_is_a_failure(tmp_path, monkeypatch):
    engine = _StubEngine(results=[LIVE_MODE], raises=KeyboardInterrupt())
    assert _sweep_main(tmp_path, monkeypatch, engine, ["--sweep", "STA2"]) == 1


def test_the_listener_is_asked_to_confirm_before_it_keys(tmp_path, monkeypatch):
    """It transmits too -- READY and RESULT are keyings, unattended ones."""
    import whale.test_cli as test_cli

    monkeypatch.chdir(tmp_path)
    _fake_devices(monkeypatch)
    monkeypatch.setattr(test_cli, "open_sweep", lambda setup: pytest.fail(
        "the radio was opened before the operator confirmed"))

    def interrupted(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    assert main(["--config", str(_config(tmp_path)), "--sweep"]) == 1
    assert not list(tmp_path.glob("whale-report-*.txt"))


def test_a_sweep_cannot_start_without_a_station(tmp_path, capsys, monkeypatch):
    """Preflight comes first for the sweep too: exit 2, nothing transmitted."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(
        "confirmation was asked for after a failed preflight"))
    assert main(["--config", str(tmp_path / "absent.toml"), "--sweep"]) == 2
    assert "whale-configure" in capsys.readouterr().err


def test_a_radio_that_will_not_open_ends_the_sweep(tmp_path, monkeypatch):
    """The failure the preflight cannot catch: the device itself."""
    transcript = Transcript()

    def no_radio(setup):
        raise OSError("the sound card is in use")

    import whale.test_cli as test_cli

    monkeypatch.setattr(test_cli, "open_sweep", no_radio)
    outcome, status, results = run_sweep(_setup(tmp_path), "STA2", transcript)
    assert status == 1 and results == []
    assert "sound card is in use" in outcome


# -- Ctrl-C ----------------------------------------------------------------

def _interrupt_main_after(delay=0.1):
    """Deliver a SIGINT to the main thread, exactly as Ctrl-C does."""
    timer = threading.Timer(delay, _thread.interrupt_main)
    timer.daemon = True
    timer.start()
    return timer


def test_run_interruptibly_returns_when_the_work_finishes():
    ran = []
    run_interruptibly(ran.append, "done")
    assert ran == ["done"]


def test_run_interruptibly_reraises_what_the_work_raised():
    def boom(message):
        raise ValueError(message)

    with pytest.raises(ValueError, match="no carrier"):
        run_interruptibly(boom, "no carrier")


def test_ctrl_c_reaches_the_main_thread_while_the_work_blocks():
    """The point of the worker thread: a blocked exercise is still stoppable.

    ``threading.Event().wait()`` stands in for the blocking socket receive a
    transfer spends its minutes in -- on Windows that receive never sees the
    interrupt, which is why the main thread must not be the one inside it.
    """
    _interrupt_main_after()
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_interruptibly(threading.Event().wait)
    assert time.monotonic() - started < 5


def test_ctrl_c_at_the_confirmation_leaves_nothing_behind(tmp_path, monkeypatch, capsys):
    """Nothing was transmitted, so there is no session to report on."""
    monkeypatch.chdir(tmp_path)
    _fake_devices(monkeypatch)

    def interrupted(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    assert main(["--config", str(_config(tmp_path))]) == 1
    assert "Stopped before transmitting" in capsys.readouterr().out
    assert not list(tmp_path.glob("whale-report-*.txt"))


class _StubProcess:
    """A modem process that is alive until it is asked, politely, to stop."""

    def __init__(self, *, deaf=False):
        self.signals = []
        self.kills = 0
        self.alive = True
        self._deaf = deaf  # ignores the signal, like a modem mid-burst

    def poll(self):
        return None if self.alive else 0

    def send_signal(self, number):
        self.signals.append(number)
        self.alive = self._deaf

    def wait(self, timeout=None):
        if self.alive:
            raise subprocess.TimeoutExpired("whale-server", timeout)
        return 0

    def kill(self):
        self.kills += 1
        self.alive = False


class _StubClient:
    """Enough StationClient for a teardown: what was sent, and closing."""

    def __init__(self):
        self.sent = []
        self.closed = 0
        self.data = None
        client = self

        class _Sock:
            def shutdown(self, how):
                pass

            def close(self):
                client.closed += 1

        self.cmd = _Sock()

    def send_cmd(self, line):
        self.sent.append(line)

    def wait_for(self, prefix, timeout):
        return prefix


def _stub_station(**kwargs):
    return Station(_StubProcess(**kwargs), 8300, 8301)


def _stubbed_run(monkeypatch, tmp_path, exercise):
    """A whale-test run with a stub modem process and a stub client."""
    import whale.test_cli as test_cli

    monkeypatch.chdir(tmp_path)
    _fake_devices(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    station, client = _stub_station(), _StubClient()
    monkeypatch.setattr(test_cli, "start_station",
                        lambda setup, verbose=False: station)
    monkeypatch.setattr(test_cli, "connect_station",
                        lambda station_, transcript, mycall, **kw: client)
    monkeypatch.setattr(test_cli, "run_exercise", exercise)
    return station, client


def test_ctrl_c_during_the_exercise_stops_the_station_and_reports(tmp_path, monkeypatch,
                                                                  capsys):
    """The whole point: the client closes and the modem process ends.

    The first Ctrl-C is the polite one. It says goodbye to the far end --
    one more keying, bounded by the grace period -- so that the station
    left listening is not tied up waiting out its inactivity timeout.
    """
    def blocked(client, mycall, peer, transcript, **kw):
        # Stands in for an exercise waiting on the air with a session up;
        # the interrupt arrives while it is blocked, the case that used to
        # hang -- and the session being up is what a parting DISC would be
        # sent for.
        transcript.on_status(f"CONNECTED {CALL} STA2 0")
        threading.Event().wait()

    station, client = _stubbed_run(monkeypatch, tmp_path, blocked)

    _interrupt_main_after()
    assert main(["--config", str(_config(tmp_path))]) == 0

    assert not station.process.alive, "the modem process was left running"
    assert len(station.process.signals) == 1, "the modem was not asked to stop"
    assert station.process.kills == 0, "the modem was killed without being asked"
    assert client.sent == ["DISCONNECT"], (
        "the first Ctrl-C did not say goodbye; the far end is left waiting "
        "out its inactivity timeout")
    assert client.closed, "the client's command socket was left open"
    out = capsys.readouterr().out
    assert "Stopping..." in out
    reports = list(tmp_path.glob(f"whale-report-{CALL}-*.txt"))
    assert len(reports) == 1
    assert "Stopped by the operator; no longer listening" in reports[0].read_text(
        encoding="utf-8")


def test_a_pass_with_a_session_still_up_says_goodbye_before_stopping(tmp_path,
                                                                    monkeypatch):
    """The other half of the distinction: a passing run is allowed to key."""
    selected_sizes = []

    def passed(client, mycall, peer, transcript, connect_timeout,
               transfer_timeout, payload_size):
        if selected_sizes:
            raise KeyboardInterrupt
        selected_sizes.append(payload_size)
        transcript.on_status(f"CONNECTED {CALL} STA2 0")

    station, client = _stubbed_run(monkeypatch, tmp_path, passed)
    assert main(["--config", str(_config(tmp_path)), "--size", "2048"]) == 0
    assert selected_sizes == [2048]
    assert client.sent == ["DISCONNECT"]
    assert not station.process.alive


def test_a_second_ctrl_c_during_the_goodbye_drops_it():
    """The second interrupt means now, so the courtesy is not retried."""
    class _Impatient(_StubClient):
        def wait_for(self, prefix, timeout):
            raise KeyboardInterrupt   # the operator, during the grace period

    client, station, transcript = _Impatient(), _stub_station(), Transcript()
    transcript.on_status(f"CONNECTED {CALL} STA2 0")

    stop_station_uninterrupted(station, client, transcript)

    assert client.sent == ["DISCONNECT"], "the dropped goodbye was sent again"
    assert client.closed, "the client was left open"
    assert not station.process.alive, "the modem process was left running"
    assert station.process.kills == 0, "the modem was killed without being asked"


def test_a_goodbye_the_air_does_not_carry_is_bounded():
    """A grace period, not another transfer budget: it expires and moves on."""
    class _Unanswered(_StubClient):
        def wait_for(self, prefix, timeout):
            raise TimeoutError(prefix)

    client, station, transcript = _Unanswered(), _stub_station(), Transcript()
    transcript.on_status(f"CONNECTED {CALL} STA2 0")

    stop_station(station, client, transcript)

    assert client.sent == ["DISCONNECT"]
    assert client.closed and not station.process.alive


def test_a_modem_that_will_not_stop_is_killed():
    """A kill cannot unkey a radio, so it is the last resort, not the first."""
    station = _stub_station(deaf=True)
    stop_modem(station, timeout=0.01)
    assert station.process.signals, "the modem was killed without being asked first"
    assert station.process.kills == 1
    assert not station.process.alive


def test_a_second_ctrl_c_does_not_abandon_the_teardown(monkeypatch):
    """The teardown is what unkeys the radio, so it has to finish."""
    import whale.test_cli as test_cli

    attempts = []

    def stop(station, timeout=None, *, force=False):
        attempts.append(force)
        if len(attempts) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(test_cli, "stop_modem", stop)
    stop_station_uninterrupted(_stub_station(), None, Transcript())
    assert attempts == [False, True], (
        "the retry after a second Ctrl-C did not tell the modem to skip its "
        "own goodbye too")


# -- the modem process -----------------------------------------------------

def test_the_modem_command_is_the_shipped_server(tmp_path):
    """Nothing about the station is left to the child's own defaults."""
    setup = _setup(tmp_path)
    argv = modem_argv(setup, 8300, 8301, verbose=True)
    assert argv[:3] == [sys.executable, "-m", "whale.vara_server"]
    for flag, value in (("--radio", "bench"), ("--channel", "fm"),
                        ("--host", "127.0.0.1"), ("--cmd-port", "8300"),
                        ("--data-port", "8301"), ("--config", setup.config_path)):
        assert argv[argv.index(flag) + 1] == value
    assert "-v" in argv
    assert "-v" not in modem_argv(setup, 8300, 8301)


def test_reserved_ports_are_two_free_ports(tmp_path):
    """Two runs on one machine must not be handed the same pair."""
    first, second = reserve_ports(), reserve_ports()
    assert len({*first, *second}) == 4
    for port in (*first, *second):
        # Released, so the modem process can bind them.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))


#: A modem process for tests: no radio, no whale, just the two ports and a
#: handful of the status lines the real one pushes. It stays up until it is
#: asked to stop, and says on stderr that it did -- which is what proves the
#: ask, not the kill, is what ended it.
STUB_MODEM = '''
import signal, socket, sys, time


def stop(signum, frame):
    raise KeyboardInterrupt        # what the real modem's handler does: stop


for name in ("SIGTERM", "SIGBREAK"):     # SIGBREAK is CTRL_BREAK_EVENT
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), stop)

cmd_port, data_port = int(sys.argv[1]), int(sys.argv[2])
listeners = []
for port in (cmd_port, data_port):
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    listeners.append(listener)
conn, _ = listeners[0].accept()
for line in ("PTT ON", "SN 9.5", "BITRATE (7)  300 bps RX", "PTT OFF"):
    conn.sendall((line + "\\r").encode("ascii"))
try:
    while True:
        time.sleep(0.05)
except KeyboardInterrupt:      # Windows: what CTRL_BREAK_EVENT becomes
    pass
sys.stderr.write("stub modem unkeyed and stopped\\n")
'''

STUB_DEAD_MODEM = '''
import sys
sys.stderr.write("no radio here\\n")
raise SystemExit(3)
'''


def _stub_modem_argv(monkeypatch, tmp_path, source, name="stub_modem.py"):
    import whale.test_cli as test_cli

    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(test_cli, "modem_argv",
                        lambda setup, cmd_port, data_port, verbose=False:
                        [sys.executable, str(path), str(cmd_port), str(data_port)])
    return path


def _wait_until(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_the_modem_is_a_child_process_reached_over_tcp(tmp_path, monkeypatch):
    """Start it, connect to it as an application would, read its report
    fields off the command port, and get it to stop without killing it."""
    _stub_modem_argv(monkeypatch, tmp_path, STUB_MODEM)
    transcript = Transcript()
    client = None
    station = start_station(_setup(tmp_path))
    try:
        assert station.process.poll() is None
        client = connect_station(station, transcript, CALL)
        assert _wait_until(lambda: transcript.modes), "no status lines arrived"
        assert transcript.keyings == 1
        assert transcript.snr_db == [9.5]
        assert transcript.modes == {(7, "RX"): 1}
    finally:
        stop_station(station, client, transcript, graceful=False)

    assert station.process.poll() is not None, "the modem process outlived the run"
    assert _wait_until(lambda: any("unkeyed and stopped" in line
                                   for line in station.log), timeout=5), (
        "the modem was killed rather than asked to stop; a killed modem "
        "never takes PTT down")


def test_the_modem_answers_a_stop_signal_by_unkeying():
    """The other end of the contract, in whale-server: being asked to stop
    runs the server's own stop(), and never a kill that would leave the
    radio keyed. A kill-only teardown is what this rules out."""
    import signal

    from whale.vara_server import stop_on_signals

    stopped = []

    class _Server:
        def stop(self, *, graceful=True, timeout=None):
            stopped.append(graceful)
            return True

    installed = [name for name in ("SIGTERM", "SIGINT", "SIGBREAK")
                 if hasattr(signal, name)]
    previous = {name: signal.getsignal(getattr(signal, name)) for name in installed}
    try:
        stop_on_signals(_Server())
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(getattr(signal, installed[0]))
    finally:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)
    assert stopped == [True], "the first signal was not the polite one"


def test_a_modem_that_dies_on_startup_fails_the_run_with_its_own_words(tmp_path,
                                                                      monkeypatch):
    _stub_modem_argv(monkeypatch, tmp_path, STUB_DEAD_MODEM)
    import whale.test_cli as test_cli

    station = start_station(_setup(tmp_path))
    with pytest.raises(test_cli.TestFailure) as caught:
        connect_station(station, Transcript(), CALL, timeout=10)
    assert "status 3" in str(caught.value)
    assert "no radio here" in str(caught.value)


# -- the whole exercise, over paired audio ---------------------------------

def test_two_stations_pass_the_exercise_over_paired_audio(monkeypatch):
    """Both sides of ``run_exercise``, end to end, with no radios.

    This is the shipped command's exercise verbatim -- the same payloads,
    the same command sequence, the same byte-for-byte verification -- run
    against two real StationServers over the simulated audio path.
    """
    monkeypatch.setattr(link, "TX_TURNAROUND_DELAY", 0.02)
    monkeypatch.setattr(link, "DECODE_POLL_INTERVAL", 0.01)
    pair = DirectionalAudioLink()

    transcripts = {"STA1": Transcript(), "STA2": Transcript()}
    servers, threads, clients = [], [], []
    try:
        for transport, call in ((pair.a, "STA1"), (pair.b, "STA2")):
            server, thread, _ = _server(transport, call, None, policy.FM)
            servers.append(server)
            threads.append(thread)
            # The same observer whale-test attaches, so the transcript is
            # fed here by real status lines off a real command port rather
            # than by hand -- the only source it has once the modem is a
            # separate process.
            clients.append(StationClient(call, "127.0.0.1",
                                         server.cmd_port, server.data_port,
                                         on_status=transcripts[call].on_status))
        caller, answerer = clients

        results: dict[str, BaseException | None] = {}

        def side(name, client, mycall, peer, delay=0.0):
            threading.Event().wait(delay)
            try:
                run_exercise(client, mycall, peer, transcripts[name],
                             connect_timeout=60, transfer_timeout=180)
                results[name] = None
            except BaseException as exc:  # noqa: BLE001 - reported by the assert
                results[name] = exc

        # The answering station is started first and given a moment to get
        # its LISTEN ON in; a CONNECT that still beats it is retried by the
        # link, so this is a courtesy rather than a synchronisation point.
        sides = [
            threading.Thread(target=side, args=("STA2", answerer, "STA2", None),
                             daemon=True),
            threading.Thread(target=side, args=("STA1", caller, "STA1", "STA2", 0.5),
                             daemon=True),
        ]
        for thread in sides:
            thread.start()
        for thread in sides:
            thread.join(timeout=300)

        assert results.get("STA1") is None, results.get("STA1")
        assert results.get("STA2") is None, results.get("STA2")
        # Both sides recorded a session they could put in a report.
        for transcript in transcripts.values():
            assert any("Payload verified" in line for line in transcript.lines)
            assert any("modem: CONNECTED" in line for line in transcript.lines)
            assert any("of 10,240 bytes sent" in line for line in transcript.lines)
            assert any("of 10,240 bytes received" in line for line in transcript.lines)
            assert transcript.keyings > 0 and transcript.modes
            assert transcript.snr_db and "observation(s)" in transcript.snr_summary()
    finally:
        for client in clients:
            _close_client(client)
        for server in servers:
            server.stop()
        for thread in threads:
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
