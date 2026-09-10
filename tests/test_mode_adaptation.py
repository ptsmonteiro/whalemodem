"""Session-local statistical DATA-mode adaptation."""

from whale import link

import link_harness as harness


def _connected_link():
    station = link.Link(harness.FakeTransport(), "STA1")
    station.state = "CONNECTED"
    station.peer_supported_modes = set(station.modes.supported_ids)
    return station


def test_untested_modes_are_probed_upward_without_waiting_for_cooldown():
    station = _connected_link()
    first = station.tx_profile

    station._maybe_adapt(1)

    assert station.tx_profile is station.modes.step(first, +1)


def test_successful_retry_is_evidence_not_an_automatic_downgrade():
    station = _connected_link()
    fastest = station.modes.modes[-1]
    station._apply_tx_profile(fastest)

    # Model the observed HF7 run: a substantial clean history, followed by
    # one timeout and a correctly decoded retransmission.
    for _ in range(7):
        station._record_mode_attempt(fastest, True)
    station._record_mode_attempt(fastest, False)
    station._maybe_adapt(2)

    assert station.tx_profile is fastest
    successes, failures = station._decayed_mode_stats(fastest)
    assert successes > 7.9
    assert failures > 0.9


def test_old_failures_decay_out_of_the_session_estimate():
    station = _connected_link()
    profile = station.tx_profile
    station._mode_delivery_stats[profile.mode_id] = {
        "successes": 0.0, "failures": 4.0, "updated_at": 100.0}

    _, failures = station._decayed_mode_stats(profile, now=160.0)

    assert failures == 2.0
