"""Focused production contracts for the HF most-robust HR0 mode."""

import numpy as np
import pytest

from whale import framing, modes, rx_audio
from whale.modes import hf_lead, hr0
from whale.modes.hc0_mode import HC0
from whale.modes.hc1w_mode import HC1W
from whale.modes.hr0_mode import HR0


def _capture(audio):
    return rx_audio.downsample(np.asarray(audio, np.float32))


def test_hr0_geometry_meets_hf_level_zero_speed_contract():
    assert HR0.mode_id == 10
    assert hr0.TONE_COUNT == 32
    assert hr0.BANK.bandwidth_hz <= 2_300
    assert hr0.SYMBOL_SAMPLES == 1_024
    assert hr0.CODEC.code is hr0.dsp.K9
    assert HR0.chunk_size == hr0.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES == 32
    assert HR0.airtime(framing.AIR_HEADER_BYTES + HR0.chunk_size) == pytest.approx(3.860)
    assert HR0.chunk_size * 8 / HR0.airtime(HR0.chunk_size) >= 20


def test_hr0_clean_round_trip_and_common_lead():
    payload = bytes(range(hr0.MAX_PAYLOAD_BYTES))
    capture = _capture(HR0.encode(payload))
    label, score = hf_lead.detect_label(capture)
    result = HR0.decode(capture)
    assert label == hf_lead.HR0_LABEL
    assert score >= hf_lead.MATCH_THRESHOLD
    assert result["payload"] == payload
    assert result["head_blocks_observed"] >= hf_lead.MIN_BLOCKS


def test_hr0_full_frame_at_revised_quiet_moderate_awgn_target():
    """The replacement's nominal +6 dB/3 kHz target, not qualification."""
    from whale.channel import AwgnChannel, SnrSpec

    payload = bytes(range(hr0.MAX_PAYLOAD_BYTES))
    audio = HR0.encode(payload)
    noisy = AwgnChannel(HR0.tx_sample_rate, SnrSpec(6.0), 20260901).process(audio)
    assert HR0.decode(_capture(noisy.audio))["payload"] == payload


def test_hr0_is_new_hf_control_and_bottom_rung():
    registry = modes.hf_registry()
    assert registry.control is HR0
    assert registry.supported_ids[:3] == (HR0.mode_id, HC0.mode_id, HC1W.mode_id)
    assert registry.step(HR0, -1) is None
    assert registry.step(HR0, +1) is HC0


@pytest.mark.parametrize("bad", [np.zeros(0), np.zeros(20_000),
                                  np.full(20_000, np.nan),
                                  np.zeros((100, 2))])
def test_hr0_rejects_invalid_or_signal_free_input(bad):
    assert HR0.decode(bad)["payload"] is None


def test_hr0_payload_limit_is_enforced():
    with pytest.raises(ValueError, match="carries at most"):
        HR0.encode(bytes(hr0.MAX_PAYLOAD_BYTES + 1))


@pytest.mark.parametrize("length", [0, 10, 12, 13, 42])
def test_hr0_airtime_tracks_encoded_length_and_round_trips(length):
    payload = bytes(range(length))
    audio = HR0.encode(payload)
    expected = 1.812 if length <= 12 else 3.860
    assert HR0.airtime(length) == pytest.approx(expected)
    assert len(audio) / HR0.tx_sample_rate == pytest.approx(expected)
    assert HR0.decode(_capture(audio))["payload"] == payload


def test_real_data_ack_uses_short_frame():
    from whale import link

    header, remainder = link._encode_air_header(
        link.PT_DATA_ACK, HR0.mode_id, bytes([0, 1, HR0.mode_id, 0]))
    payload = header + remainder
    assert len(payload) == hr0.SHORT_MAX_PAYLOAD_BYTES == 12
    assert HR0.airtime(len(payload)) < 0.52 * 3.508
    assert HR0.decode(_capture(HR0.encode(payload)))["payload"] == payload


def test_short_frame_ends_before_following_full_frame():
    short = bytes(range(12))
    full = bytes(range(42))
    first = HR0.encode(short)
    capture = _capture(np.concatenate((first, HR0.encode(full))))
    # Streaming RX sees the short body before the next preamble is complete.
    # Whole-buffer acquisition otherwise deliberately picks the strongest sync.
    available = (len(first) + hf_lead.MIN_SAMPLES) // rx_audio.DECIMATION
    result = HR0.decode(capture[:available])
    assert result["payload"] == short
    assert result["end_index"] == pytest.approx(
        len(first) // rx_audio.DECIMATION + rx_audio.FILTER_DELAY_DECODE_SAMPLES,
        abs=8)
    assert HR0.decode(capture[result["end_index"]:])["payload"] == full


def test_full_frame_prefix_is_pending_until_full_body_arrives():
    payload = bytes(range(42))
    capture = _capture(HR0.encode(payload))
    prefix = capture[:round(HR0.airtime(12) * HR0.rx_sample_rate)]
    result = HR0.decode(prefix)
    assert result["payload"] is None
    assert "end_index" not in result
    assert HR0.decode(capture)["payload"] == payload


