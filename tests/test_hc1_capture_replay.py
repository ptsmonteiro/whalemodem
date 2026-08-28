"""HC1 against audio recorded off a real HF SSB path.

`test_hc1_mode.py` proves the mode against impairments this repo *models*.
This file proves it against one it did not have to: two Icom radios on
10.145 MHz USB, in data mode, whose reference oscillators disagree by about
8 Hz -- through the IC-7300's transmit chain, the ionosphere-free few metres
between them, the IC-705's receiver, its AGC and its USB codec.

The captures came from `scripts/hw_hc1_frames.py` on 2026-08-28 (see
README.md, "HC1 on the HF bench"), trimmed to the frame plus a quarter
second either side.  Two of them, deliberately different sizes: a
full 74-byte payload and a 12-byte one the size of a DATA_ACK, because HC1
is the control mode and the small frame is the one the link sends most.

Nothing here is a golden digest.  The assertion is the one that matters --
the bytes that went in came out -- plus the handful of measurements that
say *why* it worked, so a change that starts decoding these by luck rather
than by design still shows up.

Software only: the radios were needed to make these files, not to run this.
"""

import pathlib

import numpy as np
import pytest

from whale.modes import hc1
from whale.modes.hc1_mode import HC1

CAPTURES = pathlib.Path(__file__).parent / "data" / "hc1_captures"


def capture_names():
    return sorted(path.stem for path in CAPTURES.glob("*.npy"))


@pytest.fixture(scope="module")
def decoded():
    return {name: HC1.decode(np.load(CAPTURES / f"{name}.npy"))
            for name in capture_names()}


@pytest.mark.parametrize("name", capture_names())
def test_the_capture_decodes_to_the_bytes_that_were_transmitted(name, decoded):
    expected = (CAPTURES / f"{name}.bin").read_bytes()
    result = decoded[name]
    assert result["synced"] is True
    assert result["crc_ok"] is True
    assert result["payload"] == expected


@pytest.mark.parametrize("name", capture_names())
def test_the_measured_carrier_offset_is_the_one_the_bench_saw(name, decoded):
    """About -8 Hz, every frame, on both captures.

    This is the whole reason HC1 exists rather than an HF-tuned CPFSK
    profile, and it is a real physical quantity -- the difference between
    two crystal oscillators -- not a fitting artefact.  Pinning it means a
    change that quietly stops correcting the offset fails here even if the
    FEC is strong enough to carry these particular frames anyway.
    """
    assert decoded[name]["cfo_hz"] == pytest.approx(-8.2, abs=1.0)


@pytest.mark.parametrize("name", capture_names())
def test_the_whole_band_arrived_through_the_ssb_filter(name, decoded):
    """All 19 carriers present, which is what says the band fits.

    HC1's 656-2344 Hz was chosen to clear a 2.4 kHz data filter's skirts
    with room for an offset still to be measured.  A carrier count below 19
    here would mean it does not, on real hardware, and the choice was
    wrong.
    """
    result = decoded[name]
    assert result["present_carriers"] == hc1.N_CARRIERS
    assert np.min(result["carrier_snr_db"]) > 10.0


@pytest.mark.parametrize("name", capture_names())
def test_the_forward_error_correction_had_nothing_to_do(name, decoded):
    """Both captures decoded with zero raw bit errors.

    Recorded so the margin is visible rather than assumed: these frames did
    not squeak through on the Viterbi decoder, they arrived clean, and the
    coding is all still in hand for a worse path.  `demodulate_debug`
    against the known payload is how the number is obtained.
    """
    audio = np.load(CAPTURES / f"{name}.npy")
    expected = (CAPTURES / f"{name}.bin").read_bytes()
    assert hc1.demodulate_debug(audio, expected)["total_bit_errors"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
