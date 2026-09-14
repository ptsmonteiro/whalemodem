"""Tests for whale/modem_tui.py: no curses required.

Covers the event/log accumulator (TuiState/LogTap) and the pure render
functions. Curses drawing itself (run/_draw) isn't exercised here -- there's
no terminal in CI -- but every function that touches state or produces
strings is.
"""

import logging

import pytest

from whale import modem_tui
from whale.modem_tui import DECODED_FRAME_FORMAT, LogTap, TuiState


def make_record(msg, args=(), level=logging.INFO, name="whale.link"):
    return logging.LogRecord(name=name, level=level, pathname=__file__, lineno=1,
                              msg=msg, args=args, exc_info=None)


# --- TuiState: synthetic events -------------------------------------------

def test_ptt_event_updates_snapshot():
    state = TuiState("N0CALL", "myradio", "hf")
    state.on_event("PTT", on=True)
    assert state.snapshot().ptt_on is True
    state.on_event("PTT", on=False)
    assert state.snapshot().ptt_on is False


def test_connected_and_disconnected_events():
    state = TuiState("N0CALL", "myradio", "hf")
    state.on_event("CONNECTED", mycall="N0CALL", peer="W1AW")
    snap = state.snapshot()
    assert snap.connected is True
    assert snap.peer == "W1AW"

    state.on_event("DISCONNECTED")
    snap = state.snapshot()
    assert snap.connected is False
    assert snap.peer is None


def test_connect_failed_clears_connected():
    state = TuiState("N0CALL", "myradio", "hf")
    state.on_event("CONNECTED", mycall="N0CALL", peer="W1AW")
    state.on_event("CONNECT_FAILED")
    snap = state.snapshot()
    assert snap.connected is False
    assert snap.peer is None


def test_snr_event_updates_last_and_history():
    state = TuiState("N0CALL", "myradio", "hf")
    state.on_event("SNR", snr_db=10.0)
    state.on_event("SNR", snr_db=12.5)
    snap = state.snapshot()
    assert snap.last_snr == 12.5
    assert snap.snr_history == [10.0, 12.5]


def test_snr_history_bounded():
    state = TuiState("N0CALL", "myradio", "hf")
    for i in range(modem_tui.MAX_SNR_HISTORY + 20):
        state.on_event("SNR", snr_db=float(i))
    snap = state.snapshot()
    assert len(snap.snr_history) == modem_tui.MAX_SNR_HISTORY
    assert snap.snr_history[-1] == float(modem_tui.MAX_SNR_HISTORY + 19)


def test_bitrate_event_tracks_rx_and_tx_independently():
    state = TuiState("N0CALL", "myradio", "hf")
    state.on_event("BITRATE", direction="RX", mode_id=3, bits_per_second=175.0)
    state.on_event("BITRATE", direction="TX", mode_id=4, bits_per_second=82.0)
    snap = state.snapshot()
    assert snap.rx_mode_id == 3 and snap.rx_bps == 175.0
    assert snap.tx_mode_id == 4 and snap.tx_bps == 82.0


def test_bitrate_event_resolves_mode_name_from_registry():
    class FakeProfile:
        name = "vf12"

    class FakeRegistry:
        by_id = {3: FakeProfile()}

    state = TuiState("N0CALL", "myradio", "hf", mode_registry=FakeRegistry())
    state.on_event("BITRATE", direction="RX", mode_id=3, bits_per_second=175.0)
    assert state.snapshot().rx_mode_name == "vf12"


def test_outbound_drained_event_does_not_raise():
    state = TuiState("N0CALL", "myradio", "hf")
    state.on_event("OUTBOUND_DRAINED")  # nothing tracked; just must not raise


def test_unknown_event_does_not_raise():
    state = TuiState("N0CALL", "myradio", "hf")
    state.on_event("SOMETHING_NEW", weird=1, kwargs="ignored")