def test_full_body_with_short_payload_still_decodes():
    from whale.dsp import mfsk

    payload = bytes(range(12))
    tones = np.concatenate((hr0.SYNC_PATTERN,
                            hr0.BANK.symbols_from_bits(hr0.CODEC.encode(payload))))
    audio = np.concatenate((hf_lead.modulate(hf_lead.HR0_LABEL),
                            mfsk.modulate(hr0.BANK, tones, hr0.TX_AMPLITUDE),
                            np.zeros(hr0.TAIL_SAMPLES)))
    result = HR0.decode(_capture(audio))
    assert result["payload"] == payload
    assert result["payload_symbols"] == hr0.PAYLOAD_SYMBOLS


@pytest.mark.parametrize("offset_hz", [-46, 0, 46])
def test_short_ack_with_noise_and_frequency_offset(offset_hz):
    from scipy.signal import hilbert

    payload = bytes(range(12))
    audio = HR0.encode(payload).astype(np.float64)
    phase = 2j * np.pi * offset_hz * np.arange(len(audio)) / HR0.tx_sample_rate
    shifted = np.real(hilbert(audio) * np.exp(phase))
    # Same full-Nyquist noise convention as the existing full-frame smoke;
    # -15 dB here is about -6 dB SNR/3 kHz, not a qualification claim.
    noise_rms = np.sqrt(np.mean(audio ** 2)) / 10 ** (-15 / 20)
    noisy = shifted + np.random.default_rng(20260906).normal(
        0, noise_rms, len(audio))
    result = HR0.decode(_capture(np.concatenate((np.zeros(4000), noisy))))
    assert result["payload"] == payload


def test_truncated_and_corrupt_short_bodies_do_not_deliver_payloads():
    audio = HR0.encode(bytes(range(12)))
    start = hf_lead.MIN_SAMPLES + hr0.SYNC_SYMBOLS * hr0.SYMBOL_SAMPLES
    assert HR0.decode(_capture(audio[:start + hr0.SYMBOL_SAMPLES]))["payload"] is None
    audio[start:] = 0
    assert HR0.decode(_capture(audio))["payload"] is None


@pytest.mark.channel_regression
@pytest.mark.parametrize("preset", ["mid_latitude_quiet", "mid_latitude_moderate",
                                    "mid_latitude_disturbed"])
def test_short_ack_at_original_hf_level_zero_channel_smoke_points(preset):
    # Retain the original +4 dB regression despite the revised +6/+11 dB
    # target; passing these few seeds does not establish a 3 dB HC0 margin.
    from whale.channel import AwgnChannel, ChannelChain, SnrSpec, WattersonChannel
    from whale.qualification import run_frame_trials

    def channel(seed):
        return ChannelChain((
            WattersonChannel.from_preset(HR0.tx_sample_rate, preset, seed),
            AwgnChannel(HR0.tx_sample_rate, SnrSpec(4.0), seed ^ 0x5A5A),
        ))

    records = run_frame_trials(
        HR0, channel, 2, 20260906, point_index=0,
        direction=f"{preset}, SNR/3 kHz 4 dB", payload_bytes=12)
    assert all(record.decoded for record in records)


@pytest.mark.parametrize("length", [0, 12, 13, 42])
@pytest.mark.parametrize("head_seconds", [None, 0.5])
def test_production_hr0_matches_evaluated_margin32_waveform(length, head_seconds):
    from experiments.hr0_fast_control.candidate import MARGIN32

    payload = bytes(range(length))
    expected = MARGIN32.encode(payload, head_seconds=head_seconds)
    assert np.array_equal(HR0.encode(payload, head_seconds=head_seconds), expected)
    assert HR0.decode(_capture(expected))["payload"] == payload
    assert MARGIN32.decode(_capture(HR0.encode(payload)))["payload"] == payload


def test_previous_128_fsk_body_is_not_accepted_as_new_hr0():
    from experiments.hr0_fast_control.legacy_hr0_mode import HR0 as legacy

    assert HR0.decode(_capture(legacy.encode(bytes(range(12)))))["payload"] is None


def test_complete_corrupt_body_reports_consumable_end():
    audio = HR0.encode(bytes(range(42)))
    body_start = hf_lead.MIN_SAMPLES + hr0.SYNC_SYMBOLS * hr0.SYMBOL_SAMPLES
    audio[body_start:] = 0
    result = HR0.decode(_capture(audio))
    assert result["payload"] is None
    assert result["end_index"] > result["sync_end_index"]


@pytest.mark.channel_regression
@pytest.mark.parametrize("preset,snr", [("mid_latitude_quiet", 6.0),
                                       ("mid_latitude_moderate", 6.0),
                                       ("mid_latitude_disturbed", 11.0)])
def test_replacement_full_frame_at_nominal_channel_smoke_points(preset, snr):
    from whale.qualification import channel_factory, run_frame_trials

    records = run_frame_trials(
        HR0, channel_factory("watterson", snr, watterson_preset=preset),
        2, 20260906, point_index=1, direction=f"{preset}, SNR/3 kHz {snr} dB",
        payload_bytes=42)
    assert all(record.decoded for record in records)
