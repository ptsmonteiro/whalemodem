"""The frame sweep: the exchange, the tally, and the states it must leave.

The on-air tests run two ``SweepEngine``s over ``link_harness``'s paired
FakeTransports, so every frame here is really modulated, really searched for
and really decoded -- only the sound card is missing. What that buys is the
two properties the sweep exists for: that a tally separates a frame that
decoded from one that synced and failed and one that never arrived, and that
a listener left armed by a caller that vanished goes back to idle.

The state-machine tests (the walk up the ladder, the arming window) use stub
modes instead, because they are about timing and order and a real waveform
would only make them slow.
"""

from __future__ import annotations

import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from link_harness import FakeTransport, speed_up
from whale import policy, sweep
from whale.mode_qualification import registry
from whale.modes.vf14 import VF14_4
from whale.waveform import ModeRegistry

CONTROL_ONLY = ModeRegistry((VF14_4,), VF14_4)


# -- frames ----------------------------------------------------------------

def test_a_frame_is_derived_from_its_identity_alone():
    """Both ends generate the bytes, so a frame is checked, not trusted."""
    first = sweep.frame_payload(0x1234, 23, 0, 274)
    assert len(first) == 274
    assert first == sweep.frame_payload(0x1234, 23, 0, 274)
    assert first != sweep.frame_payload(0x1234, 23, 1, 274)
    assert first != sweep.frame_payload(0x1235, 23, 0, 274)
    assert sweep.decode_frame(first).sweep_id == 0x1234
    assert sweep.data_fields(sweep.decode_frame(first)) == (23, 0)


def test_every_control_frame_fits_the_smallest_control_mode():
    """The HF control mode carries 42 payload bytes; that is the budget."""
    hf = registry("hf", "default", policy.HF.max_useful_frame_seconds)
    budget = sweep.full_frame_size(hf.control)
    tally = sweep.Tally(14, expected=5, decoded={0, 1}, near_missed=1,
                        snr_db=[3.0, 9.5])
    for extra in (b"", struct_pack_announce(), bytes([14]),
                  sweep.encode_result(14, tally)):
        payload = sweep.encode_control(sweep.FT_ANNOUNCE, "F4JAW-12", "N0CALL-15",
                                       0xFFFF, extra)
        assert len(payload) <= budget, (len(payload), budget)


def struct_pack_announce():
    return struct.pack("!BBH", 18, 5, 2910)


def test_a_payload_that_is_not_ours_is_not_a_sweep_frame():
    """An ARQ station's traffic decodes fine and is simply not ours."""
    assert sweep.decode_frame(b"WH\x03\x00rest") is None
    assert sweep.decode_frame(b"") is None
    assert sweep.decode_frame(b"WS\x63\x00\x00") is None  # unknown frame type


def test_a_tally_survives_the_result_frame():
    tally = sweep.Tally(23, expected=5, decoded={0, 2, 4}, near_missed=1,
                        snr_db=[4.0, 6.0, 11.0])
    (mode_id, sent, decoded, near, missed, low, mean, high,
     seqs) = sweep.decode_result(sweep.encode_result(23, tally))
    assert (mode_id, sent, decoded, near, missed) == (23, 5, 3, 1, 1)
    assert (low, mean, high) == (4.0, 7.0, 11.0)
    # Which frames were lost is the diagnosis the counts cannot give.
    assert seqs == (0, 2, 4)
    assert sweep.seq_list(seqs, sent) == "#.#.#"


def test_a_tally_with_no_decode_reports_no_snr():
    result = sweep.decode_result(sweep.encode_result(23, sweep.Tally(23, expected=5)))
    assert result[1:5] == (5, 0, 0, 5)
    assert result[5:] == (None, None, None, ())


# -- the ladder ------------------------------------------------------------

@dataclass(frozen=True)
class StubMode:
    """A waveform-shaped object for tests about order and timing."""

    name: str
    mode_id: int
    chunk_size: int = 64
    confidence_threshold: float = 0.5
    tx_sample_rate: int = 48_000
    rx_sample_rate: int = 12_000
    seconds: float = 1.0

    def airtime(self, payload_len):
        return self.seconds

    def encode(self, payload):
        return np.zeros(int(self.seconds * self.tx_sample_rate), dtype=np.float32)

    def decode(self, audio, **kwargs):
        return {"payload": None, "confidence": 0.0}


