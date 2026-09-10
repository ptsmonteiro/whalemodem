"""Receive progress survives chunk boundaries, but never capture gaps."""

import collections
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from whale import rx_audio
from whale.channel import AwgnChannel, ChannelChain, FrequencyOffsetChannel, SnrSpec, WattersonChannel
from whale.modes.hf7_mode import HF7
from whale.modes.hf8_mode import HF8
from whale.modes.hf9_mode import HF9
from whale.streaming import AudioHistory, ReceiveStream
from whale.transport import RadioTransport


def capture(mode, seed=19, snr=None, offset=0, fading=False):
    payload = np.random.default_rng(seed).integers(
        0, 256, mode.chunk_size + 10, dtype=np.uint8).tobytes()
    tx = mode.encode(payload)
    channels = []
    if fading:
        channels.append(WattersonChannel.from_preset(48000, "mid_latitude_moderate", seed))
    if offset:
        channels.append(FrequencyOffsetChannel(48000, offset))
    if snr is not None:
        channels.append(AwgnChannel(48000, SnrSpec(snr), seed ^ 0x5A5A))
    if channels:
        channel = ChannelChain(channels)
        tx = np.concatenate((channel.process(tx).audio, channel.drain().audio))
    return payload, rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES))))


def receive(mode, audio, chunk_size, stream=None):
    stream = stream or ReceiveStream(120000)
    results = []
    for start in range(0, len(audio), chunk_size):
        stream.append(0, start, audio[start:start + chunk_size])
        result = stream.decode(mode)
        if result.get("payload") is not None:
            results.append(result)
    return stream, results


@pytest.mark.parametrize("mode", [HF9, HF8, HF7], ids=lambda m: m.name)
@pytest.mark.parametrize("chunk_size,prefix", [(97, 0), (1200, 2990), (120000, 6051)])
def test_full_frames_across_chunk_and_search_boundaries(mode, chunk_size, prefix):
    payload, audio = capture(mode)
    audio = np.concatenate((np.zeros(prefix), audio, np.zeros(600)))
    stream, results = receive(mode, audio, chunk_size)
    assert [r["payload"] for r in results] == [payload]
    assert abs(results[0]["start_index"] - prefix - rx_audio.FILTER_DELAY_DECODE_SAMPLES) <= 2
    receiver = stream.receivers[mode.name]
    attempted, windows = set(receiver.attempted), receiver.search.windows
    for _ in range(5):
        assert stream.decode(mode).get("payload") is None
    assert receiver.attempted == attempted
    assert receiver.search.windows == windows


@pytest.mark.parametrize("snr,offset,fading", [(6, 0, False), (10, 19.4, False),
                                                (10, -19.4, False), (8, 0, True)])
def test_streaming_matches_batch_on_impaired_hf9(snr, offset, fading):
    decoded = 0
    for seed in range(5):
        payload, audio = capture(HF9, seed=seed, snr=snr, offset=offset, fading=fading)
        audio = np.concatenate((np.zeros(2980), audio, np.zeros(600)))
        batch = HF9.decode(audio)
        _, results = receive(HF9, audio, 713)
        if batch.get("payload") == payload:
            decoded += 1
            assert [r["payload"] for r in results] == [payload], seed
    assert decoded >= 3


def test_shared_acquisition_and_failed_body_are_not_repeated(monkeypatch):
    payload, audio = capture(HF8)
    stream = ReceiveStream(120000)
    stream.append(0, 0, audio)
    calls = []
    original = HF7.codec.decode

    def counted(*args, **kwargs):
        calls.append(kwargs["acquisition"])
        return original(*args, **kwargs)

    monkeypatch.setattr(HF7.codec, "decode", counted)
    assert stream.decode(HF7).get("payload") is None
    search = stream.receivers[HF7.name].search
    windows = search.windows
    assert stream.decode(HF8)["payload"] == payload
    assert stream.receivers[HF8.name].search is search
    assert search.windows == windows
    for _ in range(3):
        stream.decode(HF7)
    stream.append(0, len(audio), np.zeros(1200))
    stream.decode(HF7)
    assert len(calls) == 1


def test_back_to_back_frames_are_delivered_once():
    first, a = capture(HF9, seed=20)
    second, b = capture(HF9, seed=21)
    audio = np.concatenate((a, b, np.zeros(600)))
    _, results = receive(HF9, audio, 777)
    assert [r["payload"] for r in results] == [first, second]


def test_ring_wrap_discard_and_discontinuity():
    ring = AudioHistory(10)
    ring.append(0, np.arange(7))
    ring.append(7, np.arange(7, 15))
    assert ring.start == 5
    assert np.array_equal(ring.read(), np.arange(5, 15))
    ring.discard(8)
    assert np.array_equal(ring.read(), np.arange(8, 15))
    ring.append(100, np.arange(30))
    assert np.array_equal(ring.read(), np.arange(20, 30))


