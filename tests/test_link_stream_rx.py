"""The receive path's ownership and coordinate contract.

These are the tests that were missing when a shared, destructively consumed
RX buffer went on the air.  The link decoded a frame, told the transport to
drop "everything up to index N" -- N being a position in the decoder's own
private coordinates, not an index into the transport's buffer -- and the two
origins drifted apart the moment the rolling ring trimmed its front or a
transmission cleared it.  The frame survived in the buffer, the decoders were
reset and replayed over it, and the peer's *next* DATA frame was decoded as
the previous sequence number again: a duplicate ACK, an 11 s timeout and a
retransmission for every single frame.

The contract that replaced it, and what each test below pins:

  - the transport owns the capture and nobody else shortens it; reads are by
    monotonic stream position and never rewind;
  - audio that was lost -- trimmed by the ring, or voided around our own
    transmission -- is *announced*, never silently skipped over;
  - a decoder advances itself past a frame it reported and keeps consuming,
    so nothing can hand it that frame a second time.
"""

import numpy as np

from support.ring_transport import CHUNK_SAMPLES, RingTransport
from whale import link, rx_audio
from whale import link_protocol as protocol
from whale import transport as transport_mod


# -- transport contract -------------------------------------------------

def test_the_stream_position_survives_a_trim_and_a_discard():
    """The single coordinate everything else is expressed in.

    It counts captured samples and never restarts, so an index taken before
    a trim or a transmission still means the same audio afterwards. The old
    buffer reset this to zero on every TX, which is what let a decoder's
    indices and the transport's disagree."""
    stream = transport_mod.ReceiveStream(sample_rate=1_000, seconds=1.0)
    for _ in range(5):
        stream.write(np.zeros(400, dtype=np.float32))
    assert stream.position == 2_000

    stream.discard()
    assert stream.position == 2_000, "discarding must not rewind the stream"
    stream.write(np.zeros(400, dtype=np.float32))
    assert stream.position == 2_400


def test_a_reader_that_falls_behind_the_ring_is_told_it_lost_audio():
    """Silent misalignment is the failure mode this design exists to
    prevent, so falling behind has to be visible rather than inferred."""
    stream = transport_mod.ReceiveStream(sample_rate=1_000, seconds=1.0)
    stream.write(np.arange(500, dtype=np.float32))
    first = stream.read()
    assert not first.gap and first.start == 0

    # Three seconds arrive into a one-second ring while nobody was reading.
    for _ in range(6):
        stream.write(np.zeros(500, dtype=np.float32))

    late = stream.read(first.end)
    assert late.gap, "a reader left behind by the ring must be told so"
    assert late.start > first.end
    assert late.end == stream.position


def test_a_discard_voids_the_capture_without_moving_anyone_backwards():
    stream = transport_mod.ReceiveStream(sample_rate=1_000, seconds=10.0)
    stream.write(np.ones(500, dtype=np.float32))
    read = stream.read()

    stream.discard()
    stream.write(np.zeros(300, dtype=np.float32))

    after = stream.read(read.end)
    assert after.gap, "audio voided around our own TX must read as a gap"
    assert after.start == 500 and after.end == 800
    assert len(after.audio) == 300


def test_reading_does_not_consume_the_capture():
    """The transport owns the ring: a reader may not shorten it. Two readers
    at the same position must see the same audio."""
    stream = transport_mod.ReceiveStream(sample_rate=1_000, seconds=10.0)
    stream.write(np.arange(300, dtype=np.float32))

    first = stream.read()
    second = stream.read(0)
    assert np.array_equal(first.audio, second.audio)
    assert not second.gap


# -- link contract ------------------------------------------------------

