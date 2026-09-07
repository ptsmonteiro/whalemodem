"""Focused checks for the experiment-local HR1-B redesign candidate."""

import numpy as np

from experiments.hr1 import benchmark, hr1b
from whale import rx_audio, waveform
from whale.channel import AwgnChannel, SnrKind, SnrSpec, WattersonChannel


def _capture(audio):
    return rx_audio.downsample(np.concatenate((
        np.asarray(audio, dtype=np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))


def _aligned(audio, class_id):
    return hr1b.decode_aligned(
        _capture(audio),
        preamble_start=(hr1b.LEAD_RX_SAMPLES
                        + rx_audio.FILTER_DELAY_DECODE_SAMPLES),
        class_id=class_id)


def test_mode_classes_round_trip_and_preserve_the_session_rate_budget():
    assert isinstance(hr1b.HR1B, waveform.WaveformMode)
    for length in (0, 12, 13, 64):
        payload = bytes((29 * at + length) & 255 for at in range(length))
        class_id = hr1b.TINY_CLASS if length <= 12 else hr1b.FULL_CLASS
        result = _aligned(hr1b.HR1B.encode(payload), class_id)
        assert result["payload"] == payload
        assert result["crc_ok"] and result["fec_tail_ok"]
    assert hr1b.FULL_FRAME_SECONDS == 19.54
    assert hr1b.TINY_FRAME_SECONDS == 3.652
    assert hr1b.CLEAN_SESSION_RATE > 18


def test_gf16_inner_code_and_shortened_rs_correction_bounds():
    information = np.r_[np.arange(16, dtype=np.uint8), 0, 0]
    tones = hr1b.FULL_CODE.encode(information)
    costs = np.ones((len(tones), 16))
    costs[np.arange(len(tones)), tones] = 0
    decoded, work = hr1b.FULL_CODE.decode(costs)
    assert np.array_equal(decoded, information)
    assert work["gf16_viterbi_branches"] == len(information) * 256 * 16

    packet = bytes(range(hr1b.FULL_PACKET_BYTES))
    protected = bytearray(hr1b._rs_encode(packet))
    for at in range(13):
        protected[(7 * at) % len(protected)] ^= at + 1
    repaired, corrected = hr1b._rs_decode(bytes(protected))
    assert repaired == packet
    assert corrected == 13


def test_complete_keying_bandwidth_and_peak_are_inside_the_screen_contract():
    audio = hr1b.HR1B.encode(bytes(range(64)))
    occupied = benchmark._occupied_bandwidth_99(audio, 48_000)
    assert occupied["width_hz"] < 2_300
    assert np.max(np.abs(audio)) <= hr1b.TX_AMPLITUDE + 1e-6


def test_held_out_minus24_awgn_oracle_seed_decodes_checked_payload():
    payload = np.random.default_rng(123456).bytes(64)
    transmitted = hr1b.HR1B.encode(payload)
    spec = SnrSpec(-24.0, SnrKind.WAVEFORM, reference_start=0,
                   reference_stop=len(transmitted))
    noisy = AwgnChannel(48_000, spec, 99001).process(transmitted).audio
    result = _aligned(noisy, hr1b.FULL_CLASS)
    assert result["payload"] == payload
    assert result["rs_corrected_bytes"] <= 13


def test_real_receiver_wires_the_seven_ms_thirty_hz_preset_at_high_snr():
    payload = bytes(range(64))
    transmitted = hr1b.HR1B.encode(payload)
    faded = WattersonChannel.from_preset(
        48_000, "high_latitude_disturbed", 810).process(transmitted).audio
    spec = SnrSpec(20.0, SnrKind.WAVEFORM, reference_start=0,
                   reference_stop=len(transmitted))
    noisy = AwgnChannel(48_000, spec, 910).process(faded).audio
    result = hr1b.HR1B.decode(_capture(noisy))
    assert result["payload"] == payload
    assert result["candidate_rank"] <= hr1b.MAX_CANDIDATES
    assert result["search_cells_evaluated"] == 54_054


def test_invalid_inputs_and_wrong_class_remain_integrity_checked_and_bounded():
    for audio in (np.zeros(0), np.zeros((2, 2)), np.asarray([np.nan])):
        result = hr1b.HR1B.decode(audio)
        assert result["payload"] is None
        assert result["candidate_count"] <= hr1b.MAX_CANDIDATES

    tiny = hr1b.HR1B.encode(bytes(range(12)))
    wrong = _aligned(tiny, hr1b.FULL_CLASS)
    assert wrong["payload"] is None