def stub_registry(count=4):
    modes = tuple(StubMode(f"stub{i}", 100 + i) for i in range(count))
    return ModeRegistry(modes, modes[0])


@pytest.mark.parametrize("channel", ["fm", "hf"])
def test_the_sweep_walks_up_from_the_control_mode(channel):
    modes = registry(channel, "default",
                     policy.by_name(channel).max_useful_frame_seconds)
    ladder = sweep.sweep_modes(modes)
    assert ladder[0] is modes.control
    ids = [mode.mode_id for mode in modes.modes]
    assert [mode.mode_id for mode in ladder] == ids[ids.index(modes.control.mode_id):]


def test_the_walk_stops_after_two_consecutive_failures():
    """Two dead modes in a row ends it; the rungs below are already banked."""
    engine = sweep.SweepEngine(FakeTransport(), "STA1", stub_registry(4),
                               policy.FM, turnaround=0.0, step=lambda text: None)
    outcomes = {100: 3, 101: 1, 102: 0, 103: 0}
    engine._measure = lambda peer, sweep_id, mode: sweep.ModeResult(
        "TX", mode.mode_id, mode.name, 3, outcomes[mode.mode_id], 0, 0)

    engine._measure_all("STA2", 1)

    assert [row.mode_id for row in engine.results] == [100, 101, 102, 103]


def test_a_single_failure_does_not_stop_the_walk():
    engine = sweep.SweepEngine(FakeTransport(), "STA1", stub_registry(4),
                               policy.FM, turnaround=0.0, step=lambda text: None)
    outcomes = {100: 3, 101: 0, 102: 2, 103: 1}
    engine._measure = lambda peer, sweep_id, mode: sweep.ModeResult(
        "TX", mode.mode_id, mode.name, 3, outcomes[mode.mode_id], 0, 0)

    engine._measure_all("STA2", 1)

    assert [row.mode_id for row in engine.results] == [100, 101, 102, 103]
    assert [row.passed for row in engine.results] == [True, False, True, True]


def test_the_arming_window_outlasts_the_announced_burst():
    """The listener's fallback must never cut a burst that is still arriving."""
    engine = sweep.SweepEngine(FakeTransport(), "STA1", stub_registry(),
                               policy.FM, turnaround=0.5, step=lambda text: None)
    mode = engine.modes.control
    assert engine.arm_window(mode, 5, 64) > engine.burst_duration(mode, 5, 64)


# -- two stations ----------------------------------------------------------

def make_pair(frames=2, modes=CONTROL_ONLY):
    """Two sweep engines over paired fake transports, started."""
    speed_up()
    ta, tb = FakeTransport(), FakeTransport()
    ta.peer, tb.peer = tb, ta
    engines = tuple(
        sweep.SweepEngine(transport, call, modes, policy.FM, turnaround=0.0,
                          frames_per_mode=frames, step=lambda text: None)
        for transport, call in ((ta, "STA1"), (tb, "STA2")))
    for engine in engines:
        engine.start()
    return engines + (ta, tb)


