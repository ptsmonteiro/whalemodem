"""HC1 as a WaveformMode, and the HF properties it exists for.

Two halves.  The first is the same contract `test_vf3_mode.py` holds VF3 to
-- the surface whale/link.py drives, and the three things its receive loop
reads a decode result for.  The second is what makes HC1 different from
every mode before it: it corrects a carrier frequency offset, it survives a
multipath echo, and it is the *control* mode, so the smallest packet the
link ever builds has to go through it as reliably as the largest.

The on-air half is `test_hc1_capture_replay.py`, which decodes recorded
IC-7300 -> IC-705 audio.  Software only here -- no radios, no sound cards.
"""

import numpy as np
import pytest
from scipy.signal import hilbert

from whale import afsk, framing, rx_audio, waveform
from whale.modes import hc1, hf_lead
from whale.modes.hc1_mode import HC1, hf_registry

RNG = np.random.default_rng(20260828)


def _packet(body_len=None):
    """A packet shaped like one the link would hand to encode()."""
    body_len = HC1.chunk_size if body_len is None else body_len
    return bytes(RNG.integers(0, 256, framing.AIR_HEADER_BYTES + body_len,
                              dtype=np.uint8))


def _snapshot(audio, before=3_000, after=1_500):
    """One frame sitting in a receive buffer, with silence either side."""
    return rx_audio.downsample(np.concatenate((np.zeros(before, np.float32),
                                               np.asarray(audio, np.float32),
                                               np.zeros(after, np.float32))))


def _offset(audio, hz):
    """`audio` shifted up by `hz`, the way two SSB radios disagreeing about
    frequency shift everything one of them transmits."""
    audio = np.asarray(audio, dtype=np.float64)
    n = np.arange(len(audio))
    return np.real(hilbert(audio) * np.exp(2j * np.pi * hz * n / hc1.SAMPLE_RATE))


def _noisy(audio, snr_db):
    audio = np.asarray(audio, dtype=np.float64)
    power = np.mean(audio ** 2)
    return audio + RNG.normal(0.0, np.sqrt(power / 10 ** (snr_db / 10)), len(audio))


# -- the mode surface -----------------------------------------------------

def test_hc1_satisfies_the_waveform_mode_protocol():
    assert isinstance(HC1, waveform.WaveformMode)
    assert HC1.tx_sample_rate == afsk.SAMPLE_RATE
    assert HC1.rx_sample_rate == rx_audio.DECODE_SAMPLE_RATE
    assert HC1.chunk_size == hc1.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES


def test_the_hf_ladder_is_hc1_as_both_control_and_data():
    registry = hf_registry()
    assert registry.supported_ids == (HC1.mode_id,)
    assert registry.control is HC1
    # One rung, so adaptation has nowhere to go in either direction.
    assert registry.step(HC1, +1) is None and registry.step(HC1, -1) is None


def test_hc1_carries_the_largest_control_packet_the_link_builds():
    """CONNECT_ACK is the biggest, and the control mode has to fit it.

    Built here from the link's own encoder at the longest legal callsigns
    rather than from a remembered number, so a change to the connection
    envelope that outgrows the frame fails here instead of on the air.
    """
    from whale import link

    body = link._encode_connect_ack("A" * 15, "B" * 15, [0, 1, 2, 3, 4],
                                    4, 4, 0x5A)
    # Two body bytes ride inline in the air header; the rest is the frame's.
    on_air = framing.AIR_HEADER_BYTES + len(body) - 2
    assert on_air <= hc1.MAX_PAYLOAD_BYTES, (
        f"a worst-case CONNECT_ACK needs {on_air} B and HC1 carries "
        f"{hc1.MAX_PAYLOAD_BYTES}")


def test_an_hc1_keying_is_fixed_length_whatever_it_carries():
    assert HC1.airtime(1) == HC1.airtime(HC1.chunk_size) == pytest.approx(0.7747,
                                                                          abs=1e-4)


def test_an_oversize_packet_is_refused_rather_than_truncated():
    with pytest.raises(ValueError, match="carries at most"):
        HC1.encode(_packet(HC1.chunk_size + 1))


# -- the decode contract --------------------------------------------------

