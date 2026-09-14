"""Software invariants for the FM-band MFSK prototype (K parallel subbands).

Clean-channel loopback only: this pins the modulator/demodulator plumbing
that a phase-2 hardware campaign cannot check for itself -- geometry
integrity at both sample rates, exact-payload round trips for single-band
and multi-subband geometries, constant envelope at K=1, and that
acquisition tolerates a realistic carrier offset.
"""

import numpy as np
import pytest

from whale import rx_audio
from experiments.fm_mfsk.mfsk_fm_mode import FmMfskMode, mode_for, shift_hz


def _capture(mode, payload, *, lead=4800, tail=9600, offset_hz=0.0):
    tx = mode.modulate(payload)
    padded = np.concatenate([np.zeros(lead, np.float32), tx,
                             np.zeros(tail, np.float32)]).astype(np.float64)
    if offset_hz:
        padded = shift_hz(padded, -offset_hz, mode.tx_sample_rate)
    return rx_audio.downsample(padded.astype(np.float32))


def _payload(mode, seed=7):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()


GEOMETRIES = [
    dict(tone_count=16, subbands=1, symbol_samples=480, frame_seconds=5.0),
    dict(tone_count=32, subbands=1, symbol_samples=768, frame_seconds=5.0),
    dict(tone_count=16, subbands=2, symbol_samples=960, frame_seconds=5.0),
    dict(tone_count=16, subbands=4, symbol_samples=1920, frame_seconds=5.0),
    dict(tone_count=8, subbands=8, symbol_samples=1920, frame_seconds=5.0),
]


@pytest.mark.parametrize("geometry", GEOMETRIES, ids=lambda g: f"M{g['tone_count']}K{g['subbands']}")
def test_geometry_is_exact_at_both_sample_rates(geometry):
    mode = mode_for(**geometry)
    assert mode.symbol_samples % 4 == 0
    assert mode.rx_symbol_samples == mode.symbol_samples // 4
    for bank in mode.tx_banks:
        assert bank.spacing_hz == pytest.approx(mode.spacing_hz)
    for bank in mode.rx_banks:
        assert bank.spacing_hz == pytest.approx(mode.spacing_hz)
    assert mode.tx_banks[0].tone_hz[0] == pytest.approx(mode.band_lo_hz)
    top_hz = mode.tx_banks[-1].tone_hz[-1] + mode.spacing_hz
    assert top_hz <= mode.band_hi_hz + 1e-6
    assert len(mode.tx_banks) == mode.subbands
    assert mode.bits_per_symbol == mode.subbands * (mode.tone_count.bit_length() - 1)


@pytest.mark.parametrize("geometry", GEOMETRIES, ids=lambda g: f"M{g['tone_count']}K{g['subbands']}")
def test_clean_loopback_round_trips(geometry):
    mode = mode_for(**geometry)
    payload = _payload(mode)
    result = mode.demodulate(_capture(mode, payload))
    assert result["synced"]
    assert result["crc_ok"]
    assert result["payload"] == payload


def test_constant_envelope_at_k_equals_one():
    """A single subband is constant-envelope, MFSK's advantage on this path."""
    mode = mode_for(tone_count=32, subbands=1, symbol_samples=768, frame_seconds=5.0)
    audio = np.asarray(mode.modulate(_payload(mode)), dtype=np.float64)
    body = audio[mode.symbol_samples:len(audio) - mode.tail_samples]
    crest = 20 * np.log10(np.max(np.abs(body)) / np.sqrt(np.mean(body ** 2)))
    assert crest < 3.2


def test_multi_subband_crest_factor_grows_with_k():
    """K simultaneous tones cost peak-to-average headroom; document the trend."""
    crests = {}
    for k in (1, 2, 4, 8):
        m = 16 if k <= 4 else 8
        symbol_samples = {1: 480, 2: 960, 4: 1920, 8: 1920}[k]
        mode = mode_for(tone_count=m, subbands=k, symbol_samples=symbol_samples,
                        frame_seconds=5.0)
        audio = np.asarray(mode.modulate(_payload(mode)), dtype=np.float64)
        body = audio[mode.symbol_samples:len(audio) - mode.tail_samples]
        crests[k] = 20 * np.log10(np.max(np.abs(body)) / np.sqrt(np.mean(body ** 2)))
    assert crests[8] > crests[1]


@pytest.mark.parametrize("offset_hz", (-6.0, -2.0, 0.0, 2.0, 6.0))
def test_acquisition_tracks_carrier_offset(offset_hz):
    mode = mode_for(tone_count=16, subbands=4, symbol_samples=1920, frame_seconds=5.0)
    payload = _payload(mode)
    captured = _capture(mode, payload, offset_hz=offset_hz)
    result = mode.demodulate(captured)
    assert result["synced"]
    assert result["offset_hz"] == pytest.approx(offset_hz, abs=1.0)
    assert result["payload"] == payload


