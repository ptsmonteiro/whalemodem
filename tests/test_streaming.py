"""Live waveform feeding contracts."""

import numpy as np

from whale import afsk, framing, rx_audio
from whale.modes.hf8_mode import HF8, HF8_PHY
from whale.streaming import StreamingDecoder, decoder_for


def _capture(audio, lead=0):
    return rx_audio.downsample(np.concatenate((
        np.zeros(lead, dtype=np.float32),
        np.asarray(audio, dtype=np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))


def test_streaming_decoder_feeds_chunks_and_decodes_once_at_frame_end():
    payload = bytes(range(framing.AIR_HEADER_BYTES + 8))
    tx = afsk.PROFILE_300.encode(payload)
    captured = _capture(tx, lead=500)
    decoder = decoder_for(afsk.PROFILE_300)

    results = []
    for start in range(0, len(captured), 1_000):
        results.extend(decoder.feed(captured[start:start + 1_000]))

    assert len(results) == 1
    assert results[0]["payload"] == payload
    assert decoder.decode_count < len(captured) // 1_000
    assert results[0]["end_index"] > results[0]["start_index"]


def test_streaming_decoder_reports_positions_in_the_stream_it_was_reset_to():
    """Every index a decoder reports is a transport stream position.

    This is the coordinate contract the link depends on: the decoder is
    resynchronised at whatever position the transport says its audio starts
    at, and the indices it hands back are directly comparable with
    transport.rx_stream_position -- not offsets into some private buffer."""
    payload = bytes(range(framing.AIR_HEADER_BYTES + 8))
    captured = _capture(afsk.PROFILE_300.encode(payload), lead=500)
    origin = 4_000_000

    decoder = decoder_for(afsk.PROFILE_300)
    decoder.reset(origin)
    results = []
    for start in range(0, len(captured), 1_000):
        results.extend(decoder.feed(captured[start:start + 1_000]))

    assert len(results) == 1
    assert results[0]["payload"] == payload
    assert origin <= results[0]["start_index"] < results[0]["end_index"]
    # +1: a mode may round its final symbol boundary one sample past the
    # last sample fed.
    assert results[0]["end_index"] <= origin + len(captured) + 1
    assert decoder.stream_position == origin + len(captured)


def test_a_decoder_keeps_consuming_past_a_frame_instead_of_replaying_it():
    """The property the duplicate-sequence failure was missing.

    Having reported a frame the decoder advances past it and goes on with
    the same stream. The frame is never in its window again, so no amount of
    further feeding can deliver it a second time -- and the next frame on the
    same stream is decoded as itself, once."""
    first = bytes(range(framing.AIR_HEADER_BYTES + 8))
    second = bytes(reversed(range(framing.AIR_HEADER_BYTES + 8)))
    decoder = decoder_for(afsk.PROFILE_300)

    got = []
    for payload in (first, second):
        captured = _capture(afsk.PROFILE_300.encode(payload), lead=500)
        for start in range(0, len(captured), 1_000):
            got.extend(decoder.feed(captured[start:start + 1_000]))
        # Silence between keyings, as a real receiver hears.
        got.extend(decoder.feed(np.zeros(24_000, dtype=np.float32)))

    assert [result["payload"] for result in got] == [first, second]


def test_an_unresolvable_lock_cannot_grow_the_decoder_window_forever():
    """A lock that never produces a verdict must not pin audio.

    That is what pure noise does to a mode whose confidence threshold sits
    below its own noise floor, and an unbounded window is an unbounded
    per-poll decode cost -- which is how a receiver goes deaf while hearing
    the peer perfectly well."""
    class Liar:
        name, mode_id = "liar", 1
        chunk_size = 64
        confidence_threshold = 0.5
        tx_sample_rate, rx_sample_rate = 48_000, 12_000

        def airtime(self, payload_len):
            return 0.2

        def decode(self, audio, **kwargs):
            return {"confidence": 0.99, "start_index": 0}

    decoder = decoder_for(Liar())
    for _ in range(200):  # 20 s of audio, twice the transport's whole ring
        decoder.feed(np.zeros(1_200, dtype=np.float32))

    held = len(decoder.window()) / 12_000
    assert held <= 3.0, f"a speculative lock pinned {held:.2f}s of audio"


def test_a_lock_keeps_a_frame_that_is_still_arriving():
    """The other half: the window is bounded, not amnesiac. A frame that is
    only half here must not have its head trimmed off."""
    payload = bytes(range(framing.AIR_HEADER_BYTES + 8))
    captured = _capture(afsk.PROFILE_300.encode(payload), lead=500)
    # This frame is 22341 samples and the unlocked window holds 21000, so a
    # decoder that kept trimming would have thrown its head away before the
    # end arrived. The lock is what stops that.
    assert len(captured) > 12_000 * StreamingDecoder.ACQUISITION_SECONDS
    part = 18_000

    decoder = decoder_for(afsk.PROFILE_300)
    for start in range(0, part, 1_000):
        assert not decoder.feed(captured[start:start + 1_000])
    assert decoder.locked, "the sync word should have been found by now"

    results = []
    for start in range(part, len(captured), 1_000):
        results.extend(decoder.feed(captured[start:start + 1_000]))
    assert [result["payload"] for result in results] == [payload]
    assert decoder.window().size == 0, "the frame is still in the window"


def test_streaming_decoder_reset_starts_a_new_stream():
    decoder = decoder_for(afsk.PROFILE_300)
    decoder.feed(np.zeros(10_000, dtype=np.float32))
    assert decoder.decode_count > 0
    decoder.reset()
    assert decoder.decode_count == 0
    assert not decoder.locked
    assert decoder.stream_position == 0


def test_failed_ofdm_frame_is_terminal_so_the_next_retry_can_be_decoded():
    payload = bytes((i * 37 + 5) & 0xFF
                    for i in range(HF8.chunk_size + framing.AIR_HEADER_BYTES))
    tx = HF8.encode(payload)
    damaged = tx.copy()
    body_start = HF8_PHY.head_samples() * 4
    damaged[body_start + 48_000:body_start + 96_000] = 0.0

    decoder = decoder_for(HF8)
    failed = decoder.feed(_capture(damaged))
    assert len(failed) == 1
    assert failed[0]["payload"] is None
    assert failed[0]["end_index"] > failed[0]["start_index"]

    # The decoder stepped past that near miss by itself -- no reset needed --
    # so the retransmission that follows on the same stream decodes normally.
    retried = decoder.feed(_capture(tx))
    assert len(retried) == 1
    assert retried[0]["payload"] == payload
