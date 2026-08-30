"""HC0 as a WaveformMode, and the margin it exists to provide.

HC0 is the HF ladder's control mode and bottom rung, so the first half of
this file is the same contract `test_vf3_mode.py` and `test_hc1_mode.py`
hold their modes to -- the surface whale/link.py drives and the three
things its receive loop reads a decode result for.

The second half is the reason the mode was written.  HC1 decodes nothing
below +3.5 dB, which on the bench's weak leg meant 0 frames out of 10; the
tests here pin HC0 working roughly 19 dB further down, tolerating a carrier
offset without estimating one to detect, and not false-triggering on the
things a receiver actually hears when no frame is present.  Those are
assertions rather than prose because "more robust" is a claim that rots
silently.

The on-air half is `test_hc0_capture_replay.py`.  Software only here.
"""

import numpy as np
import pytest
from scipy.signal import hilbert

from whale import afsk, framing, rx_audio, waveform
from whale.modes import hc0, hf_lead
from whale.modes.hc0_mode import HC0, hf_registry
from whale.modes.hc1_mode import HC1

RNG = np.random.default_rng(20260828)

#: Noise for a given signal-to-noise ratio, against HC0's transmitted RMS,
#: white across the whole 24 kHz band.  Every dB figure in this file is in
#: these units, which is what makes them comparable with HC1's.
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
    assert registry.supported_ids == (HC0.mode_id, HC1.mode_id)
    assert registry.step(HC0, -1) is None       # nothing below it
    assert registry.step(HC0, +1) is HC1        # and the fast rung above
    assert registry.step(HC1, -1) is HC0


def test_hc0_carries_the_largest_control_packet_the_link_builds():
    """CONNECT_ACK at the longest legal callsigns, from the link's encoder.

    The control mode has to fit it or the handshake cannot complete, and
    HC0 is the smallest-payload mode in the tree -- so this is where a
    change to the connection envelope outgrows a frame.
    """
    from whale import link

    body = link._encode_connect_ack("A" * 15, "B" * 15, [HC0.mode_id, HC1.mode_id],
                                    HC1.mode_id, HC1.mode_id, 0x5A)
    on_air = framing.AIR_HEADER_BYTES + len(body) - 2   # two bytes ride inline
    assert on_air <= hc0.MAX_PAYLOAD_BYTES, (
        f"a worst-case CONNECT_ACK needs {on_air} B and HC0 carries "
        f"{hc0.MAX_PAYLOAD_BYTES}")


def test_an_hc0_keying_is_fixed_length_whatever_it_carries():
    assert HC0.airtime(1) == HC0.airtime(HC0.chunk_size) == pytest.approx(3.4227,
                                                                         abs=1e-4)


def test_an_oversize_packet_is_refused_rather_than_truncated():
    with pytest.raises(ValueError, match="carries at most"):
        HC0.encode(_packet(HC0.chunk_size + 1))