@pytest.mark.parametrize("generation,start", [(1, 14000), (0, 15000)])
def test_reset_or_missing_samples_abandons_pending_frame(generation, start):
    payload, audio = capture(HF9)
    stream = ReceiveStream(120000)
    stream.append(0, 0, audio[:14000])
    assert stream.decode(HF9).get("payload") is None
    old = stream.receivers[HF9.name]
    stream.append(generation, start, audio[14000:])
    assert stream.decode(HF9).get("payload") is None
    assert stream.receivers[HF9.name] is not old
    end = stream.audio.end
    stream.append(generation, end, np.concatenate((np.zeros(12000), audio, np.zeros(600))))
    assert stream.decode(HF9)["payload"] == payload


def transport():
    radio = object.__new__(RadioTransport)
    radio._chunks = collections.deque()
    radio._chunks_len = radio._rx_end = radio._rx_generation = 0
    radio._buf_lock = threading.Lock()
    radio._decimator_lock = threading.Lock()
    radio._rx_decimator = rx_audio.ReceiveDecimator()
    radio._rx_overflows = 0
    radio.radio = SimpleNamespace(name="test")
    return radio


def test_transport_reads_only_new_samples_and_old_consumers_cannot_erase_new_capture():
    radio = transport()
    block = np.ones((4800, 1), dtype=np.float32)
    radio._in_callback(block, 4800, None, None)
    epoch, start, audio = radio.read_rx()
    assert start == 0 and len(audio) == 1200
    cursor = (epoch, start + len(audio))
    assert not len(radio.read_rx(cursor)[2])
    radio._in_callback(block, 4800, None, None)
    assert len(radio.read_rx(cursor)[2]) == 1200
    radio.snapshot_rx()
    radio._clear_buffer()
    radio._in_callback(block, 4800, None, None)
    radio.consume_rx(2400)
    assert len(radio.read_rx(cursor)[2]) == 1200
    assert radio.read_rx(cursor)[0] != epoch


def test_overflow_resets_capture_and_decimation_history():
    radio = transport()
    block = np.ones((4800, 1), dtype=np.float32)
    radio._in_callback(block, 4800, None, None)
    radio._in_callback(block, 4800, None, SimpleNamespace(input_overflow=True))
    epoch, start, audio = radio.read_rx((0, 1200))
    assert epoch == 1 and start == 1200 and radio.rx_overflows == 1
    assert np.array_equal(audio, rx_audio.downsample(block[:, 0]))


def test_live_decode_loop_drains_two_frames_without_new_audio(monkeypatch):
    from whale import link
    from whale.modes.hc0_mode import HC0
    from whale.waveform import ModeRegistry

    radio = transport()
    radio._transmitting = threading.Event()
    frames = []
    for sequence in (0, 1):
        header, body = link._encode_air_header(
            link.PT_DATA, HF9.mode_id, bytes([sequence]) + b"streaming")
        frames.append(rx_audio.downsample(np.concatenate((
            HF9.encode(header + body),
            np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES)))))
    audio = np.concatenate((*frames, np.zeros(600)))
    radio._chunks.append(audio)
    radio._chunks_len = radio._rx_end = len(audio)
    receiver = link.Link(radio, "TEST", mode_registry=ModeRegistry((HC0, HF9), HC0))
    receiver.state = "CONNECTED"
    receiver.peer_supported_modes = {HC0.mode_id, HF9.mode_id}
    delivered = []

    def finish(ptype, body, profile, snap, end, result):
        delivered.append((ptype, body, profile))
        if len(delivered) == 2:
            receiver._stop.set()

    monkeypatch.setattr(receiver, "_finish_air_packet", finish)
    # All audio is already available: sleeping instead of draining it is a bug.
    monkeypatch.setattr(link.time, "sleep", lambda _: receiver._stop.set())
    receiver._decode_loop()
    assert delivered == [(link.PT_DATA, bytes([seq]) + b"streaming", HF9)
                         for seq in (0, 1)]


def test_tx_clear_during_decode_invalidates_its_result(monkeypatch):
    from whale import link
    from whale.waveform import ModeRegistry

    radio = transport()
    header, body = link._encode_air_header(link.PT_DATA, HF9.mode_id, b"\x00message")
    audio = rx_audio.downsample(np.concatenate((HF9.encode(header + body), np.zeros(2400))))
    receiver = link.Link(radio, "TEST", mode_registry=ModeRegistry((HF9,), HF9))
    receiver._receive_stream = ReceiveStream(120000)
    receiver._receive_stream.append(0, 0, audio)
    original = HF9.codec.decode

    def clear_during_decode(*args, **kwargs):
        result = original(*args, **kwargs)
        radio._clear_buffer()
        return result

    monkeypatch.setattr(HF9.codec, "decode", clear_during_decode)
    delivered = []
    monkeypatch.setattr(receiver, "_finish_air_packet", lambda *args: delivered.append(args))
    # The poll may still make bookkeeping progress on another candidate; the
    # completed frame from the invalidated capture must never be delivered.
    receiver._decode_one(receiver._receive_stream.audio.read())
    assert not delivered
