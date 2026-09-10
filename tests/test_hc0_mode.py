"""HC0 mode contract and impairment coverage."""

import numpy as np
import pytest
from scipy.signal import hilbert

from whale import afsk, framing, rx_audio, waveform
from whale.modes import hc0
from whale.modes.hc0_mode import HC0, hf_registry
from whale.modes.hc1w_mode import HC1W

RNG = np.random.default_rng(20260828)

#: Noise for a given signal-to-noise ratio against HC0's transmitted RMS,
#: white across the whole 24 kHz band.
TX_RMS = 0.13


def _packet(body_len=None):
    body_len = HC0.chunk_size if body_len is None else body_len
    return bytes(RNG.integers(0, 256, framing.AIR_HEADER_BYTES + body_len,
                              dtype=np.uint8))


def _snapshot(audio, before=4_000, after=2_000):
    return rx_audio.downsample(np.concatenate((np.zeros(before, np.float32),
                                               np.asarray(audio, np.float32),
                                               np.zeros(after, np.float32))))


def _noisy(audio, snr_db):
    audio = np.asarray(audio, dtype=np.float64)
    return audio + RNG.normal(0.0, TX_RMS / 10 ** (snr_db / 20), len(audio))


def _offset(audio, hz):
    """`audio` shifted up by `hz`, as two SSB radios shift each other."""
    audio = np.asarray(audio, dtype=np.float64)
    n = np.arange(len(audio))
    return np.real(hilbert(audio) * np.exp(2j * np.pi * hz * n
                                           / hc0.SAMPLE_RATE))


# -- the mode surface -----------------------------------------------------

def test_hc0_satisfies_the_waveform_mode_protocol():
    assert isinstance(HC0, waveform.WaveformMode)
    assert HC0.tx_sample_rate == afsk.SAMPLE_RATE
    assert HC0.rx_sample_rate == rx_audio.DECODE_SAMPLE_RATE
    assert HC0.chunk_size == hc0.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES


def test_hc0_is_the_control_mode_and_the_bottom_of_the_hf_ladder():
    registry = hf_registry()
    assert registry.control is HC0
    assert registry.supported_ids == (HC0.mode_id, HC1W.mode_id)
    assert registry.step(HC0, -1) is None       # nothing below it
    assert registry.step(HC0, +1) is HC1W        # and the fast rung above
    assert registry.step(HC1W, -1) is HC0


def test_hc0_carries_the_largest_control_packet_the_link_builds():
    """CONNECT_ACK at the longest legal callsigns, from the link's encoder.

    The control mode has to fit it or the handshake cannot complete, and
    HC0 is the smallest-payload mode in the tree -- so this is where a
    change to the connection envelope outgrows a frame.
    """
    from whale import link

    body = link._encode_connect_ack("A" * 15, "B" * 15, [HC0.mode_id, HC1W.mode_id],
                                    HC1W.mode_id, HC1W.mode_id, 0x5A)
    on_air = framing.AIR_HEADER_BYTES + len(body) - 2   # two bytes ride inline
    assert on_air <= hc0.MAX_PAYLOAD_BYTES, (
        f"a worst-case CONNECT_ACK needs {on_air} B and HC0 carries "
        f"{hc0.MAX_PAYLOAD_BYTES}")


def test_an_hc0_keying_is_fixed_length_whatever_it_carries():
    assert HC0.airtime(1) == HC0.airtime(HC0.chunk_size) == pytest.approx(
        hc0.FRAME_SECONDS, abs=1e-4)


def test_an_oversize_packet_is_refused_rather_than_truncated():
    with pytest.raises(ValueError, match="carries at most"):
        HC0.encode(_packet(HC0.chunk_size + 1))


