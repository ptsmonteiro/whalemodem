"""Decode-loop diagnostics and the two limits that keep the receiver awake.

The SNR summaries below are the readouts. The tests after them are the
budget: an on-air HF session went deaf because one candidate mode reported a
sync lock on plain noise on every poll, which stopped the RX buffer from ever
being pruned, which left every later poll re-searching a full 10-second buffer
at five profiles -- about 17 seconds of work per poll against a buffer holding
10 seconds of audio, so ~41% of the timeline was never examined at all and the
peer's retransmissions landed in the gaps.  Both halves of that have to be
impossible: a speculative lock must not be able to pin the buffer, and a poll
must not be able to cost more than the buffer window it is searching.
"""

import time
from dataclasses import dataclass, field

import numpy as np

from whale import link, rx_audio
from whale.modes.hc0_mode import HC0
from whale.modes import hf_lead
from whale.waveform import ModeRegistry

import link_harness as harness


def test_decode_snr_summary_uses_tone_estimate():
    assert link._decode_snr_summary({"tone_snr_db": 14.46}) == (
        "SNR 14.5 dB (tone)")


def test_decode_snr_summary_uses_median_finite_carriers():
    result = {"carrier_snr_db": np.array([10.0, np.inf, 14.0, 12.0])}
    assert link._decode_snr_summary(result) == (
        "SNR 12.0 dB (median carrier)")


def test_decode_snr_summary_marks_missing_estimate_unavailable():
    assert link._decode_snr_summary({"confidence": 0.99}) == "SNR unavailable"


def test_decode_snr_summary_uses_effective_sync_estimate():
    assert link._decode_snr_summary({"snr_db": 8.24}) == (
        "SNR 8.2 dB (effective sync)")


# -- the decode poll's two limits ---------------------------------------
#
# Deliberately unit-level and deliberately tiny. tests/test_audio_e2e.py hands
# audio between stations instantly -- its own docstring says wall-clock time is
# meaningless there, it measures airtime -- and the single-frame tests decode
# one frame at a time, so neither can see a receiver that has fallen behind
# real time. Costs below are simulated by burning a known amount of CPU inside
# a stub decoder against a fraction-of-a-second buffer, so the whole section
# runs in a few seconds.

RX_RATE = 12_000
BUFFER_SECONDS = 0.5
FRAME_SECONDS = 0.2


class _StubCodec:
    """A decoder with a dial for what it costs and what it claims to see."""

    def __init__(self, seconds_per_audio_second=0.0, result=None):
        self.rate = seconds_per_audio_second
        self.result = {} if result is None else result
        self.attempts = 0

    def decode(self, audio):
        self.attempts += 1
        if self.rate:
            deadline = (time.perf_counter()
                        + len(audio) / RX_RATE * self.rate)
            while time.perf_counter() < deadline:
                pass
        return dict(self.result)


@dataclass(frozen=True)
class _StubMode:
    name: str
    mode_id: int
    codec: _StubCodec = field(compare=False, repr=False)
    chunk_size: int = 64
    confidence_threshold: float = 0.5
    tx_sample_rate: int = 48_000
    rx_sample_rate: int = RX_RATE

    def airtime(self, payload_len):
        return FRAME_SECONDS

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio)


@dataclass(frozen=True)
class _LeadStubMode(_StubMode):
    lead_label: int = 0


def _stub_link(*modes):
    transport = harness.FakeTransport()
    transport.peer = harness.FakeTransport()
    a_link = link.Link(transport, "STA1",
                       mode_registry=ModeRegistry(modes, modes[0]))
    a_link.peer_supported_modes = {mode.mode_id for mode in modes}
    return a_link, transport


def _fill(transport, seconds):
    transport._buf = np.zeros(int(seconds * RX_RATE), dtype=np.float32)


def test_a_perpetual_speculative_lock_cannot_pin_the_rx_buffer():
    """A candidate reports a sync it never resolves -- confidence over its own
    threshold, no frame boundary, on every poll forever. That is what pure
    noise does to hf7/hf8, whose threshold sits below their own noise floor.

    The buffer must be pruned anyway. Left pinned at full length it makes
    every later poll re-search all of it, and once a poll costs more than the
    buffer holds, audio ages out unexamined between polls and the receiver is
    deaf to a peer it can hear perfectly well."""
    liar = _StubMode("liar", 1, _StubCodec(result={"confidence": 0.99}))
    a_link, transport = _stub_link(liar)

    for _ in range(8):
        # More audio arrives between polls than any poll consumes.
        transport._buf = np.concatenate(
            [transport.snapshot_rx(),
             np.zeros(int(BUFFER_SECONDS * RX_RATE), dtype=np.float32)])
        a_link._decode_one(transport.snapshot_rx())

    held = len(transport.snapshot_rx()) / RX_RATE
    assert held <= a_link._rx_keep_seconds + 0.01, (
        f"a speculative lock pinned {held:.2f}s of audio")


def test_pruning_keeps_a_frame_that_is_still_arriving():
    """Why the prune is bounded rather than absolute: a frame that is only
    half here must not have its head cut off."""
    quiet = _StubMode("quiet", 1, _StubCodec(result={"confidence": 0.0}))
    a_link, transport = _stub_link(quiet)
    _fill(transport, 30.0)

    a_link._decode_one(transport.snapshot_rx())

    held = len(transport.snapshot_rx()) / RX_RATE
    assert held >= FRAME_SECONDS, (
        f"kept {held:.2f}s, less than one {FRAME_SECONDS:.2f}s frame")


