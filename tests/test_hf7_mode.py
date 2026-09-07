"""HF7 OFDM codec contracts; no hardware required."""

import numpy as np

from whale import framing, rx_audio
from whale.mode_qualification import registry
from whale.modes.hf7_mode import HF7, HF7_PHY, BAND_LO_HZ, BAND_HI_HZ
from experiments.hf10_ofdm49_v6 import ofdm49_v6 as ofdm49

# SPEED_LADDERS.md: every HF rung is capped at 2,300 Hz occupied bandwidth,
# and MODE_QUALIFICATION.md measures it as the 99%-power interval.
OCCUPIED_BANDWIDTH_CEILING_HZ = 2_300.0
LEVEL4_MIN_NET_BPS = 4_000.0


def _occupied_bandwidth_hz(audio, fraction=0.99):
    x = np.asarray(audio, dtype=np.float64)
    freqs = np.fft.rfftfreq(len(x), 1.0 / HF7.tx_sample_rate)
    power = np.abs(np.fft.rfft(x)) ** 2
    cumulative = np.cumsum(power) / np.sum(power)
    tail = (1.0 - fraction) / 2.0
    lo = freqs[np.searchsorted(cumulative, tail)]
    hi = freqs[np.searchsorted(cumulative, 1.0 - tail)]
    return float(hi - lo)


def test_hf7_clean_loopback_and_throughput():
    payload = bytes((i * 47 + 11) & 0xFF
                    for i in range(HF7.chunk_size + framing.AIR_HEADER_BYTES))
    tx = HF7.encode(payload)
    captured = rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))
    result = HF7.decode(captured)
    assert result["payload"] == payload
    assert result["crc_ok"]
    # SPEED_LADDERS.md Level 4: net application bits per full DATA frame over
    # that frame's complete airtime.
    assert 8 * HF7.chunk_size / HF7.airtime(len(payload)) > LEVEL4_MIN_NET_BPS


def test_hf7_occupied_bandwidth_is_inside_the_hf_ceiling():
    """The gate HF2 failed and hf18's own 49-carrier version failed.

    The 49-carrier 300-2700 Hz arrangement measures ~2,444 Hz. HF7 drops the
    four lowest (and, per the experiment's per-bin SNR census, weakest)
    subcarriers to land inside the ceiling, so this is the assertion that
    pins that trim in place.
    """
    payload = bytes((i * 31 + 7) & 0xFF
                    for i in range(HF7.chunk_size + framing.AIR_HEADER_BYTES))
    width = _occupied_bandwidth_hz(HF7.encode(payload))
    assert width < OCCUPIED_BANDWIDTH_CEILING_HZ, (
        f"HF7 occupies {width:.1f} Hz, over the "
        f"{OCCUPIED_BANDWIDTH_CEILING_HZ:.0f} Hz HF ceiling")

    untrimmed = ofdm49.OFDM49Mode(
        fft_size=HF7_PHY.fft_size, cp_len=HF7_PHY.cp_len,
        active_bins=tuple(ofdm49.bins_in_band(HF7_PHY.fft_size)),
        bits_per_symbol=HF7_PHY.bits_per_symbol,
        packet_bytes=HF7_PHY.packet_bytes, pilot_interval=HF7_PHY.pilot_interval,
        fec_rate=HF7_PHY.fec_rate, interleave=HF7_PHY.interleave,
        drive_scale=HF7_PHY.drive_scale)
    assert _occupied_bandwidth_hz(untrimmed.modulate(
        bytes(untrimmed.max_payload_bytes))) > OCCUPIED_BANDWIDTH_CEILING_HZ


def test_hf7_carrier_layout_matches_the_measured_configuration():
    assert HF7_PHY.n_active == 45
    assert HF7_PHY.active_bins == tuple(
        ofdm49.bins_in_band(HF7_PHY.fft_size, BAND_LO_HZ, BAND_HI_HZ))
    spacing = ofdm49.DESIGN_RATE / HF7_PHY.fft_size
    assert spacing == 50.0
    assert HF7_PHY.active_bins[0] * spacing == 500.0
    assert HF7_PHY.active_bins[-1] * spacing == 2700.0
    # 2 ms guard: zero costs 6.7 dB of EVM on hardware, 5 ms costs 14% of rate.
    assert HF7_PHY.cp_len / ofdm49.DESIGN_RATE == 0.002
    # The payload packs whole rate-3/4 LDPC codewords, wasting no coded bits.
    assert HF7_PHY.n_codewords == 72
    assert HF7_PHY.packet_bytes * 8 == 72 * 486


def test_hf7_rejects_oversize_payload():
    try:
        HF7.encode(bytes(HF7.chunk_size + framing.AIR_HEADER_BYTES + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("oversize HF7 payload was accepted")


def test_hf7_is_the_top_rung_of_the_default_hf_ladder():
    modes = registry("hf-ssb", "default").modes
    assert HF7.mode_id in {mode.mode_id for mode in modes}
    rates = [8 * mode.chunk_size / mode.airtime(mode.chunk_size) for mode in modes]
    hf7_rate = 8 * HF7.chunk_size / HF7.airtime(HF7.chunk_size)
    assert hf7_rate == max(rates)
