"""Software conformance for VF13, the shipped combinatorial FM MFSK mode."""

import numpy as np
import pytest

from whale import framing, rx_audio, waveform
from whale.dsp import fec
from whale.modes.vf13 import VF13, Vf13Mode, mode_for


def _capture(mode, payload, *, lead=4800, tail=9600):
    tx = mode.modulate(payload)
    padded = np.concatenate([np.zeros(lead, np.float32), tx,
                             np.zeros(tail, np.float32)])
    return rx_audio.downsample(padded)


def _payload(mode, seed=7):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()


def test_default_mode_contract_and_shipped_config_a():
    assert isinstance(VF13, waveform.WaveformMode)
    assert VF13.mode_id == 19
    assert VF13.tone_count == 16
    assert VF13.subbands == 1
    assert VF13.mapping == "combinatorial"
    assert VF13.active_tones == 6
    assert VF13.symbol_samples == 320
    assert VF13.band_lo_hz == 600.0 and VF13.band_hi_hz == 3000.0
    assert VF13.fec_rate == "7/8"
    assert VF13.bits_per_symbol == 12
    assert VF13.chunk_size + framing.AIR_HEADER_BYTES == VF13.max_payload_bytes


def test_net_bit_rate_matches_the_measured_config_a_figure():
    # C(16,6)=8008 truncated to 2**12=4096 codewords; 150 Bd; punctured
    # rate-7/8 K=7 convolutional; 8.483 s fixed frame.
    assert VF13.net_bit_rate() == pytest.approx(1355.1, abs=0.5)
    assert VF13.frame_seconds() == pytest.approx(8.483, abs=0.001)
    assert VF13.spacing_hz == pytest.approx(150.0)
    assert VF13.occupied_bandwidth_hz == pytest.approx(2400.0)
    geometry = VF13.geometry()
    assert geometry["band_hi_hz"] == pytest.approx(3000.0)


def test_geometry_is_exact_at_both_sample_rates():
    assert VF13.symbol_samples % 4 == 0
    assert VF13.rx_symbol_samples == VF13.symbol_samples // 4
    bank = VF13.tx_banks[0]
    assert bank.spacing_hz == pytest.approx(VF13.spacing_hz)
    assert len(VF13.tx_banks) == 1
    assert VF13.combination_masks.shape == (2 ** VF13.bits_per_symbol, VF13.tone_count)
    assert np.all(VF13.combination_masks.sum(axis=1) == VF13.active_tones)


def test_clean_loopback_round_trips_exactly():
    payload = _payload(VF13)
    result = VF13.demodulate(_capture(VF13, payload))
    assert result["synced"]
    assert result["crc_ok"]
    assert result["payload"] == payload


def test_preamble_is_drawn_from_the_payload_codebook():
    audio = np.asarray(VF13.modulate(_payload(VF13)), np.float64)
    n = VF13.symbol_samples
    sync = audio[VF13.head_symbols * n:(VF13.head_symbols + VF13.sync_symbols) * n]
    body = audio[len(audio) - VF13.tail_samples - VF13.payload_symbols * n:
                 len(audio) - VF13.tail_samples]
    rms = lambda x: np.sqrt(np.mean(x ** 2))
    assert 20 * np.log10(rms(sync) / rms(body)) == pytest.approx(0.0, abs=0.5)
    assert np.all(VF13.sync_grid[:, 0] < len(VF13.combination_masks))
    assert VF13.sync_pattern.shape == (VF13.sync_symbols, VF13.active_tones)


def test_acquires_through_a_handheld_squelch_opening():
    # Shape seen on IC-705 -> KG-UV9D captures: head lost, a decaying click
    # ~20x the frame RMS at sync start, then a ~0.1 s mute inside the sync.
    payload = _payload(VF13, seed=3)
    audio = _capture(VF13, payload).astype(np.float64)
    n = VF13.rx_symbol_samples
    sync = 1200 + VF13.head_symbols * n
    level = np.sqrt(np.mean(audio[sync:sync + 12000] ** 2))
    audio[:sync] = 0.0
    click = 20 * level * np.sqrt(2) * np.exp(-np.arange(1200) / 150.0)
    audio[sync:sync + 1200] += click * np.sin(np.arange(1200) * 0.3)
    audio[sync + 2000:sync + 3200] = 0.0
    result = VF13.demodulate(audio.astype(np.float32))
    assert result["confidence"] >= VF13.confidence_threshold
    assert result["payload"] == payload


def test_fec_rate_is_punctured_7_8_and_the_codec_agrees():
    assert VF13.fec_rate == "7/8"
    assert VF13.codec.puncture_rate == "7/8"
    assert VF13.codec.coded_bits == VF13.codec_bits
    pattern = fec.PUNCTURE_PATTERNS["7/8"]
    assert VF13.codec_bits % int(pattern.sum()) == 0


def test_encode_decode_matches_the_link_facing_contract():
    payload = np.random.default_rng(3).integers(
        0, 256, VF13.max_payload_bytes, dtype=np.uint8).tobytes()
    audio = VF13.encode(payload)
    captured = rx_audio.downsample(np.concatenate(
        [np.zeros(4800, np.float32), audio, np.zeros(9600, np.float32)]))
    result = VF13.decode(captured)
    assert result["payload"] == payload
    assert VF13.airtime(len(payload)) == pytest.approx(VF13.frame_seconds())


def test_rejects_band_overflow():
    with pytest.raises(ValueError):
        Vf13Mode(symbol_samples=320, tone_count=16, subbands=1,
                 mapping="combinatorial", active_tones=6,
                 band_lo_hz=2900.0, band_hi_hz=3000.0, payload_symbols=8,
                 fec_rate="7/8")


def test_mode_for_reproduces_the_shipped_geometry():
    built = mode_for(tone_count=16, subbands=1, mapping="combinatorial",
                     active_tones=6, symbol_samples=320, band_lo_hz=600.0,
                     band_hi_hz=3000.0, frame_seconds=8.49, fec_rate="7/8",
                     constraint=7)
    assert built.payload_symbols == VF13.payload_symbols
    assert built.net_bit_rate() == pytest.approx(VF13.net_bit_rate())
    assert built.max_payload_bytes == VF13.max_payload_bytes


def test_vf13_sits_below_vf16_in_rate_order_on_the_default_fm_ladder():
    """VF13 is slower than VF16 in the negotiable ladder."""
    from whale.modes.vf16 import VF16
    from whale.mode_qualification import registry
    r = registry("fm", "default")
    ids = [mode.mode_id for mode in r.modes]
    assert VF13.net_bit_rate() < VF16.chunk_size * 8 / VF16.airtime(VF16.chunk_size)
    assert ids.index(VF13.mode_id) < ids.index(VF16.mode_id)
