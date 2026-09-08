"""HF8 OFDM codec contracts; no hardware required.

HF8 is HF7's carrier plan at 8PSK (see whale/modes/hf8_mode.py). These tests
pin the things that make it *that* mode rather than a differently-tuned HF7:
the shared carrier geometry, the constellation and coding that buy the
margin, and the deliberately short frame that carries its fading envelope.
"""

import numpy as np
import pytest

from whale import framing, rx_audio
from whale.mode_qualification import registry
from whale.modes.hf7_mode import HF7
from whale.modes.hf8_mode import HF8, HF8_PHY, BAND_LO_HZ, BAND_HI_HZ
from whale.phy import ofdm49 as ofdm49

# SPEED_LADDERS.md: HF rungs occupy the 300-2,700 Hz channel, gated on a
# 99%-power occupied bandwidth of no more than 2,500 Hz.
OCCUPIED_BANDWIDTH_CEILING_HZ = 2_500.0
HF_CHANNEL_LO_HZ = 300.0
HF_CHANNEL_HI_HZ = 2_700.0
# SPEED_LADDERS.md Level 3, "fast data". HF8 does not reach Level 4's
# 4,000 bit/s floor and does not claim to: it spends that rate on margin.
LEVEL3_MIN_NET_BPS = 2_000.0


def _occupied_bandwidth_hz(audio, fraction=0.99):
    x = np.asarray(audio, dtype=np.float64)
    freqs = np.fft.rfftfreq(len(x), 1.0 / HF8.tx_sample_rate)
    power = np.abs(np.fft.rfft(x)) ** 2
    cumulative = np.cumsum(power) / np.sum(power)
    tail = (1.0 - fraction) / 2.0
    lo = freqs[np.searchsorted(cumulative, tail)]
    hi = freqs[np.searchsorted(cumulative, 1.0 - tail)]
    return float(hi - lo)


def _full_frame_payload(mode, salt=11):
    return bytes((i * 47 + salt) & 0xFF
                 for i in range(mode.chunk_size + framing.AIR_HEADER_BYTES))


def test_hf8_clean_loopback_and_throughput():
    payload = _full_frame_payload(HF8)
    tx = HF8.encode(payload)
    captured = rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))
    result = HF8.decode(captured)
    assert result["payload"] == payload
    assert result["crc_ok"]
    # SPEED_LADDERS.md: net application bits per full DATA frame over that
    # frame's complete airtime.
    assert 8 * HF8.chunk_size / HF8.airtime(len(payload)) > LEVEL3_MIN_NET_BPS


def test_hf8_occupied_bandwidth_is_inside_the_hf_gate():
    """HF8 fills the 300-2,700 Hz channel, so this pins the splatter gate."""
    width = _occupied_bandwidth_hz(HF8.encode(_full_frame_payload(HF8, salt=7)))
    assert width < OCCUPIED_BANDWIDTH_CEILING_HZ, (
        f"HF8 occupies {width:.1f} Hz, over the "
        f"{OCCUPIED_BANDWIDTH_CEILING_HZ:.0f} Hz HF gate")


def test_hf8_shares_hf7s_carrier_plan_and_guard():
    """The design claim is 'HF7's geometry, a different constellation'. If a
    future change moves the carriers or the guard, HF8 is no longer that
    mode and its measured comparison against HF7 no longer applies."""
    spacing = ofdm49.DESIGN_RATE / HF8_PHY.fft_size
    assert spacing == 50.0
    assert HF8_PHY.n_active == 49
    assert HF8_PHY.active_bins == tuple(
        ofdm49.bins_in_band(HF8_PHY.fft_size, BAND_LO_HZ, BAND_HI_HZ))
    carriers = [b * spacing for b in HF8_PHY.active_bins]
    # Edge to edge, exactly as HF7: the band is fully used, and nothing sits
    # outside it.
    assert min(carriers) == HF_CHANNEL_LO_HZ
    assert max(carriers) == HF_CHANNEL_HI_HZ
    # 2 ms guard, HF7's. Screening found 3 ms and 4 ms bought no boundary.
    assert HF8_PHY.cp_len / ofdm49.DESIGN_RATE == 0.002


def test_hf8_trades_constellation_and_coding_for_margin_against_hf7():
    """The waveform parameters that differ from HF7, and those that must
    not: the comparison against HF7 is only meaningful while the carrier
    plan, guard and equalizer stay identical."""
    assert HF8_PHY.bits_per_symbol == 3          # 8PSK, HF7 is 32-QAM at 5
    assert HF8_PHY.fec_rate == "2/3"             # HF7 is 3/4
    assert HF8_PHY.pilot_interval == 10          # HF7 is 20
    assert HF8_PHY.interleave
    # Everything else is HF7's.
    assert HF8_PHY.fft_size == 240
    assert HF8_PHY.equalizer == "gain"
    # Comb pilots lost on every channel in screening and cost rate as well.
    assert HF8_PHY.pilot_comb_stride == 0
    assert HF8_PHY.n_comb() == 0
    # HF8 is the slower, more robust sibling: this ordering is the whole point.
    hf8_rate = 8 * HF8.chunk_size / HF8.airtime(HF8.chunk_size)
    hf7_rate = 8 * HF7.chunk_size / HF7.airtime(HF7.chunk_size)
    assert hf8_rate < hf7_rate


def test_hf8_frame_is_codeword_aligned_and_five_seconds():
    """270 B is 5 whole rate-2/3 codewords with no padding, in a 0.616 s
    frame.

    The shortness is the design, not an accident, and it is the opposite of
    HF7's choice: frame length was the largest single lever on this mode's
    fading envelope (quiet-Watterson 90% delivery at 24/20/16 dB for 2.090 s
    / 1.144 s / 0.616 s frames, and never reached at 4.862 s). A future
    change that lengthens this frame to chase throughput gives the envelope
    back, so it should fail here and be argued on new evidence.
    """
    assert HF8_PHY.n_codewords == 46
    assert HF8.airtime(HF8.chunk_size) == pytest.approx(4.972)


def test_hf8_rejects_oversize_payload():
    try:
        HF8.encode(bytes(HF8.chunk_size + framing.AIR_HEADER_BYTES + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("oversize HF8 payload was accepted")


def test_hf8_sits_between_hc1_and_hf7_on_the_default_ladder():
    """Installed DEFAULT by owner decision on 2026-09-07 (see
    MODE_QUALIFICATION.md). The ladder is ordered by rate because
    `_maybe_adapt` climbs it in order, so HF8's position is behaviour, not
    presentation: at 3,299 bit/s it belongs between HC1 and HF7."""
    names = [m.name for m in registry("hf-ssb", "default").modes]
    assert "hf8" in names
    assert names.index("hc1") < names.index("hf8") < names.index("hf7")


def test_default_hf_ladder_is_ordered_by_rate():
    """The whole ladder, not just HF8: adaptation climbs it one rung at a
    time, so an out-of-order rung would make a step down to a *faster* mode."""
    modes = registry("hf-ssb", "default").modes
    rates = [8 * m.chunk_size / m.airtime(m.chunk_size) for m in modes]
    assert rates == sorted(rates), [
        (m.name, round(r)) for m, r in zip(modes, rates)]
