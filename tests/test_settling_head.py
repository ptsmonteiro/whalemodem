"""Every HF waveform prepends the same settling head to every keying.

The settling head is not the sync preamble. It carries nothing, the
receiver never correlates against it, and it exists only so the
transmitter's PTT ramp and the receiver's audio AGC have in-band signal to
settle on before the sync preamble acquisition does need. Its length is
whale.framing.SETTLING_HEAD_SECONDS for every mode, rounded up to whatever
each waveform's own grid requires.

Software only -- no radios, no sound cards.
"""

from dataclasses import replace

import numpy as np
import pytest

from whale import framing, rx_audio, waveform
from whale.phy import hc0, hc1w, hr0
from whale.modes.hf2_mode import HF2
from whale.modes.hf5_mode import HF5
from whale.modes.vf12 import VF12
from whale.modes.hc0_mode import HC0
from whale.modes.hc1w_mode import HC1W
from whale.modes.hf6_mode import HF6, HF6_PHY
from whale.modes.hf7_mode import HF7, HF7_PHY
from whale.modes.hf8_mode import HF8, HF8_PHY
from whale.modes.hf9_mode import HF9, HF9_PHY
from whale.modes.hr0_mode import HR0

#: Each waveform rounds the shared budget up to its own grid, so none is
#: shorter than the budget and none overshoots by a whole extra symbol.
TOLERANCE = 0.05

OFDM_MODES = ((HF6, HF6_PHY), (HF7, HF7_PHY), (HF8, HF8_PHY), (HF9, HF9_PHY))


def head_seconds(mode):
    if mode is HR0:
        return hr0.settling_head_samples() / hr0.SAMPLE_RATE
    if mode is HC0:
        return hc0.SETTLING_HEAD_SAMPLES / hc0.SAMPLE_RATE
    if mode is HC1W:
        return hc1w.SETTLING_HEAD_SAMPLES / hc1w.SAMPLE_RATE
    return dict(OFDM_MODES)[mode].settling_head_seconds()


ALL_MODES = (HR0, HC0, HC1W, HF6, HF7, HF8, HF9)


def test_the_shared_budget_is_six_hundred_milliseconds():
    assert framing.SETTLING_HEAD_SECONDS == 0.6


@pytest.mark.parametrize("mode", ALL_MODES, ids=lambda m: m.name)
def test_every_hf_mode_has_a_settling_head_of_the_shared_length(mode):
    seconds = head_seconds(mode)
    assert framing.SETTLING_HEAD_SECONDS <= seconds
    assert seconds <= framing.SETTLING_HEAD_SECONDS + TOLERANCE


@pytest.mark.parametrize("mode", ALL_MODES, ids=lambda m: m.name)
def test_airtime_counts_the_settling_head(mode):
    payload = bytes(mode.chunk_size + framing.AIR_HEADER_BYTES)
    audio = mode.encode(payload)
    assert len(audio) == pytest.approx(
        mode.airtime(len(payload)) * mode.tx_sample_rate, abs=2)
    assert len(audio) >= head_seconds(mode) * mode.tx_sample_rate


@pytest.mark.parametrize("mode,phy", OFDM_MODES, ids=lambda m: getattr(m, "name", ""))
def test_ofdm_settling_head_is_not_counted_in_the_decoded_frame(mode, phy):
    """total_ofdm_symbols() sizes the streaming decode window, which starts
    at the sync preamble -- so the head must stay out of it."""
    assert phy.keying_seconds() == pytest.approx(
        phy.settling_head_seconds() + phy.frame_seconds())
    assert phy.total_ofdm_symbols() * phy.symbol_len == pytest.approx(
        phy.frame_seconds() * 12_000)