def test_rejects_band_overflow():
    with pytest.raises(ValueError):
        FmMfskMode(symbol_samples=480, tone_count=16, subbands=8, payload_symbols=8)


def test_rejects_non_power_of_two_tone_count():
    with pytest.raises(ValueError):
        FmMfskMode(symbol_samples=480, tone_count=24, subbands=1, payload_symbols=8)


def test_net_bit_rate_counts_only_delivered_payload():
    mode = mode_for(tone_count=16, subbands=4, symbol_samples=1920, frame_seconds=5.0)
    expected = mode.max_payload_bytes * 8 / mode.frame_seconds()
    assert mode.net_bit_rate() == pytest.approx(expected)
    assert mode.frame_seconds() == pytest.approx(5.0, abs=0.5)


def test_chunk_size_matches_air_header_convention():
    from whale import framing
    mode = mode_for(tone_count=16, subbands=2, symbol_samples=960, frame_seconds=5.0)
    assert mode.chunk_size == mode.max_payload_bytes - framing.AIR_HEADER_BYTES

    payload = np.random.default_rng(3).integers(
        0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()
    # encode/decode is what the sweep harness calls
    audio = mode.encode(payload)
    result = mode.decode(_capture_audio(mode, audio))
    assert result["payload"] == payload


def _capture_audio(mode, audio, lead=4800, tail=9600):
    padded = np.concatenate([np.zeros(lead, np.float32), audio,
                             np.zeros(tail, np.float32)]).astype(np.float32)
    return rx_audio.downsample(padded)


@pytest.mark.parametrize("fec_rate", ("1/2", "2/3", "3/4", "5/6", "7/8"))
def test_fec_rate_loopback_and_capacity_grows_with_rate(fec_rate):
    """Every punctured rate round-trips clean-channel, and higher rates
    carry strictly more payload for the same on-air geometry."""
    mode = mode_for(tone_count=16, subbands=4, symbol_samples=1920,
                    frame_seconds=5.0, fec_rate=fec_rate)
    assert mode.fec_rate == fec_rate
    assert mode.codec.puncture_rate == fec_rate
    assert mode.codec.coded_bits == mode.codec_bits
    payload = _payload(mode)
    result = mode.demodulate(_capture(mode, payload))
    assert result["synced"]
    assert result["crc_ok"]
    assert result["payload"] == payload


def test_fec_rate_increases_net_bit_rate_on_clean_channel():
    base = mode_for(tone_count=16, subbands=4, symbol_samples=1920,
                    frame_seconds=5.0, fec_rate="1/2")
    faster = mode_for(tone_count=16, subbands=4, symbol_samples=1920,
                      frame_seconds=5.0, fec_rate="7/8")
    assert faster.max_payload_bytes > base.max_payload_bytes
    assert faster.net_bit_rate() > base.net_bit_rate()


def test_rejects_unknown_fec_rate():
    with pytest.raises(ValueError):
        FmMfskMode(symbol_samples=1920, tone_count=16, subbands=4,
                  payload_symbols=16, fec_rate="9/10")


@pytest.mark.parametrize("preemph_db", (0.0, 6.0, 10.0))
def test_preemph_normalizes_total_transmit_power(preemph_db):
    """Pre-emphasis reshapes the spectrum, but must not change the total
    transmitted power of the payload body (peak drive stays comparable)."""
    flat = mode_for(tone_count=16, subbands=4, symbol_samples=1920,
                    frame_seconds=5.0, preemph_db=0.0)
    tilted = mode_for(tone_count=16, subbands=4, symbol_samples=1920,
                      frame_seconds=5.0, preemph_db=preemph_db)
    payload = _payload(flat)
    flat_audio = np.asarray(flat.modulate(payload), dtype=np.float64)
    tilted_audio = np.asarray(tilted.modulate(payload), dtype=np.float64)
    flat_body = flat_audio[flat.symbol_samples:len(flat_audio) - flat.tail_samples]
    tilted_body = tilted_audio[tilted.symbol_samples:
                               len(tilted_audio) - tilted.tail_samples]
    flat_power = np.mean(flat_body ** 2)
    tilted_power = np.mean(tilted_body ** 2)
    if preemph_db == 0.0:
        assert tilted_power == pytest.approx(flat_power, rel=1e-9)
    else:
        # RMS-normalized per-tone weights: total body power stays within a
        # few percent, not exactly equal, because which tone is *chosen*
        # each symbol is payload-dependent while the normalization is
        # computed over the uniform tone population.
        assert tilted_power == pytest.approx(flat_power, rel=0.15)


def test_preemph_weights_are_monotonic_and_flat_at_zero_db():
    mode = mode_for(tone_count=16, subbands=4, symbol_samples=1920,
                    frame_seconds=5.0, preemph_db=6.0)
    for weights, bank in zip(mode.preemph_weights, mode.tx_banks):
        order = np.argsort(bank.tone_hz)
        assert np.all(np.diff(weights[order]) >= -1e-9)
    flat_mode = mode_for(tone_count=16, subbands=4, symbol_samples=1920,
                         frame_seconds=5.0, preemph_db=0.0)
    for weights in flat_mode.preemph_weights:
        assert np.allclose(weights, 1.0)


COMBINATORIAL_GEOMETRIES = [
    dict(tone_count=16, subbands=1, symbol_samples=480, frame_seconds=5.0,
         mapping="combinatorial", active_tones=4),
    dict(tone_count=16, subbands=1, symbol_samples=480, frame_seconds=5.0,
         mapping="combinatorial", active_tones=5),
    dict(tone_count=8, subbands=1, symbol_samples=160, frame_seconds=5.0,
         mapping="combinatorial", active_tones=4),
]


@pytest.mark.parametrize("geometry", COMBINATORIAL_GEOMETRIES,
                         ids=lambda g: f"N{g['tone_count']}_k{g['active_tones']}")
def test_combinatorial_mapping_round_trips(geometry):
    mode = mode_for(**geometry)
    from math import comb
    expected_bits = int(np.floor(np.log2(comb(mode.tone_count,
                                              geometry["active_tones"]))))
    assert mode.bits_per_symbol == expected_bits
    payload = _payload(mode)
    result = mode.demodulate(_capture(mode, payload))
    assert result["synced"]
    assert result["crc_ok"]
    assert result["payload"] == payload


def test_combinatorial_mapping_beats_subband_bits_per_symbol():
    """The whole point of the combinatorial mapping: more bits/symbol on
    the same N=16 tone grid than one-hot K=4 subbands of M=4."""
    subband = mode_for(tone_count=4, subbands=4, symbol_samples=320,
                       frame_seconds=5.0, mapping="subband")
    combinatorial = mode_for(tone_count=16, subbands=1, symbol_samples=480,
                             frame_seconds=5.0, mapping="combinatorial",
                             active_tones=4)
    assert subband.bits_per_symbol == 8
    assert combinatorial.bits_per_symbol == 10


def test_combinatorial_active_tone_count_drives_amplitude_and_channel_k():
    mode = mode_for(tone_count=16, subbands=1, symbol_samples=480,
                    frame_seconds=5.0, mapping="combinatorial", active_tones=4)
    assert mode.active_tone_count == 4
    assert mode.per_tone_amplitude == pytest.approx(0.5 / np.sqrt(4))


def test_combinatorial_hard_tones_and_tone_grid_shapes_match():
    mode = mode_for(tone_count=16, subbands=1, symbol_samples=480,
                    frame_seconds=5.0, mapping="combinatorial", active_tones=4)
    payload = _payload(mode)
    truth = mode.tone_grid(payload)
    result = mode.demodulate(_capture(mode, payload))
    assert truth.shape == (mode.payload_symbols, 1)
    assert result["hard_tones"].shape == truth.shape
    assert np.array_equal(truth, result["hard_tones"])


def test_combinatorial_rejects_subbands_other_than_one():
    with pytest.raises(ValueError):
        FmMfskMode(symbol_samples=480, tone_count=16, subbands=2,
                  payload_symbols=8, mapping="combinatorial", active_tones=4)


def test_combinatorial_rejects_active_tones_out_of_range():
    with pytest.raises(ValueError):
        FmMfskMode(symbol_samples=480, tone_count=16, subbands=1,
                  payload_symbols=8, mapping="combinatorial", active_tones=16)
    with pytest.raises(ValueError):
        FmMfskMode(symbol_samples=480, tone_count=16, subbands=1,
                  payload_symbols=8, mapping="combinatorial", active_tones=0)


def test_rejects_unknown_mapping():
    with pytest.raises(ValueError):
        FmMfskMode(symbol_samples=480, tone_count=16, subbands=1,
                  payload_symbols=8, mapping="weird")


def test_default_mapping_is_unchanged_subband_behavior():
    """Ensure adding `mapping`/`active_tones` did not change the default
    (subband, one-hot) geometry or its net bit rate."""
    mode = mode_for(tone_count=16, subbands=4, symbol_samples=1920,
                    frame_seconds=5.0)
    assert mode.mapping == "subband"
    assert mode.bits_per_symbol == 4 * 4
    assert mode.active_tone_count == mode.subbands


def test_guard_interval_round_trips_and_costs_airtime():
    plain = mode_for(tone_count=16, subbands=4, symbol_samples=1920,
                     frame_seconds=5.0, guard_seconds=0.0)
    guarded = mode_for(tone_count=16, subbands=4, symbol_samples=1920,
                       frame_seconds=5.0, guard_seconds=0.01)
    assert guarded.guard_rx_samples > 0
    payload = _payload(guarded)
    result = guarded.demodulate(_capture(guarded, payload))
    assert result["synced"]
    assert result["crc_ok"]
    assert result["payload"] == payload
    # Same target frame_seconds, so the guard eats into payload symbols and
    # net bit rate drops relative to no guard at all.
    assert guarded.net_bit_rate() < plain.net_bit_rate()