def test_the_transmitted_waveform_is_constant_envelope():
    """One tone at a time, so the crest factor is a sine's.

    This is not cosmetic. A transmitter is peak-limited, so at the same
    peak a crest factor of 1.41 puts about 8 dB more average power on the
    air than HC1's 3.9 -- robustness this mode gets for free on top of
    everything measured below, which is all at equal RMS.
    """
    audio = np.asarray(HC0.encode(_packet()), np.float64)
    body = audio[hf_lead.MIN_SAMPLES:-hc0.TAIL_SAMPLES]
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
    arrived = hf_lead.MIN_SAMPLES + 100 * hc0.SYMBOL_SAMPLES
    arrived_rx = ((4_000 + arrived) // rx_audio.DECIMATION
                  + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    result = HC0.decode(_snapshot(audio)[:arrived_rx])

    assert result["confidence"] >= HC0.confidence_threshold
    assert "end_index" not in result
    assert result["payload"] is None


def test_a_corrupted_frame_is_a_near_miss_the_link_can_skip_past():
    audio = np.asarray(HC0.encode(_packet()), np.float64)
    start = hf_lead.MIN_SAMPLES + hc0.SYNC_SYMBOLS * hc0.SYMBOL_SAMPLES
    audio[start:] = RNG.normal(0.0, 0.2, len(audio) - start)
    result = HC0.decode(_snapshot(audio))

    assert result["payload"] is None
    # Both indices present and ordered: the link skips to sync_end_index so
    # the ruined payload cannot mask a following frame.
    assert result["sync_end_index"] < result["end_index"]


# -- the margin the mode exists for ---------------------------------------

@pytest.mark.parametrize("snr_db", [-6.0, -12.0, -15.0])
def test_it_decodes_far_below_where_the_ofdm_rung_gives_out(snr_db):
    """The headline.

    HC1 -- the rung above this one, and the only HF mode before it -- needs
    +3.5 dB, because its confidence is a self-correlation whose expected
    value is SNR/(SNR+1) and whose threshold is therefore an SNR floor.
    Each of these points is below that by more than 9 dB.
    """
    for _ in range(3):
        packet = _packet()
        audio = _noisy(HC0.encode(packet), snr_db)
        assert HC0.decode(_snapshot(audio))["payload"] == packet


def test_the_ofdm_rung_cannot_do_what_this_one_does():
    """The same channel, both modes, so the comparison is not folklore."""
    snr_db = -12.0
    hc0_packet = _packet()
    hc1_packet = bytes(RNG.integers(0, 256,
                                    framing.AIR_HEADER_BYTES + HC1.chunk_size,
                                    dtype=np.uint8))
    assert HC0.decode(_snapshot(_noisy(HC0.encode(hc0_packet),
                                       snr_db)))["payload"] == hc0_packet
    assert HC1.decode(_snapshot(_noisy(HC1.encode(hc1_packet),
                                       snr_db)))["payload"] is None


@pytest.mark.parametrize("hz", [-30.0, -8.0, 0.0, 8.0, 30.0])
def test_a_carrier_offset_is_measured_and_removed(hz):
    """+-8 Hz is what the IC-7300/IC-705 pair measures on 10.145 MHz.

    Nothing in HC0's *detector* needs this -- energy detection does not
    care about phase, which is why acquisition survives where HC1's does
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
    start = hf_lead.MIN_SAMPLES
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
        "an HC1 frame": np.asarray(HC1.encode(
            bytes(RNG.integers(0, 256, framing.AIR_HEADER_BYTES + HC1.chunk_size,
                               dtype=np.uint8))), np.float64),
    }
    for name, audio in candidates.items():
        result = HC0.decode(rx_audio.downsample(audio))
        assert result["payload"] is None, name
        assert result["confidence"] < HC0.confidence_threshold, (
            f"{name} scored {result['confidence']:.3f}")


# -- the adaptive head ----------------------------------------------------

def test_the_head_is_whole_blocks_at_every_requested_duration():
    for seconds in (None, 0.0, 0.0853, 0.2, 1 / 3, 0.5, 1.0):
        lead = hf_lead.lead_samples(seconds)
        assert lead % hf_lead.BLOCK_SAMPLES == 0
        assert lead >= hf_lead.MIN_SAMPLES


@pytest.mark.parametrize("head_seconds", [0.0853, 0.3, 1.0])
def test_a_negotiated_head_only_lengthens_the_lead_in(head_seconds):
    packet = _packet()
    audio = HC0.encode(packet, head_seconds=head_seconds)
    lead = hf_lead.lead_samples(head_seconds)

    expected = lead + hc0.TOTAL_SYMBOLS * hc0.SYMBOL_SAMPLES + hc0.TAIL_SAMPLES
    assert len(audio) == expected
    assert np.array_equal(audio[lead:], hc0.modulate(packet)[hc0.LEAD_IN_SAMPLES:])
    assert HC0.decode(_snapshot(audio), head_seconds=head_seconds)["payload"] == packet


def test_the_head_is_measured_through_a_carrier_offset():
    """The bug this ordering exists to prevent.

    The head is the one thing in HC0 matched against a reference
    *waveform*, so it is the one thing an offset decorrelates -- the
    bench's own 8 Hz turns a 42.7 ms block by a third of a turn. Measured
    before the estimate was moved ahead of it, a 1 s head arriving
    perfectly reported as one block, and the link went on transmitting a
    full second of padding for the rest of the session.
    """
    audio = HC0.encode(_packet(), head_seconds=1.0)
    blocks = hf_lead.lead_samples(1.0) // hf_lead.BLOCK_SAMPLES
    for hz in (0.0, 8.0, -30.0):
        result = HC0.decode(_snapshot(_offset(audio, hz)))
        assert result["head_blocks_observed"] == blocks, f"{hz} Hz"
        assert result["head_seconds_received"] == pytest.approx(
            blocks * hf_lead.BLOCK_SAMPLES / hc0.SAMPLE_RATE)
    assert "head_symbols_received" not in HC0.decode(_snapshot(audio))


def test_a_clipped_head_measures_short_and_the_frame_still_decodes():
    packet = _packet()
    audio = HC0.encode(packet, head_seconds=0.5)
    full = HC0.decode(_snapshot(audio))["head_blocks_observed"]
    blackout = int(0.3 * hc0.SAMPLE_RATE)
    clipped = np.concatenate((np.zeros(blackout, np.float32), audio[blackout:]))
    result = HC0.decode(_snapshot(clipped))

    assert 0 < result["head_blocks_observed"] < full
    assert result["payload"] == packet


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