def test_a_full_chunk_round_trips_and_reports_where_the_frame_ended():
    packet = _packet()
    audio = HC1.encode(packet)
    result = HC1.decode(_snapshot(audio))

    assert result["payload"] == packet
    assert result["confidence"] >= HC1.confidence_threshold
    # The link consumes up to end_index and dates the peer's unkeying from
    # what is left after it, so this has to be the end of our audio -- give
    # or take the sample or two acquisition may be out by.
    expected = ((3_000 + len(audio)) // rx_audio.DECIMATION
                + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    assert abs(result["end_index"] - expected) <= 2


def test_the_smallest_control_packet_round_trips_too():
    """A bare DISC is header-only; a DATA_ACK is the header plus two bytes.

    On VHF the control mode charges by the byte and these are cheap.  Here
    they cost a whole frame, which is the trade hc1_mode documents -- what
    must not happen is that they stop *working*.
    """
    for body_len in (0, 2):
        packet = _packet(body_len)
        assert HC1.decode(_snapshot(HC1.encode(packet)))["payload"] == packet


def test_a_partial_frame_reports_a_lock_but_no_end_index():
    """Confidence over threshold with no end_index is how the link is told to
    keep waiting instead of consuming a half-arrived frame."""
    audio = HC1.encode(_packet())
    arrived = hf_lead.MIN_SAMPLES + 20 * hc1.SYMBOL_SAMPLES
    arrived_rx = ((3_000 + arrived) // rx_audio.DECIMATION
                  + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    result = HC1.decode(_snapshot(audio)[:arrived_rx])

    assert result["confidence"] >= HC1.confidence_threshold
    assert "end_index" not in result
    assert result["payload"] is None


def test_a_corrupted_frame_is_a_near_miss_the_link_can_skip_past():
    audio = np.asarray(HC1.encode(_packet()), np.float64)
    start = hf_lead.MIN_SAMPLES + hc1.HEADER_SYMBOLS * hc1.SYMBOL_SAMPLES
    audio[start:] += RNG.normal(0.0, 0.6, len(audio) - start)
    result = HC1.decode(_snapshot(audio))

    assert result["payload"] is None
    # Both indices present and ordered: the link skips to sync_end_index so
    # the ruined payload cannot mask a following frame.
    assert result["sync_end_index"] < result["end_index"]


def test_noise_and_a_bare_tone_decode_to_nothing():
    noise = RNG.normal(0.0, 0.1, hc1.FRAME_SAMPLES)
    t = np.arange(hc1.FRAME_SAMPLES) / hc1.SAMPLE_RATE
    tone = 0.3 * np.sin(2 * np.pi * 1_500.0 * t)
    for audio in (noise, tone):
        assert HC1.decode(rx_audio.downsample(audio))["payload"] is None


# -- the reason the mode exists -------------------------------------------

@pytest.mark.parametrize("hz", [-45.0, -8.0, 0.0, 8.0, 45.0])
def test_a_carrier_offset_inside_the_estimator_range_is_measured_and_removed(hz):
    """The property no CPFSK profile has.

    +-8 Hz is what the IC-7300/IC-705 pair actually measured on 10.145 MHz
    (see logs and docs/PERFORMANCE.md); +-45 Hz is just inside
    `hc1.COARSE_OFFSET_LIMIT_HZ` and is there so the documented limit is a
    tested one rather than an arithmetic claim.
    """
    packet = _packet()
    result = HC1.decode(_snapshot(_offset(HC1.encode(packet), hz)))

    assert result["payload"] == packet
    assert result["cfo_hz"] == pytest.approx(hz, abs=0.5)


def test_an_offset_past_the_estimator_range_fails_rather_than_decoding_wrongly():
    """Past half a carrier spacing the prefix angle wraps, so the estimate is
    not merely imprecise, it points the wrong way.  A frame that cannot be
    corrected must fail its CRC, not deliver something."""
    beyond = hc1.COARSE_OFFSET_LIMIT_HZ + 5.0
    result = HC1.decode(_snapshot(_offset(HC1.encode(_packet()), beyond)))
    assert result["payload"] is None


def test_the_frame_survives_a_multipath_echo_inside_the_guard():
    packet = _packet()
    audio = np.asarray(HC1.encode(packet), np.float64)
    delay = int(0.002 * hc1.SAMPLE_RATE)  # 2 ms, inside the 2.67 ms prefix
    echoed = audio.copy()
    echoed[delay:] += 0.7 * audio[:-delay]

    assert HC1.decode(_snapshot(_noisy(echoed, 12.0)))["payload"] == packet


def test_the_fec_is_load_bearing_at_the_snr_the_mode_is_for():
    """Rate-1/2 K=7 over the interleaved grid, exercised rather than assumed.

    8 dB is comfortably below the 13.5-24.5 dB per-carrier header SNR the
    bench measured, and comfortably above the ~4 dB where the mode gives
    out -- so this is the working range, and it is the coding that puts it
    there.
    """
    for _ in range(3):
        packet = _packet()
        assert HC1.decode(_snapshot(_noisy(HC1.encode(packet), 8.0)))["payload"] == packet


def test_a_sample_clock_mismatch_the_cyclic_prefix_absorbs():
    packet = _packet()
    audio = np.asarray(HC1.encode(packet), np.float64)
    stretched = np.interp(np.linspace(0, len(audio) - 1, int(len(audio) * 1.0002)),
                          np.arange(len(audio)), audio)  # 200 ppm

    assert HC1.decode(_snapshot(stretched))["payload"] == packet


# -- the adaptive head ----------------------------------------------------

def test_the_common_head_is_whole_mfsk_blocks():
    """Every HF duration is rounded up in the common lead's own unit."""
    for seconds in (None, 0.0, 0.048, 0.1, 0.2, 1 / 3, 0.5, 1.0):
        lead = hf_lead.lead_samples(seconds)
        assert lead % hf_lead.BLOCK_SAMPLES == 0
        assert lead >= hf_lead.MIN_SAMPLES


@pytest.mark.parametrize("head_seconds", [0.048, 0.3, 1.0])
def test_a_negotiated_head_only_lengthens_the_lead_in(head_seconds):
    packet = _packet()
    audio = HC1.encode(packet, head_seconds=head_seconds)
    lead = hf_lead.lead_samples(head_seconds)

    expected = lead + hc1.TOTAL_SYMBOLS * hc1.SYMBOL_SAMPLES + hc1.TAIL_SAMPLES
    assert len(audio) == expected
    # Everything after the head is the frame the default head produces.
    assert np.array_equal(audio[lead:], hc1.modulate(packet)[hc1.LEAD_IN_SAMPLES:])
    assert HC1.decode(_snapshot(audio), head_seconds=head_seconds)["payload"] == packet


def test_the_surviving_head_is_reported_in_cores_and_in_seconds():
    audio = HC1.encode(_packet(), head_seconds=0.5)
    result = HC1.decode(_snapshot(audio))

    blocks = hf_lead.lead_samples(0.5) // hf_lead.BLOCK_SAMPLES
    assert result["head_blocks_observed"] == blocks
    # The seconds are what link._head_feedback_request and link._encode_timing
    # consume; the block count stays as the diagnostic.
    assert result["head_seconds_received"] == pytest.approx(
        blocks * hf_lead.BLOCK_SAMPLES / hc1.SAMPLE_RATE)
    assert "head_symbols_received" not in result


def test_a_clipped_head_measures_short_and_the_frame_still_decodes():
    packet = _packet()
    audio = HC1.encode(packet, head_seconds=0.5)
    blackout = int(0.3 * hc1.SAMPLE_RATE)
    clipped = np.concatenate((np.zeros(blackout, np.float32), audio[blackout:]))
    full = HC1.decode(_snapshot(audio))["head_blocks_observed"]
    result = HC1.decode(_snapshot(clipped))

    assert 0 < result["head_blocks_observed"] < full
    assert result["payload"] == packet


def test_the_head_is_measured_through_a_carrier_offset():
    """The head is matched against a reference waveform, so an uncorrected
    offset would decorrelate it and report a perfectly received head as
    lost -- which the link would answer by lengthening it forever."""
    audio = HC1.encode(_packet(), head_seconds=0.5)
    clean = HC1.decode(_snapshot(audio))["head_blocks_observed"]
    shifted = HC1.decode(_snapshot(_offset(audio, 30.0)))["head_blocks_observed"]

    assert shifted == clean


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