def test_an_expensive_candidate_cannot_spend_the_whole_poll():
    """One candidate costing seconds per attempt must not take the poll with
    it: it is rate-limited, the poll stays inside its budget, and the cheap
    candidates are attempted every single time."""
    cheap = _StubMode("cheap", 1, _StubCodec(0.002, {"confidence": 0.0}))
    dear = _StubMode("dear", 2, _StubCodec(2.0, {"confidence": 0.0}))
    a_link, transport = _stub_link(cheap, dear)
    budget = link.DECODE_POLL_BUDGET_FRACTION * a_link._rx_keep_seconds

    polls = []
    for _ in range(6):
        _fill(transport, BUFFER_SECONDS)
        started = time.perf_counter()
        a_link._decode_one(transport.snapshot_rx())
        polls.append(time.perf_counter() - started)

    # The first poll is how an unmeasured candidate gets measured at all, so
    # it is allowed to overrun. Nothing after it is.
    assert max(polls[1:]) <= budget, (
        f"polls {polls[1:]} exceed the {budget:.2f}s budget")
    assert cheap.codec.attempts == 6, "the cheap candidate was starved"
    assert dear.codec.attempts < 6, "the expensive candidate was never limited"


def test_an_expensive_candidate_is_rate_limited_rather_than_dropped():
    """A mode nobody ever tries is a mode nobody can receive, so the duty
    cycle has to let it back in."""
    cheap = _StubMode("cheap", 1, _StubCodec(0.002, {"confidence": 0.0}))
    dear = _StubMode("dear", 2, _StubCodec(1.0, {"confidence": 0.0}))
    a_link, transport = _stub_link(cheap, dear)

    for _ in range(8):
        _fill(transport, BUFFER_SECONDS)
        a_link._decode_one(transport.snapshot_rx())

    assert dear.codec.attempts >= 2, "the expensive candidate never ran again"


def test_a_candidate_that_is_decoding_frames_keeps_its_attempt():
    """Cost is not the only thing that matters: the mode carrying the session
    is attempted whatever it costs."""
    cheap = _StubMode("cheap", 1, _StubCodec(0.002, {"confidence": 0.0}))
    dear = _StubMode("dear", 2, _StubCodec(2.0, {"confidence": 0.0}))
    a_link, transport = _stub_link(cheap, dear)

    _fill(transport, BUFFER_SECONDS)
    a_link._decode_one(transport.snapshot_rx())
    cost = a_link._decode_cost["dear"]
    cost.frames, cost.last_frame_at = 1, time.monotonic()

    before = dear.codec.attempts
    for _ in range(3):
        _fill(transport, BUFFER_SECONDS)
        a_link._decode_one(transport.snapshot_rx())
    assert dear.codec.attempts == before + 3, (
        "a mode that is delivering frames was skipped for cost")


def test_candidates_are_attempted_cheapest_first():
    """Ordering is what stops an expensive candidate costing a cheap one its
    acquisition."""
    cheap = _StubMode("cheap", 1, _StubCodec(0.002, {"confidence": 0.0}))
    dear = _StubMode("dear", 2, _StubCodec(0.2, {"confidence": 0.0}))
    a_link, transport = _stub_link(dear, cheap)

    _fill(transport, BUFFER_SECONDS)
    a_link._decode_one(transport.snapshot_rx())
    _fill(transport, BUFFER_SECONDS)
    plan = a_link._budgeted_candidates((dear, cheap), transport.snapshot_rx())
    assert [mode.name for mode in plan][:2] == ["cheap", "dear"]


def test_hf_lead_candidates_cannot_starve_control_decode(monkeypatch):
    """A noisy lead may offer dozens of boundaries, but only a few body
    decodes may run in one poll.  Before the bound, this path sat outside the
    regular candidate budget and could postpone a DATA_ACK past the sender's
    timeout, producing the seq-04 late-ACK/duplicate pattern in the radio log.
    """
    mode = _LeadStubMode("lead", 1, _StubCodec(), lead_label=0)
    a_link, transport = _stub_link(mode)
    offered = [hf_lead.LeadCandidate(1.0, 0, i * 100)
               for i in range(40)]

    def candidates(_audio, limit=hf_lead.MAX_CANDIDATE_BOUNDARIES):
        del limit  # Deliberately ignore it: the link owns the safety bound.
        return tuple(offered)

    monkeypatch.setattr(hf_lead, "candidates", candidates)
    a_link._decode_one(transport.snapshot_rx())

    # The lead path is bounded to four body attempts.  One fallback attempt is
    # acceptable and verifies that a missed lead still has a recovery path.
    assert mode.codec.attempts <= link.HF_LEAD_CANDIDATE_LIMIT + 1


def test_hf_head_measurement_uses_the_full_snapshot_after_a_cropped_decode():
    payload = bytes(range(16))
    tx = HC0.encode(payload, head_seconds=0.3)
    captured = rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))
    decoded = HC0.decode(captured, head_seconds=0.3)
    assert decoded["payload"] == payload

    # A lead-candidate decoder may have reported a stale/short measurement;
    # the link must replace it using the absolute body start on the full RX
    # snapshot before producing DATA feedback.
    decoded["head_seconds_received"] = 0.0
    link._refresh_hf_head_measurement(HC0, captured, decoded, 0.3)
    assert decoded["head_blocks_observed"] >= hf_lead.MIN_BLOCKS
    assert decoded["head_seconds_received"] > 0.0
