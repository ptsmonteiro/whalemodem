"""Deterministic protocol-v3 tests for in-session head-timing feedback."""

from whale import afsk, link


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
    request, reason = link._head_feedback_request(42, 25, 300)
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
    request, reason = link._head_feedback_request(42, 0, 1200)
    assert reason == "zero observation is a lower bound"
    assert link._decode_head_duration(request) == 0.52
    request, _ = link._head_feedback_request(95, 0, 300)
    assert link._decode_head_duration(request) == link.HEAD_MAX_SECONDS


def test_small_matcher_window_deficit_does_not_adjust(monkeypatch):
    monkeypatch.setattr(link, "HEAD_MIN_GUARD_SECONDS", 0.3)
    # At 300 baud the conservative matcher may exclude a complete 16-symbol
    # (53.3 ms) window. A 40 ms apparent deficit is therefore not evidence
    # that more transmitted padding is required.
    request, reason = link._head_feedback_request(42, 78, 300)
    assert request == 42
    assert reason == "deficit is within matcher-window allowance"


def test_padding_never_decreases_during_connection():
    station = _connected()
    assert not station._apply_head_feedback(30, seq=8)
    assert station._tx_head_seconds == 0.42


def test_feedback_duration_survives_baud_change(monkeypatch):
    monkeypatch.setattr(link, "HEAD_MIN_GUARD_SECONDS", 0.3)
    request, _ = link._head_feedback_request(42, 100, 1200)
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
