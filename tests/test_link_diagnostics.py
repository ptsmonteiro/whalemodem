"""Decode-loop diagnostics and the limit that keeps the receiver awake.

The SNR summaries below are the readouts. The tests after them are the limit:
a decode attempt must not be able to cost more than the window it searches,
and nothing a candidate mode claims to see may enlarge that window. See the
comment above them for the on-air incident that is.
"""

import time
from dataclasses import dataclass, field

import numpy as np

from whale import link, rx_audio, streaming
from whale.modes import hc0
from whale.modes.hc0_mode import HC0
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


# -- what keeps the receiver awake -----------------------------------
#
# The budget below is an incident, not a preference. An on-air HF session went
# deaf because one candidate mode reported a sync lock on plain noise on every
# poll, which stopped the RX buffer from ever being pruned, which left every
# later poll re-searching a full 10-second buffer at five profiles -- about 17
# seconds of work per poll against a buffer holding 10 seconds of audio, so
# ~41% of the timeline was never examined at all and the peer's
# retransmissions landed in the gaps.
#
# What answers it now is ownership rather than arbitration: the transport's
# ring is not what any decoder searches. Each mode keeps its own short window
# (whale/streaming.py) and gives up a lock that never resolves, so the work a
# poll does is bounded by that window and by nothing else -- not by how long
# the channel was idle, not by how far behind the loop has fallen, and not by
# what any other candidate claims to see.
#
# Deliberately unit-level and deliberately tiny. tests/test_audio_e2e.py hands
# audio between stations instantly -- its own docstring says wall-clock time is
# meaningless there, it measures airtime -- and the single-frame tests decode
# one frame at a time, so neither can see a receiver that has fallen behind
# real time. Costs below are simulated by burning a known amount of CPU inside
# a stub decoder, so the whole section runs in a few seconds.

RX_RATE = 12_000
FRAME_SECONDS = 0.2


class _StubCodec:
    """A decoder with a dial for what it costs and what it claims to see."""

    def __init__(self, seconds_per_audio_second=0.0, result=None):
        self.rate = seconds_per_audio_second
        self.result = {} if result is None else result
        self.attempts = 0
        self.longest_audio = 0

    def decode(self, audio):
        self.attempts += 1
        self.longest_audio = max(self.longest_audio, len(audio))
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


def _poll(a_link, transport, seconds):
    """`seconds` of capture arrive, then one decode poll, as the loop runs."""
    transport.deliver(np.zeros(int(seconds * RX_RATE), dtype=np.float32))
    while a_link._decode_stream():
        pass


def test_a_perpetual_speculative_lock_cannot_pin_the_receiver():
    """A candidate reports a sync it never resolves -- confidence over its own
    threshold, no frame boundary, on every poll forever. That is what pure
    noise does to hf7/hf8, whose threshold sits below their own noise floor.

    It must not be able to accumulate audio behind it. Left unbounded it makes
    every later attempt search more, and once an attempt costs more than the
    interval it runs on, audio ages out unexamined and the receiver is deaf to
    a peer it can hear perfectly well."""
    liar = _StubMode("liar", 1, _StubCodec(result={"confidence": 0.99,
                                                   "start_index": 0}))
    a_link, transport = _stub_link(liar)

    for _ in range(20):  # 20 s: twice the transport's whole ring
        _poll(a_link, transport, 1.0)

    examined = liar.codec.longest_audio / RX_RATE
    assert examined <= 3.0, (
        f"a speculative lock grew an attempt to {examined:.2f}s of audio")


def test_an_attempt_never_searches_more_than_its_own_window():
    """The property the incident turned on, stated directly: how long the
    channel was idle before a frame arrives changes nothing about what one
    decode attempt costs."""
    quiet = _StubMode("quiet", 1, _StubCodec(result={"confidence": 0.0}))
    a_link, transport = _stub_link(quiet)

    for _ in range(5):
        _poll(a_link, transport, 1.0)
    settled = quiet.codec.longest_audio
    quiet.codec.longest_audio = 0

    for _ in range(30):  # 30 s of nothing at all, well past the ring's length
        _poll(a_link, transport, 1.0)

    assert quiet.codec.longest_audio <= settled, (
        "attempt size grew with the idle stretch before it")
    assert (settled / RX_RATE
            <= streaming.StreamingDecoder.ACQUISITION_SECONDS + 0.01)


def test_one_expensive_candidate_cannot_starve_a_cheap_one():
    """Every candidate is fed the same stream. Cost differs between them by
    orders of magnitude, so what has to be true is that an expensive
    candidate's cost is bounded, not that it is scheduled away: a mode nobody
    ever tries is a mode nobody can receive."""
    cheap = _StubMode("cheap", 1, _StubCodec(0.002, {"confidence": 0.0}))
    dear = _StubMode("dear", 2, _StubCodec(0.2, {"confidence": 0.0}))
    a_link, transport = _stub_link(cheap, dear)

    for _ in range(8):
        _poll(a_link, transport, 1.0)

    assert cheap.codec.attempts == dear.codec.attempts > 0, (
        "candidates on one stream must be attempted alike")
    window = streaming.StreamingDecoder.ACQUISITION_SECONDS
    assert dear.codec.longest_audio / RX_RATE <= window + 0.01


def test_decode_cost_is_recorded_per_mode_across_a_resynchronisation():
    """The cost accounting rides on the decoder, so voiding the capture
    around our own TX does not strand it."""
    quiet = _StubMode("quiet", 1, _StubCodec(0.001, {"confidence": 0.0}))
    a_link, transport = _stub_link(quiet)

    _poll(a_link, transport, 2.0)
    before = a_link._decode_cost["quiet"].attempts
    assert before > 0

    transport.send(np.zeros(4_800, dtype=np.float32))
    _poll(a_link, transport, 2.0)

    assert a_link._decode_cost["quiet"].attempts > before


def test_native_head_measurement_is_reported_by_the_mode_decoder():
    payload = bytes(range(16))
    tx = HC0.encode(payload, head_seconds=0.3)
    captured = rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))
    decoded = HC0.decode(captured, head_seconds=0.3)
    assert decoded["payload"] == payload

    assert decoded["head_blocks_observed"] >= hc0.LEAD_IN_BLOCKS
    assert decoded["head_seconds_received"] > 0.0
