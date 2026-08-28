"""VF3 as a WaveformMode: the contract whale/link.py actually relies on.

The DSP itself is covered by `experiments/vf3/test_vf3.py` and was validated
on air (`experiments/vf3/RESULTS.md`).  What is tested here is the adapter:
that VF3 presents the surface the link drives, that its decode results say
the three things the link's receive loop reads them for, and that the
adaptive head the link negotiates does not disturb the waveform the bench
signed off on.

Software only -- no radios, no sound cards.
"""

import numpy as np
import pytest

from whale import afsk, framing, waveform
from whale.modes import vf3
from whale.modes.vf3_mode import VF3, registry_with_vf3

RNG = np.random.default_rng(20260828)


def _packet(body_len=None):
    """A packet shaped like one the link would hand to encode()."""
    body_len = VF3.chunk_size if body_len is None else body_len
    return bytes(RNG.integers(0, 256, framing.AIR_HEADER_BYTES + body_len,
                              dtype=np.uint8))


def _snapshot(audio, before=3_000, after=1_500):
    """One frame sitting in a receive buffer, with silence either side."""
    return np.concatenate((np.zeros(before, np.float32),
                           np.asarray(audio, np.float32),
                           np.zeros(after, np.float32)))


# -- the mode surface -----------------------------------------------------

def test_vf3_satisfies_the_waveform_mode_protocol():
    assert isinstance(VF3, waveform.WaveformMode)
    assert VF3.sample_rate == afsk.SAMPLE_RATE  # one transport, one rate
    assert VF3.chunk_size == vf3.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES


def test_vf3_is_the_top_rung_of_the_ladder_and_steps_both_ways():
    registry = registry_with_vf3()
    assert registry.supported_ids == (0, 1, 2, 3)
    assert registry.control is afsk.CONTROL_PROFILE  # control plane unchanged
    assert registry.step(VF3, +1) is None
    assert registry.step(VF3, -1) is afsk.PROFILE_1200
    assert registry.step(afsk.PROFILE_1200, +1) is VF3


def test_a_vf3_keying_is_fixed_length_whatever_it_carries():
    assert VF3.airtime(1) == VF3.airtime(VF3.chunk_size) == pytest.approx(5.2)


def test_a_full_chunk_is_worth_more_than_four_1200_baud_keyings():
    # The reason for the mode, kept as an assertion so a chunk_size or frame
    # change that quietly gives the win back fails here.
    vf3_rate = VF3.chunk_size * 8 / VF3.airtime(VF3.chunk_size)
    cpfsk = afsk.PROFILE_1200
    cpfsk_rate = (cpfsk.chunk_size * 8
                  / cpfsk.airtime(framing.AIR_HEADER_BYTES + cpfsk.chunk_size))
    assert vf3_rate > 2 * cpfsk_rate


def test_an_oversize_packet_is_refused_rather_than_truncated():
    with pytest.raises(ValueError, match="carries at most"):
        VF3.encode(_packet(VF3.chunk_size + 1))


# -- the decode contract --------------------------------------------------

def test_a_full_chunk_round_trips_and_reports_where_the_frame_ended():
    packet = _packet()
    snap = _snapshot(VF3.encode(packet))
    result = VF3.decode(snap)

    assert result["payload"] == packet
    assert result["confidence"] >= VF3.confidence_threshold
    # The link consumes up to end_index and dates the peer's unkeying from
    # what is left after it, so this has to be the true end of our audio.
    assert result["end_index"] == 3_000 + len(VF3.encode(packet))


def test_a_partial_frame_reports_a_lock_but_no_end_index():
    """Confidence over threshold with no end_index is how the link is told to
    keep waiting instead of consuming a half-arrived frame."""
    snap = _snapshot(VF3.encode(_packet()))[:80_000]
    result = VF3.decode(snap)

    assert result["confidence"] >= VF3.confidence_threshold
    assert "end_index" not in result
    assert result["payload"] is None


def test_a_corrupted_frame_is_a_near_miss_the_link_can_skip_past():
    audio = np.asarray(VF3.encode(_packet()), np.float64)
    start = vf3.lead_in_samples() + vf3.HEADER_SYMBOLS * vf3.SYMBOL_SAMPLES
    audio[start:] += RNG.normal(0.0, 0.6, len(audio) - start)
    result = VF3.decode(_snapshot(audio))

    assert result["payload"] is None
    # Both indices present and ordered: the link skips to sync_end_index so
    # the ruined payload cannot mask a following frame.
    assert result["sync_end_index"] < result["end_index"]


def test_noise_and_a_bare_tone_decode_to_nothing():
    noise = RNG.normal(0.0, 0.1, vf3.frame_samples())
    t = np.arange(vf3.frame_samples()) / vf3.SAMPLE_RATE
    tone = 0.3 * np.sin(2 * np.pi * 1_500.0 * t)
    for audio in (noise, tone):
        assert VF3.decode(audio)["payload"] is None


# -- the adaptive head ----------------------------------------------------

def test_the_default_head_reproduces_the_waveform_the_bench_validated():
    packet = _packet()
    assert np.array_equal(VF3.encode(packet), vf3.modulate(packet))
    assert np.array_equal(VF3.encode(packet, include_head=False),
                          vf3.modulate(packet))


@pytest.mark.parametrize("head_seconds", [0.045, 0.3, 1.0])
def test_a_negotiated_head_only_lengthens_the_lead_in(head_seconds):
    packet = _packet()
    audio = VF3.encode(packet, head_seconds=head_seconds)
    lead = vf3.lead_in_samples(head_seconds)

    assert len(audio) == vf3.frame_samples(head_seconds)
    # Everything after the head is the frame the bench validated, untouched.
    assert np.array_equal(audio[lead:], vf3.modulate(packet)[vf3.LEAD_IN_SAMPLES:])
    assert VF3.decode(_snapshot(audio), head_seconds=head_seconds)["payload"] == packet


def test_the_head_absorbs_leading_audio_that_a_squelch_blackout_would_eat():
    """The point of the head: clipping costs padding, not the header."""
    packet = _packet()
    audio = VF3.encode(packet, head_seconds=0.5)
    blackout = int(0.4 * vf3.SAMPLE_RATE)
    clipped = np.concatenate((np.zeros(blackout, np.float32), audio[blackout:]))

    assert VF3.decode(_snapshot(clipped))["payload"] == packet


def test_the_surviving_head_is_measured_but_not_offered_as_head_feedback():
    audio = VF3.encode(_packet(), head_seconds=0.5)
    result = VF3.decode(_snapshot(audio))

    # 0.5 s of head holds 23 whole 1024-sample cores.
    assert result["head_cores_observed"] == int(0.5 * vf3.SAMPLE_RATE) // vf3.CORE_SAMPLES
    # Deliberately absent: link._head_feedback_request weighs an observation
    # against a CPFSK constant in CPFSK units.  See vf3_mode's docstring.
    assert "head_symbols_received" not in result


def test_a_clipped_head_measures_short():
    audio = VF3.encode(_packet(), head_seconds=0.5)
    blackout = int(0.3 * vf3.SAMPLE_RATE)
    clipped = np.concatenate((np.zeros(blackout, np.float32), audio[blackout:]))
    full = VF3.decode(_snapshot(audio))["head_cores_observed"]

    assert 0 < VF3.decode(_snapshot(clipped))["head_cores_observed"] < full


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
