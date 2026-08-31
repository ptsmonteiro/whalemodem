import numpy as np
import pytest

from whale import rx_audio
from whale.modes import hf_lead
from whale.modes.hc0_mode import HC0
from whale.modes.hc1_mode import HC1


HF_MODES = ((HC0, hf_lead.HC0_LABEL), (HC1, hf_lead.HC1_LABEL))


def _capture(audio):
    return rx_audio.downsample(np.asarray(audio, np.float32))


def _offset(audio, hz):
    samples = np.asarray(audio, np.float64)
    analytic = np.fft.ifft(np.fft.fft(samples) * 2 * (
        np.fft.fftfreq(len(samples)) > 0))
    t = np.arange(len(samples)) / HC0.tx_sample_rate
    return np.real(analytic * np.exp(2j * np.pi * hz * t)).astype(np.float32)


@pytest.mark.parametrize("mode,label", HF_MODES)
def test_common_lead_identifies_the_following_hf_mode(mode, label):
    payload = bytes(range(12))
    capture = rx_audio.downsample(mode.encode(payload))
    found, score = hf_lead.detect_label(capture)
    assert found == label
    assert score >= hf_lead.MATCH_THRESHOLD
    assert mode.decode(capture)["payload"] == payload


def test_both_hf_modes_use_the_same_lead_geometry_and_rate():
    hc0_audio = HC0.encode(b"x")[:hf_lead.MIN_SAMPLES]
    hc1_audio = HC1.encode(b"x")[:hf_lead.MIN_SAMPLES]
    assert len(hc0_audio) == len(hc1_audio) == 6_144
    assert np.max(np.abs(hc0_audio)) == pytest.approx(
        np.max(np.abs(hc1_audio)), rel=1e-6)


def test_lead_hint_is_not_required_for_checked_frame_recovery():
    payload = bytes(range(12))
    audio = HC0.encode(payload)
    clipped = audio.copy()
    clipped[:hf_lead.MIN_SAMPLES] = 0.0
    capture = rx_audio.downsample(clipped)
    assert HC0.decode(capture)["payload"] == payload


@pytest.mark.parametrize("mode,label", HF_MODES)
def test_erased_lead_does_not_prevent_either_checked_frame(mode, label):
    payload = bytes(range(12))
    audio = mode.encode(payload)
    audio[:hf_lead.MIN_SAMPLES] = 0.0
    capture = _capture(audio)

    assert mode.decode(capture)["payload"] == payload


@pytest.mark.parametrize("mode,label", HF_MODES)
def test_wrong_valid_label_is_only_a_hint(mode, label):
    payload = bytes(range(12))
    wrong = hf_lead.HC1_LABEL if label == hf_lead.HC0_LABEL else hf_lead.HC0_LABEL
    audio = mode.encode(payload)
    audio[:hf_lead.MIN_SAMPLES] = hf_lead.modulate(wrong)
    capture = _capture(audio)

    assert hf_lead.detect_label(capture)[0] == wrong
    assert mode.decode(capture)["payload"] == payload


@pytest.mark.parametrize("mode,_label", HF_MODES)
@pytest.mark.parametrize("repetition", [0, 1])
def test_corruption_of_either_repetition_does_not_lose_the_body(
        mode, _label, repetition):
    payload = bytes(range(12))
    audio = mode.encode(payload)
    start = repetition * hf_lead.BLOCK_SAMPLES
    audio[start:start + hf_lead.BLOCK_SAMPLES] = 0.0

    assert mode.decode(_capture(audio))["payload"] == payload


@pytest.mark.parametrize("mode,_label", HF_MODES)
@pytest.mark.parametrize("lost_symbols", [0, 1, 5, 6, 11, 12])
def test_leading_blackout_at_symbol_boundaries_preserves_the_body(
        mode, _label, lost_symbols):
    payload = bytes(range(12))
    audio = mode.encode(payload)
    audio[:lost_symbols * (hf_lead.BLOCK_SAMPLES // hf_lead.BLOCK_SYMBOLS)] = 0.0

    assert mode.decode(_capture(audio))["payload"] == payload


@pytest.mark.parametrize("mode,_label", HF_MODES)
def test_partially_lost_extended_lead_still_delivers_and_measures_short(
        mode, _label):
    payload = bytes(range(12))
    audio = mode.encode(payload, head_seconds=0.5)
    clean = mode.decode(_capture(audio))["head_blocks_observed"]
    audio[:6 * hf_lead.BLOCK_SAMPLES] = 0.0
    result = mode.decode(_capture(audio))

    assert result["payload"] == payload
    assert 0 < result["head_blocks_observed"] < clean


@pytest.mark.parametrize("mode,_label", HF_MODES)
@pytest.mark.parametrize("hz", [-46.0, 46.0])
def test_supported_frequency_offsets_do_not_make_the_lead_mandatory(
        mode, _label, hz):
    payload = bytes(range(12))
    shifted = _offset(mode.encode(payload), hz)

    assert mode.decode(_capture(shifted))["payload"] == payload


@pytest.mark.parametrize("mode,_label", HF_MODES)
def test_clipping_and_an_amplitude_ramp_do_not_lose_a_clean_frame(mode, _label):
    payload = bytes(range(12))
    audio = np.asarray(mode.encode(payload), np.float64)
    ramp = np.minimum(1.0, np.arange(len(audio)) / (0.08 * mode.tx_sample_rate))
    impaired = np.clip(audio * ramp * 2.0, -0.16, 0.16)

    assert mode.decode(_capture(impaired))["payload"] == payload


@pytest.mark.parametrize("bad", [
    np.array([], dtype=np.float32),
    np.zeros(1, dtype=np.float32),
    np.zeros(2 * hf_lead.RX_BLOCK_SAMPLES, dtype=np.float32),
    np.full(2 * hf_lead.RX_BLOCK_SAMPLES, np.nan, dtype=np.float32),
    np.full(2 * hf_lead.RX_BLOCK_SAMPLES, np.inf, dtype=np.float32),
    np.zeros((32, 2), dtype=np.float32),
])
def test_invalid_or_signal_free_input_proposes_no_boundary(bad):
    assert hf_lead.candidates(bad) == ()
    assert hf_lead.detect_label(bad) == (None, 0.0)


def test_candidate_work_is_bounded_even_with_many_false_signatures():
    false_leads = np.concatenate([
        hf_lead.modulate(i % 2) for i in range(20)
    ])
    capture = _capture(false_leads)

    ranked = hf_lead.candidates(capture, limit=3)
    # every label at no more than three boundaries
    assert len(ranked) <= 3 * len(hf_lead.BLOCKS)
    assert {candidate.label for candidate in ranked} <= set(range(len(hf_lead.BLOCKS)))


def test_a_false_signature_before_a_real_frame_cannot_hide_its_boundary():
    payload = bytes(range(12))
    prefix = np.concatenate((hf_lead.modulate(hf_lead.HC1_LABEL),
                             np.zeros(2_000, np.float32)))
    capture = _capture(np.concatenate((prefix, HC0.encode(payload))))
    expected = (len(prefix) + hf_lead.MIN_SAMPLES) // 4

    ranked = hf_lead.candidates(capture)
    assert any(candidate.label == hf_lead.HC0_LABEL
               and abs(candidate.body_start - expected)
               <= hf_lead.RX_BLOCK_SAMPLES // hf_lead.BLOCK_SYMBOLS
               for candidate in ranked)
    assert HC0.decode(capture)["payload"] == payload