def test_reset_clears_counters_but_not_connection_state():
    state = TuiState("N0CALL", "myradio", "hf")
    state.on_event("CONNECTED", mycall="N0CALL", peer="W1AW")
    state.on_event("SNR", snr_db=9.0)
    state.note_frame("vf12", 9.0)
    state.reset()
    snap = state.snapshot()
    assert snap.connected is True
    assert snap.peer == "W1AW"
    assert snap.last_snr is None
    assert snap.snr_history == []
    assert snap.profiles == {}


# --- LogTap: structural parsing of link.py's exact format strings --------

def test_log_tap_parses_decoded_frame_record():
    state = TuiState("N0CALL", "myradio", "hf")
    tap = LogTap(state)
    record = make_record(DECODED_FRAME_FORMAT,
                          ("N0CALL", "DATA", "vf12", "decode cpu 1.2 ms", "SNR 14.3 dB (tone)"))
    tap.emit(record)
    snap = state.snapshot()
    assert "vf12" in snap.profiles
    assert snap.profiles["vf12"].count == 1
    assert snap.profiles["vf12"].last_snr == 14.3


def test_log_tap_accumulates_frame_counts_across_records():
    state = TuiState("N0CALL", "myradio", "hf")
    tap = LogTap(state)
    for snr in (10.0, 11.0, 12.0):
        record = make_record(DECODED_FRAME_FORMAT,
                              ("N0CALL", "DATA", "ofdm49", "decode cpu unmeasured",
                               f"SNR {snr:.1f} dB (carrier)"))
        tap.emit(record)
    snap = state.snapshot()
    assert snap.profiles["ofdm49"].count == 3
    assert snap.profiles["ofdm49"].last_snr == 12.0


def test_log_tap_handles_snr_unavailable():
    state = TuiState("N0CALL", "myradio", "hf")
    tap = LogTap(state)
    record = make_record(DECODED_FRAME_FORMAT,
                          ("N0CALL", "ACK", "vf12", "decode cpu unmeasured", "SNR unavailable"))
    tap.emit(record)
    snap = state.snapshot()
    assert snap.profiles["vf12"].count == 1
    assert snap.profiles["vf12"].last_snr is None


def test_log_tap_appends_every_record_to_log_lines():
    state = TuiState("N0CALL", "myradio", "hf")
    tap = LogTap(state)
    tap.emit(make_record("plain message %s", ("x",)))
    snap = state.snapshot()
    assert len(snap.log_lines) == 1
    assert "plain message x" in snap.log_lines[0]


def test_log_tap_ignores_non_matching_records_without_raising():
    state = TuiState("N0CALL", "myradio", "hf")
    tap = LogTap(state)
    tap.emit(make_record("[%s] RX %s at %s (%d body byte(s))",
                          ("N0CALL", "DATA", "vf12", 42)))
    snap = state.snapshot()
    assert snap.profiles == {}
    assert len(snap.log_lines) == 1


@pytest.mark.parametrize("msg,args", [
    (DECODED_FRAME_FORMAT, ("only", "three", "args")),  # wrong arity
    (DECODED_FRAME_FORMAT, None),  # args is None instead of a tuple
    (DECODED_FRAME_FORMAT, ("a", "b", "c", "d", "e", "f")),  # too many
    ("%s %s", (object(), object())),  # unformattable-looking args
    (None, ()),  # msg is None
])
def test_log_tap_malformed_records_never_raise(msg, args):
    state = TuiState("N0CALL", "myradio", "hf")
    tap = LogTap(state)
    record = make_record(msg, args if args is not None else ())
    if args is None:
        record.args = None
    tap.emit(record)  # must not raise
    # A ring-buffer line is still appended (emit degrades, it doesn't drop).
    assert len(state.snapshot().log_lines) == 1


def test_log_tap_handle_error_never_raises():
    state = TuiState("N0CALL", "myradio", "hf")
    tap = LogTap(state)
    tap.handleError(make_record("boom", ()))  # must not raise or print a traceback


# --- poll(): read-only, defensive attribute access ------------------------

def test_poll_defaults_when_service_has_no_link():
    state = TuiState("N0CALL", "myradio", "hf")
    state.poll(object())  # no _link attribute at all
    snap = state.snapshot()
    assert snap.rx_overflows == 0
    assert snap.tx_underflows == 0
    assert snap.qualification_metrics == {}
    assert snap.poll_error is None