@pytest.mark.parametrize("mode,phy", OFDM_MODES, ids=lambda m: getattr(m, "name", ""))
def test_ofdm_acquisition_lands_past_the_settling_head(mode, phy):
    """The head must never outrank the sync preamble it precedes.

    The head is built from the mode's own modulation, one independent
    PN-BPSK draw per symbol, so the preamble template correlates against it
    only at the noise floor instead of adding coherently the way a head of
    repeated preamble symbols would.
    """
    payload = bytes((i * 37 + 11) & 0xFF
                    for i in range(mode.chunk_size + framing.AIR_HEADER_BYTES))
    design_rate = phy.modulate(payload)[::4].astype(np.float64)
    head = phy.n_settling_head_symbols * phy.symbol_len
    preamble = phy.n_preamble_symbols * phy.symbol_len

    confidence, start, _ = phy.acquire(design_rate)
    assert start == head
    inside_head = phy.acquire(design_rate,
                              search_slice=slice(0, head - preamble))[0]
    # The best score anywhere inside the head sits at the noise floor --
    # measured 0.10-0.13, against ~1.0 for the sync preamble. It is close
    # enough to the streaming receiver's 0.12 raise threshold that a head
    # may occasionally cost one failed decode attempt per keying; what must
    # never happen is the head outranking the preamble, because acquisition
    # would then align a whole head out.
    assert inside_head < 0.25 * confidence


# -- the head as a swept parameter ----------------------------------------
#
# The measurement campaign varies the head and reads what it costs. That
# only means anything if the receivers are indifferent to it: every one of
# them searches for the sync preamble, so a head of any length -- including
# none -- must still decode and must still acquire past the head rather
# than inside it.

SWEEP_SECONDS = (0.0, 0.15, 1.2)


def head_samples(mode, seconds):
    """Transmit-rate samples of head `mode` emits when asked for `seconds`."""
    if mode is HR0:
        return hr0.settling_head_samples(seconds)
    if mode is HC0:
        return hc0.settling_head_samples(seconds)
    if mode is HC1W:
        return hc1w.settling_head_samples(seconds)
    phy = dict(OFDM_MODES)[mode]
    return (replace(phy, head_seconds=seconds).n_settling_head_symbols
            * phy.symbol_len * 4)


def rx_symbol_samples(mode):
    """One receive-rate symbol -- the slack acquisition is allowed."""
    if mode is HR0:
        return hr0.RX_SYMBOL_SAMPLES
    if mode is HC0:
        return hc0.RX_SYMBOL_SAMPLES
    if mode is HC1W:
        return hc1w.SYMBOL_SAMPLES // rx_audio.DECIMATION
    return dict(OFDM_MODES)[mode].symbol_len


def test_a_mode_without_a_settling_head_is_refused():
    for mode in (HF2, HF5, VF12):
        with pytest.raises(ValueError):
            waveform.with_settling_head(mode, 0.3)


@pytest.mark.parametrize("seconds", SWEEP_SECONDS)
@pytest.mark.parametrize("mode", ALL_MODES, ids=lambda m: m.name)
def test_a_resized_head_still_round_trips(mode, seconds):
    variant = waveform.with_settling_head(mode, seconds)
    payload = bytes((i * 37 + 11) & 0xFF
                    for i in range(mode.chunk_size + framing.AIR_HEADER_BYTES))
    tx = np.asarray(variant.encode(payload))

    head = head_samples(mode, seconds)
    shipped = np.asarray(mode.encode(payload))
    assert len(tx) == len(shipped) - head_samples(mode, head_seconds(mode)) + head
    assert len(tx) == pytest.approx(
        variant.airtime(len(payload)) * mode.tx_sample_rate, abs=2)

    captured = rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))
    result = variant.decode(captured)
    assert result["payload"] == payload
    # Acquisition landed on the preamble, not somewhere inside the head:
    # within a symbol of where the head ends, whatever its length.
    # "start_index" on the FSK/SC receivers, "start_sample" on the OFDM one.
    acquired = result.get("start_index", result.get("start_sample"))
    assert acquired == pytest.approx(
        head / rx_audio.DECIMATION, abs=rx_symbol_samples(mode))