def wait_for(predicate, timeout=20.0):
    """Poll until the station reaches a state, or give up and say so."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def listening(engine):
    """Run ``engine`` as the idle listener for the duration of a test."""
    thread = threading.Thread(target=engine.run_listener, daemon=True)
    thread.start()
    return thread


def test_the_whole_exchange_runs_over_the_air():
    """Announce, ready, burst, result -- and then the roles swap."""
    a, b, ta, tb = make_pair()
    listening(b)
    try:
        a.run_caller("STA2")
    finally:
        a.stop()
        b.stop()

    forward = [row for row in a.results if row.direction == "TX"]
    reverse = [row for row in a.results if row.direction == "RX"]
    assert [row.decoded for row in forward] == [2]
    assert [row.decoded for row in reverse] == [2], "the roles never swapped"
    assert forward[0].snr_mean is not None and reverse[0].snr_mean is not None
    # The far end's view of the same measurement is the mirror image.
    assert [(row.direction, row.decoded) for row in b.results] == [
        ("RX", 2), ("TX", 2)]


def test_the_burst_is_one_keying():
    """The whole burst goes out under a single PTT: the overhead is paid once."""
    a, b, ta, tb = make_pair(frames=3)
    sent = []
    real_send = ta.send
    ta.send = lambda audio, **kw: (sent.append(len(audio)), real_send(audio, **kw))[1]
    listening(b)
    try:
        a.run_caller("STA2")
    finally:
        a.stop()
        b.stop()

    frame = len(VF14_4.encode(sweep.frame_payload(1, 23, 0, 274)))
    assert 3 * frame in sent
    assert sum(1 for length in sent if length >= 2 * frame) == 1


def corrupt_frames(transport, mode, *, near_miss=(), missed=()):
    """Damage chosen frames of a burst, leaving the others untouched.

    ``near_miss`` keeps the frame's head and sync and randomises the rest,
    which is what a frame that synchronised and failed its CRC looks like.
    ``missed`` silences the frame outright, so nothing ever syncs on it.
    """
    frame = len(mode.encode(sweep.frame_payload(1, mode.mode_id, 0, 274)))
    # The head and sync symbols come first; keeping them is what makes the
    # receiver find a frame and then fail to read it.
    keep = int(mode.head_symbols + mode.sync_symbols) * mode.symbol_samples

    def corrupt(audio):
        transport.corrupt = None  # one burst only; the handshake stays clean
        audio = np.array(audio, dtype=np.float32)
        rng = np.random.default_rng(7)
        for seq in near_miss:
            start = seq * frame + keep
            audio[start:(seq + 1) * frame] = rng.normal(
                0, 0.3, (seq + 1) * frame - start).astype(np.float32)
        for seq in missed:
            audio[seq * frame:(seq + 1) * frame] = 0.0
        return audio
    return corrupt


def test_the_tally_separates_decoded_near_missed_and_missed():
    """The distinction the report depends on, over real audio."""
    a, b, ta, tb = make_pair(frames=3)
    # Armed on the burst itself, so the control frames around it stay clean:
    # what is under test is how the burst is counted, not the handshake.
    burst = a._tx_burst

    def damaged(sweep_id, mode, count, frame_size):
        ta.corrupt = corrupt_frames(ta, mode, near_miss=(1,), missed=(2,))
        return burst(sweep_id, mode, count, frame_size)

    a._tx_burst = damaged
    listening(b)
    try:
        a.run_caller("STA2")
    finally:
        a.stop()
        b.stop()

    measured = [row for row in a.results if row.direction == "TX"][0]
    assert (measured.sent, measured.decoded) == (3, 1)
    assert measured.near_missed == 1, "the damaged frame was not seen at all"
    assert measured.missed == 1, "the silenced frame was counted as something"


def test_a_frame_that_is_not_the_bytes_we_generate_is_not_a_decode():
    """CRC-clean is not the same as correct; the payload is checked."""
    a, b, ta, tb = make_pair(frames=1)
    listening(b)
    try:
        # A well-formed burst frame for the armed measurement, whose body is
        # not what this (sweep id, seq) generates.
        a._tx_control(sweep.FT_ANNOUNCE, "STA2", 0x4242,
                      struct.pack("!BBH", 23, 1, 274))
        assert wait_for(lambda: b.receiver.armed is not None)
        good = sweep.frame_payload(0x4242, 23, 0, 274)
        a._tx(VF14_4.encode(good[:8] + bytes(len(good) - 8)))
        assert wait_for(lambda: b.receiver.armed.tally.near_missed)
        tally = b.receiver.armed.tally
        assert (tally.decoded_count, tally.near_missed) == (0, 1)
    finally:
        a.stop()
        b.stop()


def test_an_armed_listener_returns_to_idle_when_the_burst_never_comes(monkeypatch):
    """One aborted sweep must not brick a station left running all night.

    The caller announces, is answered, and then vanishes -- which is what a
    Ctrl-C between the announcement and the burst looks like from here. The
    listener has to fall out of its armed state on the announced burst's own
    clock and go on serving, or an evening of sweeping ends with the first
    interrupted one.
    """
    monkeypatch.setattr(sweep, "ARM_MARGIN_SECONDS", 0.2)
    monkeypatch.setattr(sweep, "ARM_MARGIN_FRACTION", 0.0)
    monkeypatch.setattr(sweep, "REPLY_SLACK", 0.1)
    a, b, ta, tb = make_pair(frames=1)
    steps = []
    b.step = steps.append
    listening(b)
    try:
        a._tx_control(sweep.FT_ANNOUNCE, "STA2", 0x77,
                      struct.pack("!BBH", VF14_4.mode_id, 1, 274))
        assert wait_for(lambda: b.receiver.armed is not None), "never armed"
        assert tb.keyings == 1, "the listener did not answer READY"

        assert wait_for(lambda: b.receiver.armed is None), "stayed armed"
        assert any("back to listening" in line for line in steps)
        # The tally is held rather than thrown away: the caller may still ask.
        assert b.receiver.tally_for(0x77, VF14_4.mode_id) is not None

        # And the station is not bricked: the next sweep is served normally.
        a.run_caller("STA2")
        assert [row.decoded for row in a.results if row.direction == "TX"] == [1]
    finally:
        a.stop()
        b.stop()


def test_a_listener_serves_two_sweeps_without_restarting():
    """An evening of sweeps is one listener, not one listener per sweep."""
    a, b, ta, tb = make_pair(frames=1)
    listening(b)
    try:
        a.run_caller("STA2")
        first = list(b.results)
        a.results.clear()
        a.run_caller("STA2")
    finally:
        a.stop()
        b.stop()

    assert len(first) == 2 and len(b.results) == 4
    assert [row.direction for row in b.results] == ["RX", "TX", "RX", "TX"]
    assert all(row.decoded == 1 for row in b.results)


def test_a_repeated_hand_over_is_answered_not_swept_again():
    """A lost DONE costs a control frame, not a second sweep's air time."""
    a, b, ta, tb = make_pair(frames=1)
    listening(b)
    try:
        a.run_caller("STA2")
        keyings = tb.keyings
        a._tx_control(sweep.FT_SWAP, "STA2", a.sweep_id)
        assert wait_for(lambda: tb.keyings > keyings)
        time.sleep(0.5)
        assert tb.keyings == keyings + 1, "the far end swept a second time"
    finally:
        a.stop()
        b.stop()