def test_poll_reads_transport_and_link_counters():
    class FakeTransport:
        rx_overflows = 3
        tx_underflows = 1

    class FakeLink:
        transport = FakeTransport()
        qualification_metrics = {"data_attempts": 5, "retransmissions": 1,
                                  "ack_timeouts": 0, "duplicate_data": 0, "mode_changes": []}

    class FakeService:
        _link = FakeLink()

    state = TuiState("N0CALL", "myradio", "hf")
    state.poll(FakeService())
    snap = state.snapshot()
    assert snap.rx_overflows == 3
    assert snap.tx_underflows == 1
    assert snap.qualification_metrics["data_attempts"] == 5


def test_poll_swallows_attribute_access_exceptions():
    class ExplodingLink:
        @property
        def transport(self):
            raise RuntimeError("boom")

    class FakeService:
        _link = ExplodingLink()

    state = TuiState("N0CALL", "myradio", "hf")
    state.poll(FakeService())  # must not raise
    snap = state.snapshot()
    assert snap.rx_overflows == 0
    assert snap.poll_error is not None


# --- pure renderers: never exceed width, expected labels present ---------

def _snapshot_with_data():
    state = TuiState("N0CALL", "myradio", "hf")
    state.on_event("PTT", on=True)
    state.on_event("CONNECTED", mycall="N0CALL", peer="W1AW")
    state.on_event("SNR", snr_db=11.5)
    state.on_event("BITRATE", direction="RX", mode_id=3, bits_per_second=175.0)
    state.on_event("BITRATE", direction="TX", mode_id=4, bits_per_second=82.0)
    state.note_frame("vf12", 11.5)
    state.note_frame("ofdm49", 9.0)
    tap = LogTap(state)
    for i in range(5):
        tap.emit(make_record("line %d", (i,)))
    state.poll(object())
    return state.snapshot()


@pytest.mark.parametrize("width", [80, 120])
def test_render_header_within_width_and_has_labels(width):
    snap = _snapshot_with_data()
    lines = modem_tui.render_header(snap, width)
    assert lines
    for line in lines:
        assert len(line) <= width
    joined = "\n".join(lines)
    assert "N0CALL" in joined
    assert "myradio" in joined
    assert "hf" in joined
    assert "W1AW" in joined
    assert "PTT ON" in joined
    assert "RX overflows" in joined
    assert "TX underflows" in joined


@pytest.mark.parametrize("width", [80, 120])
def test_render_link_within_width_and_has_labels(width):
    snap = _snapshot_with_data()
    lines = modem_tui.render_link(snap, width)
    assert lines
    for line in lines:
        assert len(line) <= width
    joined = "\n".join(lines)
    assert "Last SNR" in joined
    assert "Profile" in joined
    assert "vf12" in joined
    assert "ofdm49" in joined


@pytest.mark.parametrize("width", [80, 120])
def test_render_log_within_width_and_returns_tail(width):
    snap = _snapshot_with_data()
    lines = modem_tui.render_log(snap, width, height=3)
    assert len(lines) == 3
    for line in lines:
        assert len(line) <= width
    # most recent lines, in order
    assert lines[-1].endswith("line 4")


def test_render_log_zero_height_returns_empty():
    snap = _snapshot_with_data()
    assert modem_tui.render_log(snap, 80, height=0) == []


def test_render_header_handles_empty_state():
    state = TuiState("", "", "")
    snap = state.snapshot()
    lines = modem_tui.render_header(snap, 80)
    for line in lines:
        assert len(line) <= 80
    assert any("listening" in line for line in lines)


def test_sparkline_handles_flat_and_empty_series():
    assert modem_tui._sparkline([]) == ""
    flat = modem_tui._sparkline([5.0, 5.0, 5.0])
    assert len(flat) == 3


def test_clip_never_exceeds_width():
    assert len(modem_tui._clip("x" * 500, 10)) == 10
    assert modem_tui._clip("short", 0) == ""