def _capture(tx_audio):
    """The 48 kHz transmission as the receiving station's capture hears it."""
    return rx_audio.downsample(np.concatenate((
        np.asarray(tx_audio, dtype=np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))


def _data_frame(profile, seq, chunk, head_seconds):
    body = (bytes([seq & protocol.SEQ_MASK, link._encode_head_duration(head_seconds)])
            + chunk)
    header, remainder = link._encode_air_header(
        protocol.PT_DATA, profile.mode_id, body)
    return profile.encode(header + remainder, head_seconds=head_seconds)


def _receiver():
    """A CONNECTED station listening on the control waveform only.

    One candidate mode, because what is under test is the receive stream's
    bookkeeping, not the mode search."""
    t = RingTransport()
    b = link.Link(t, "STA2")
    control = b.modes.control
    b.state, b.role, b.peer_call = "CONNECTED", "IRS", "STA1"
    b.peer_supported_modes = {control.mode_id}
    b.rx_profile = b.tx_profile = control
    return b, t, control


def _pump(b, t, audio, delivered):
    """Deliver audio in capture-callback-sized pieces, polling as the decode
    loop does. Inline rather than on the real thread so the test is
    deterministic; the code path is the same one."""
    audio = np.asarray(audio, dtype=np.float32)
    for start in range(0, len(audio), CHUNK_SAMPLES):
        t.deliver(audio[start:start + CHUNK_SAMPLES])
        while b._decode_stream():
            pass
        while not b._rx_packets.empty():
            ptype, body = b._rx_packets.get()
            delivered.append((ptype, body[0] & protocol.SEQ_MASK))


def test_consecutive_frames_are_each_delivered_once_across_a_trim_and_a_tx():
    """The on-air failure, in software.

    Twelve seconds of quiet first, so the transport's rolling ring has
    genuinely slid past its own length before anything arrives -- that slide
    is what pulled the old shared-buffer coordinates apart. Then a DATA
    frame, then this station keys its ACK (which voids its own capture),
    then the peer's *next* DATA frame. Each must be delivered exactly once,
    as itself. Under the old code the second frame arrived as another copy
    of the first."""
    b, t, control = _receiver()
    rate = control.rx_sample_rate
    head = b._rx_head_seconds
    frames = [_capture(_data_frame(control, seq, bytes([seq]) * 32, head))
              for seq in (0, 1)]

    delivered = []
    _pump(b, t, np.zeros(int(12.0 * rate), dtype=np.float32), delivered)
    assert t.rx_stream_position > transport_mod.RX_BUFFER_SECONDS * rate

    _pump(b, t, frames[0], delivered)
    assert delivered == [(protocol.PT_DATA, 0)], delivered

    # Our own ACK. transport.send() voids the capture around the keying, so
    # the decoders must resynchronise rather than replay what they held.
    t.send(np.zeros(48_000, dtype=np.float32))

    _pump(b, t, np.zeros(int(2.5 * rate), dtype=np.float32), delivered)
    _pump(b, t, frames[1], delivered)
    _pump(b, t, np.zeros(int(3.0 * rate), dtype=np.float32), delivered)

    assert delivered == [(protocol.PT_DATA, 0), (protocol.PT_DATA, 1)], delivered


def test_a_delivered_frame_is_behind_every_decoder_and_cannot_come_back():
    """Why the above holds however long the session runs: the frame is not
    merely 'already seen', it is no longer reachable. Every decoder's window
    starts after it, and the link's read position is past it."""
    b, t, control = _receiver()
    head = b._rx_head_seconds
    delivered = []
    _pump(b, t, _capture(_data_frame(control, 0, b"x" * 32, head)), delivered)
    assert delivered == [(protocol.PT_DATA, 0)]

    frame_end = b._peer_unkeyed_at is not None
    assert frame_end, "the frame's end must have anchored the turnaround"
    decoder = b._stream_decoders[control]
    assert decoder.window_start > 0
    assert b._rx_stream_position >= decoder.stream_position

    # Nothing but silence follows, and nothing is delivered again.
    _pump(b, t, np.zeros(int(6.0 * control.rx_sample_rate), dtype=np.float32),
          delivered)
    assert delivered == [(protocol.PT_DATA, 0)], delivered


def test_our_own_transmission_resynchronises_the_decoders():
    """Half duplex, expressed as a discard marker rather than a buffer reset:
    the position keeps counting and every decoder is moved to the point the
    capture resumes from."""
    b, t, control = _receiver()
    delivered = []
    _pump(b, t, np.zeros(int(2.0 * control.rx_sample_rate), dtype=np.float32),
          delivered)
    assert b._stream_decoders

    t.send(np.zeros(48_000, dtype=np.float32))
    resume = t.rx_discard_position
    assert resume == t.rx_stream_position

    _pump(b, t, np.zeros(CHUNK_SAMPLES, dtype=np.float32), delivered)
    for decoder in b._stream_decoders.values():
        assert decoder.window_start >= resume
    assert b._rx_stream_position == t.rx_stream_position