def test_a_lost_result_is_asked_for_again():
    """The burst is already spent, so the tally is held and re-asked for."""
    a, b, ta, tb = make_pair(frames=1)
    losses = []

    def lose_first_result(audio):
        # The listener's RESULT is the reply to the first ASK; lose it once.
        losses.append(len(audio))
        tb.corrupt = None
        return np.zeros(len(audio), dtype=np.float32)

    original = b._tx_control

    def watched(ftype, peer, sweep_id, extra=b""):
        if ftype == sweep.FT_RESULT and not losses:
            tb.corrupt = lose_first_result
        return original(ftype, peer, sweep_id, extra)

    b._tx_control = watched
    listening(b)
    try:
        a.run_caller("STA2")
    finally:
        a.stop()
        b.stop()

    assert losses, "no result was lost, so nothing was re-asked for"
    measured = [row for row in a.results if row.direction == "TX"][0]
    assert measured.decoded == 1, "the re-asked tally never arrived"


def test_an_idle_listener_transmits_nothing():
    """It sits passive until it is addressed by name."""
    a, b, ta, tb = make_pair(frames=1)
    listening(b)
    try:
        a._tx_control(sweep.FT_ANNOUNCE, "N0CALL", 0x55,
                      struct.pack("!BBH", 23, 1, 274))
        time.sleep(1.0)
        assert tb.keyings == 0
        assert b.receiver.armed is None
    finally:
        a.stop()
        b.stop()


def test_an_unanswered_announcement_is_not_a_measurement():
    """Nothing at the control mode means there is nothing to measure."""
    a, b, ta, tb = make_pair(frames=1)
    b.stop()  # nobody is listening
    a.control_reply_timeout = lambda: 0.05
    try:
        with pytest.raises(sweep.SweepError) as caught:
            a.run_caller("STA2")
    finally:
        a.stop()
    assert "did not answer" in str(caught.value)
    assert a.results == []


# -- the report ------------------------------------------------------------

def test_the_table_carries_both_directions():
    rows = [sweep.ModeResult("TX", 23, "vf14-4", 5, 5, 0, 0, 3.0, 6.0, 9.0),
            sweep.ModeResult("RX", 18, "vf12", 5, 0, 2, 3)]
    text = "\n".join(sweep.result_table(rows))
    assert "TX" in text and "RX" in text
    assert "vf14-4" in text and "vf12" in text
    assert "3.0" in text and "9.0" in text
    assert "--" in text, "a mode that decoded nothing has no SNR to show"


def test_the_table_says_so_when_nothing_was_measured():
    assert "no mode was measured" in "\n".join(sweep.result_table([]))
