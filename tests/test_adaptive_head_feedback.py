"""Deterministic protocol-v3 tests for in-session head-timing feedback."""

from whale import afsk, link
from whale.modes import vf3, vf3_mode


class _NullTransport:
    def start_receiving(self): pass
    def stop_receiving(self): pass
    def is_transmitting(self): return False


def _connected(call="STA1"):
    item = link.Link(_NullTransport(), call)
    item.state = "CONNECTED"
    item._tx_head_seconds = 0.42
    return item


def test_later_worse_observation_increases_padding(monkeypatch):
    monkeypatch.setattr(link, "HEAD_MIN_GUARD_SECONDS", 0.3)
    request, reason = link._head_feedback_request(
        42, 25 / 300, afsk.PROFILE_300.head_match_allowance_seconds)
    station = _connected()
    assert reason == "residual guard below target"
    assert station._apply_head_feedback(request, seq=7)
    assert abs(station._tx_head_seconds - 0.64) < 1e-12


def test_repeated_identical_feedback_is_idempotent():
    station = _connected()
    assert station._apply_head_feedback(64, seq=7)
    assert not station._apply_head_feedback(64, seq=7)
    assert abs(station._tx_head_seconds - 0.64) < 1e-12


def test_stale_and_duplicate_feedback_cannot_repeatedly_inflate_padding():
    station = _connected()
    assert station._apply_head_feedback(64, seq=7)
    for _ in range(4):
        assert not station._apply_head_feedback(64, seq=7)
    # A delayed smaller absolute request is stale, not another delta.
    assert not station._apply_head_feedback(55, seq=6)
    assert abs(station._tx_head_seconds - 0.64) < 1e-12


def test_zero_observation_increases_safely_and_is_bounded():
    request, reason = link._head_feedback_request(
        42, 0.0, afsk.PROFILE_1200.head_match_allowance_seconds)
    assert reason == "zero observation is a lower bound"
    assert link._decode_head_duration(request) == 0.52
    request, _ = link._head_feedback_request(
        95, 0.0, afsk.PROFILE_300.head_match_allowance_seconds)
    assert link._decode_head_duration(request) == link.HEAD_MAX_SECONDS


def test_small_matcher_window_deficit_does_not_adjust(monkeypatch):
    monkeypatch.setattr(link, "HEAD_MIN_GUARD_SECONDS", 0.3)
    # At 300 baud the conservative matcher may exclude a complete 16-symbol
    # (53.3 ms) window. A 40 ms apparent deficit is therefore not evidence
    # that more transmitted padding is required.
    request, reason = link._head_feedback_request(
        42, 78 / 300, afsk.PROFILE_300.head_match_allowance_seconds)
    assert request == 42
    assert reason == "deficit is within matcher-window allowance"


def test_padding_never_decreases_during_connection():
    station = _connected()
    assert not station._apply_head_feedback(30, seq=8)
    assert station._tx_head_seconds == 0.42


def test_feedback_duration_survives_baud_change(monkeypatch):
    monkeypatch.setattr(link, "HEAD_MIN_GUARD_SECONDS", 0.3)
    request, _ = link._head_feedback_request(
        42, 100 / 1200, afsk.PROFILE_1200.head_match_allowance_seconds)
    station = _connected()
    station._apply_head_feedback(request, seq=9)
    duration = station._tx_head_seconds
    assert int(__import__("math").ceil(duration * 300)) == 192
    assert int(__import__("math").ceil(duration * 1200)) == 768
    assert duration == 0.64
    station._apply_tx_profile(afsk.PROFILE_1200)
    assert station._tx_head_seconds == duration


def test_each_direction_adapts_independently():
    a = _connected("STA1")
    b = _connected("STA2")
    assert a._apply_head_feedback(64, seq=1)
    assert a._tx_head_seconds == 0.64
    assert b._tx_head_seconds == 0.42


def test_retry_floor_and_mode_changes_preserve_monotonic_state():
    station = _connected()
    station._apply_head_feedback(64, seq=3)
    station.role = "IRS"
    station.role = "ISS"  # the state transition made by a floor grant
    station._apply_tx_profile(afsk.PROFILE_600)
    assert not station._apply_head_feedback(64, seq=3)  # retried feedback
    assert station._tx_head_seconds == 0.64


def test_a_vf3_data_frame_with_a_short_head_asks_for_more_padding(monkeypatch):
    """The whole point of moving the loop to seconds: VF3 adapts too."""
    monkeypatch.setattr(link, "HEAD_MIN_GUARD_SECONDS", 0.3)
    station = _connected()
    station._apply_rx_profile(vf3_mode.VF3)
    sent = []
    monkeypatch.setattr(station, "_tx_packet",
                        lambda ptype, body: sent.append((ptype, body)))
    advertised = link._encode_head_duration(0.42)
    body = bytes([link.EOF_BIT, advertised]) + b"payload"
    # Two surviving cores, ~42.7 ms -- far short of the 300 ms guard and well
    # outside VF3's one-core allowance.
    station._rx_measurements[(link.PT_DATA, body)] = {
        "head": None,
        "head_seconds": 2 * vf3.CORE_SAMPLES / vf3.SAMPLE_RATE,
    }

    assert station._handle_data(body) == b"payload"
    ptype, ack = sent[-1]
    assert ptype == link.PT_DATA_ACK
    requested = ack[3]
    assert requested > advertised
    assert link._decode_head_duration(requested) > 0.42


def test_a_vf3_head_within_one_core_of_target_is_left_alone(monkeypatch):
    monkeypatch.setattr(link, "HEAD_MIN_GUARD_SECONDS", 0.3)
    allowance = vf3_mode.VF3.head_match_allowance_seconds
    request, reason = link._head_feedback_request(42, 0.29, allowance)
    assert request == 42
    assert reason == "deficit is within matcher-window allowance"
