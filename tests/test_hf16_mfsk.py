"""Software invariants for the HF16 MFSK waveform. No hardware.

These pin the parts a hardware campaign cannot check for itself: that the
geometry lands on exact integers at both sample rates, that a frame
round-trips byte for byte through the production 48 kHz -> 12 kHz receive
decimation, that the repetition layer is actually decodable rather than
merely plausible, and that acquisition tolerates the carrier offset the path
was measured to have.
"""

import numpy as np
import pytest

from whale import rx_audio
from experiments.hf16_mfsk_lowsnr import mfsk_mode as mm
from experiments.hf16_mfsk_lowsnr.mfsk_mode import MfskMode, mode_for

TONE_COUNTS = (16, 32, 64, 128, 256)


def _capture(mode, payload, *, lead=4800, tail=9600, offset_hz=0.0):
    """One clean loopback: modulate, pad, decimate as the receiver does."""
    tx = mode.modulate(payload)
    padded = np.concatenate([np.zeros(lead, np.float32), tx,
                             np.zeros(tail, np.float32)]).astype(np.float64)
    if offset_hz:
        padded = mm.shift_hz(padded, -offset_hz, mm.TX_SAMPLE_RATE)
    return rx_audio.downsample(padded.astype(np.float32))


def _payload(mode, seed=7):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()


@pytest.mark.parametrize("tone_count", TONE_COUNTS)
def test_geometry_is_exact_at_both_sample_rates(tone_count):
    mode = mode_for(tone_count, frame_seconds=6.0)
    assert mode.tx_symbol_samples == 20 * tone_count
    assert mode.rx_symbol_samples == 5 * tone_count
    assert mode.tx_symbol_samples % mm.DECIMATION == 0
    # spacing, symbol rate and FFT bin width are one number for orthogonal
    # non-coherent MFSK; the banks must agree on it at both rates
    assert mode.tx_bank.spacing_hz == pytest.approx(mode.spacing_hz)
    assert mode.rx_bank.spacing_hz == pytest.approx(mode.spacing_hz)
    assert mode.tone_hz[0] == pytest.approx(mm.BAND_LO_HZ)
    assert mode.tone_hz[-1] < mm.BAND_HI_HZ
    assert len(mode.tone_hz) == tone_count


@pytest.mark.parametrize("tone_count", TONE_COUNTS)
def test_clean_loopback_round_trips(tone_count):
    mode = mode_for(tone_count, frame_seconds=6.0)
    payload = _payload(mode)
    result = mode.demodulate(_capture(mode, payload))
    assert result["synced"]
    assert result["crc_ok"]
    assert result["payload"] == payload


@pytest.mark.parametrize("repeat", (1, 2, 4))
def test_repetition_round_trips_and_costs_exactly_its_rate(repeat):
    mode = mode_for(64, frame_seconds=8.0, repeat=repeat)
    assert mode.repeat == repeat
    assert mode.codec_bits * repeat == mode.coded_bits
    payload = _payload(mode)
    assert mode.demodulate(_capture(mode, payload))["payload"] == payload


def test_repetition_copies_are_spread_across_the_whole_frame():
    """The point of the outer interleaver is time diversity, not averaging.

    Two copies of the same coded bit must land far apart, or repetition buys
    nothing against a fade that lasts seconds.
    """
    mode = mode_for(64, frame_seconds=12.0, repeat=2)
    positions = mode.outer_interleaver.spread(
        np.arange(mode.coded_bits) % mode.codec_bits)
    first = {}
    gaps = []
    for index, bit in enumerate(positions):
        if bit in first:
            gaps.append(index - first[bit])
        else:
            first[bit] = index
    assert len(gaps) == mode.codec_bits
    # median separation should be a large fraction of the frame, not a handful
    # of bits
    assert np.median(gaps) > mode.coded_bits / 4


def test_constant_envelope():
    """MFSK's whole physical advantage here: peak equals RMS times sqrt(2).

    A crest factor above about 3.1 dB would mean the modulator is summing
    tones somewhere and the mode has lost the reason it beat OFDM on this
    path.
    """
    mode = mode_for(64, frame_seconds=6.0)
    audio = np.asarray(mode.modulate(_payload(mode)), dtype=np.float64)
    body = audio[mode.tx_symbol_samples:len(audio) - mode.tail_samples]
    crest = 20 * np.log10(np.max(np.abs(body)) / np.sqrt(np.mean(body ** 2)))
    assert crest < 3.2


@pytest.mark.parametrize("offset_hz", (-9.0, -4.0, 0.0, 4.0, 8.0, 12.0))
def test_acquisition_tracks_the_measured_carrier_offset(offset_hz):
    """The path sits near +8 Hz and drifts a couple of Hz between keyings.

    At M=128 that is most of a tone spacing, which is why acquisition
    searches offset hypotheses instead of assuming one.
    """
    mode = mode_for(128, frame_seconds=8.0)
    payload = _payload(mode)
    captured = _capture(mode, payload, offset_hz=offset_hz)
    result = mode.demodulate(captured)
    assert result["synced"]
    assert result["offset_hz"] == pytest.approx(offset_hz, abs=1.0)
    assert result["payload"] == payload


def test_rejects_geometries_the_codec_cannot_frame():
    with pytest.raises(ValueError):
        MfskMode(tone_count=24, payload_symbols=100)      # not a power of two
    with pytest.raises(ValueError):
        # 101 symbols x 6 bits = 606 coded bits, which divides by repeat=2 to
        # 303 codec bits -- odd, and a rate-1/2 codec cannot frame an odd
        # coded-bit count
        MfskMode(tone_count=64, payload_symbols=101, repeat=2)


def test_net_bit_rate_counts_only_delivered_payload():
    mode = mode_for(64, frame_seconds=12.0)
    expected = mode.max_payload_bytes * 8 / mode.frame_seconds()
    assert mode.net_bit_rate() == pytest.approx(expected)
    # and the frame really is about as long as asked for
    assert mode.frame_seconds() == pytest.approx(12.0, abs=0.2)
