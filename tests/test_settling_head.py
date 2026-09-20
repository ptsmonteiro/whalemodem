"""Every HF waveform prepends the same settling head to every keying.

The settling head is not the sync preamble. It carries nothing, the
receiver never correlates against it, and it exists only so the
transmitter's PTT ramp and the receiver's audio AGC have in-band signal to
settle on before the sync preamble acquisition does need. Its length is
whale.framing.SETTLING_HEAD_SECONDS for every mode, rounded up to whatever
each waveform's own grid requires.

Software only -- no radios, no sound cards.
"""

import numpy as np
import pytest

from whale import framing
from whale.phy import hc0, hc1w, hr0
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