def test_the_transmitted_waveform_is_constant_envelope():
    """One tone at a time, so the crest factor is a sine's.

    This matters because a peak-limited transmitter can apply more average
    power to a lower-crest-factor waveform.
    """
    audio = np.asarray(HC0.encode(_packet()), np.float64)
    body = audio[hc0.HEAD_SAMPLES:-hc0.TAIL_SAMPLES]
    crest = np.max(np.abs(body)) / np.sqrt(np.mean(body ** 2))
    assert crest == pytest.approx(np.sqrt(2.0), abs=0.02)


# -- the decode contract --------------------------------------------------

def test_a_full_chunk_round_trips_and_reports_where_the_frame_ended():
    packet = _packet()
    audio = HC0.encode(packet)
    result = HC0.decode(_snapshot(audio))

    assert result["payload"] == packet
    assert result["confidence"] >= HC0.confidence_threshold
    # The link consumes up to end_index and dates the peer's unkeying from
    # what is left after it, so this has to be the end of our audio.
    expected = ((4_000 + len(audio)) // rx_audio.DECIMATION
                + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    assert abs(result["end_index"] - expected) <= 2


def test_the_smallest_control_packet_round_trips_too():
    """A bare DISC is header-only; a DATA_ACK is the header plus two bytes.

    These cost a whole 3.38 s frame each, which is the trade hc0_mode
    documents. What must not happen is that they stop working.
    """
    for body_len in (0, 2):
        packet = _packet(body_len)
        assert HC0.decode(_snapshot(HC0.encode(packet)))["payload"] == packet


def test_a_partial_frame_reports_a_lock_but_no_end_index():
    """Confidence over threshold with no end_index is how the link is told
    to keep waiting instead of consuming a half-arrived frame."""
    audio = HC0.encode(_packet())
    arrived = hc0.HEAD_SAMPLES + 100 * hc0.SYMBOL_SAMPLES
    arrived_rx = ((4_000 + arrived) // rx_audio.DECIMATION
                  + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    result = HC0.decode(_snapshot(audio)[:arrived_rx])

    assert result["confidence"] >= HC0.confidence_threshold
    assert "end_index" not in result
    assert result["payload"] is None


def test_a_partial_current_frame_is_not_accepted_as_legacy():
    """The retained 64-byte format must not shorten a new frame in flight."""
    audio = HC0.encode(_packet())
    # Let the legacy grid be available, but stop before the current grid is
    # complete. Its CRC must not turn this partial current frame into a frame
    # the link consumes.
    arrived = (hc0.HEAD_SAMPLES
               + (hc0.SYNC_SYMBOLS + hc0.LEGACY_PAYLOAD_SYMBOLS + 1)
               * hc0.SYMBOL_SAMPLES)
    arrived_rx = ((4_000 + arrived) // rx_audio.DECIMATION
                  + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    result = HC0.decode(_snapshot(audio)[:arrived_rx])

    assert result["payload"] is None
    assert "end_index" not in result


def test_a_corrupted_frame_is_a_near_miss_the_link_can_skip_past():
    audio = np.asarray(HC0.encode(_packet()), np.float64)
    start = hc0.HEAD_SAMPLES + hc0.SYNC_SYMBOLS * hc0.SYMBOL_SAMPLES
    audio[start:] = RNG.normal(0.0, 0.2, len(audio) - start)
    result = HC0.decode(_snapshot(audio))

    assert result["payload"] is None
    # Both indices present and ordered: the link skips to sync_end_index so
    # the ruined payload cannot mask a following frame.
    assert result["sync_end_index"] < result["end_index"]


# -- the margin the mode exists for ---------------------------------------

@pytest.mark.parametrize("snr_db", [-6.0, -12.0, -15.0])
def test_it_decodes_at_its_low_snr_regression_points(snr_db):
    for _ in range(3):
        packet = _packet()
        audio = _noisy(HC0.encode(packet), snr_db)
        assert HC0.decode(_snapshot(audio))["payload"] == packet


def test_hc0_remains_the_lower_snr_rung():
    snr_db = -12.0
    hc0_packet = _packet()
    hc1w_packet = bytes(RNG.integers(0, 256,
                                    framing.AIR_HEADER_BYTES + HC1W.chunk_size,
                                    dtype=np.uint8))
    assert HC0.decode(_snapshot(_noisy(HC0.encode(hc0_packet),
                                       snr_db)))["payload"] == hc0_packet
    assert HC1W.decode(_snapshot(_noisy(HC1W.encode(hc1w_packet),
                                       snr_db)))["payload"] is None


@pytest.mark.parametrize("hz", [-30.0, -8.0, 0.0, 8.0, 30.0])
def test_a_carrier_offset_is_measured_and_removed(hz):
    """+-8 Hz is what the IC-7300/IC-705 pair measures on 10.145 MHz.

    Nothing in HC0's *detector* needs this -- energy detection does not
    care about phase, which is why acquisition survives where HC1W's does
    not -- but the payload tone bins do, and the estimate is what keeps
    them centred.
    """
    packet = _packet()
    audio = _noisy(_offset(HC0.encode(packet), hz), -10.0)
    result = HC0.decode(_snapshot(audio))

    assert result["payload"] == packet
    # A few Hz, not a fraction of one: this is a single frame at -10 dB and
    # the estimate's spread there is about 1 Hz. What matters is that it is
    # small against the +-46.9 Hz it has to resolve and against the ~20 Hz
    # of residual the tone bins would tolerate anyway.
    assert result["cfo_hz"] == pytest.approx(hz, abs=3.0)


def test_the_offset_estimate_survives_a_timing_error():
    """The reason the preamble is built from repeated tone pairs.

    A symbol's phase carries a timing term that depends on its tone, so
    across two *different* tones a timing error leaks into the frequency
    estimate -- 48 samples of it read a zero offset as -13.5 Hz. Across a
    repeated pair it cancels.
    """
    from whale.dsp import mfsk

    assert len(mfsk.repeated_pairs(hc0.SYNC_PATTERN)) == hc0.SYNC_SYMBOLS // 2
    audio = np.asarray(HC0.encode(_packet()), np.float64)
    start = hc0.HEAD_SAMPLES
    for error in (-48, 0, 48):
        estimate = mfsk.offset_hz(hc0.BANK, audio, start + error,
                                  hc0.SYNC_PATTERN)
        assert abs(estimate) < 1.0, f"{error} samples of timing read {estimate} Hz"


def test_nothing_that_is_not_a_frame_clears_the_threshold():
    """The other half of a low threshold: it has to stay quiet.

    0.12 is low enough to detect two dB past where the payload gives out,
    which only works because the statistic has the across-tone mean removed
    -- the bug `experiments/mfsk` records, where raw magnitudes scored pure
    noise at 0.73 against a 0.70 threshold.
    """
    seconds = 4 * hc0.SAMPLE_RATE
    t = np.arange(seconds) / hc0.SAMPLE_RATE
    candidates = {
        "white noise": RNG.normal(0.0, 0.1, seconds),
        "a bare carrier": 0.3 * np.sin(2 * np.pi * 1_500.0 * t),
        "silence": np.zeros(seconds),
        "an HC1W frame": np.asarray(HC1W.encode(
            bytes(RNG.integers(0, 256, framing.AIR_HEADER_BYTES + HC1W.chunk_size,
                               dtype=np.uint8))), np.float64),
    }
    for name, audio in candidates.items():
        result = HC0.decode(rx_audio.downsample(audio))
        assert result["payload"] is None, name
        if name != "an HC1W frame":
            assert result["confidence"] < HC0.confidence_threshold, (
                f"{name} scored {result['confidence']:.3f}")


def test_hc0_has_a_fixed_native_preamble():
    packet = _packet()
    audio = HC0.encode(packet)
    assert len(audio) == hc0.FRAME_SAMPLES
    assert HC0.decode(_snapshot(audio))["payload"] == packet


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
