"""HC0 against audio recorded off a real HF SSB path, both directions.

The IC-7300/IC-705 bench is 30 dB asymmetric: the IC-705's antenna port
radiates and hears about that much worse than the IC-7300's, so one leg
arrives at 37 dB of tone SNR and the other at 14 dB.  That asymmetry is a
nuisance for the station and a gift for this file, because it means the two
captures here are not two samples of one channel -- one of them is the leg
that decoded **0 of 10** HC1 frames on 2026-08-28, and HC0 carries it with
2 raw bit errors in 1,132 that the convolutional code absorbs without
noticing.

The captures came from `scripts/hw_hf_frames.py --mode hc0` that day
(see README.md, "HC0: the rung that gets through"), trimmed to the frame
plus a quarter second either side.

Nothing here is a golden digest.  The assertion is the one that matters --
the bytes that went in came out -- plus the measurements that say why, so
a change that starts decoding these by luck rather than by design still
shows up.

Software only: the radios were needed to make these files, not to run this.
"""

import pathlib

import numpy as np
import pytest

from whale.modes import hc0
from whale.modes.hc0_mode import HC0

CAPTURES = pathlib.Path(__file__).parent / "data" / "hc0_captures"

#: What each leg measured on the day, as the yardstick for "still working
#: for the right reason".  The weak one is the interesting number.
EXPECTED = {
    "ic7300_to_ic705": {"cfo_hz": -8.0, "tone_snr_db": 36.9, "raw_errors": 0},
    "ic705_to_ic7300": {"cfo_hz": +8.9, "tone_snr_db": 14.5, "raw_errors": 2},
}


def capture_names():
    return sorted(path.stem for path in CAPTURES.glob("*.npy"))


@pytest.fixture(scope="module")
def decoded():
    return {name: HC0.decode(np.load(CAPTURES / f"{name}.npy"))
            for name in capture_names()}


@pytest.mark.parametrize("name", capture_names())
def test_the_capture_decodes_to_the_bytes_that_were_transmitted(name, decoded):
    expected = (CAPTURES / f"{name}.bin").read_bytes()
    result = decoded[name]
    assert result["synced"] is True
    assert result["crc_ok"] is True
    assert result["payload"] == expected


def test_the_weak_leg_is_the_one_hc1_could_not_carry():
    """The whole point, as an assertion.

    On the same pair, the same day, HC1 decoded 0/10 in this direction.
    If this ever stops passing, the mode has lost the margin it was
    written for.
    """
    name = "ic705_to_ic7300"
    result = HC0.decode(np.load(CAPTURES / f"{name}.npy"))
    assert result["payload"] == (CAPTURES / f"{name}.bin").read_bytes()
    assert result["confidence"] >= 3 * HC0.confidence_threshold


@pytest.mark.parametrize("name", capture_names())
def test_the_measured_offset_is_the_one_the_bench_saw(name, decoded):
    """About 8 Hz, and opposite in sign between the two directions.

    A real physical quantity -- the difference between two crystal
    oscillators, seen from each end -- rather than a fitting artefact, and
    the thing no CPFSK profile in this repo can see at all.
    """
    assert decoded[name]["cfo_hz"] == pytest.approx(EXPECTED[name]["cfo_hz"],
                                                    abs=1.5)


@pytest.mark.parametrize("name", capture_names())
def test_the_tone_detector_had_the_margin_it_was_designed_for(name, decoded):
    """Winning tone against the mean of the fifteen that were not sent.

    Pinned per direction because the two legs are 22 dB apart and averaging
    them would hide exactly the case worth watching.
    """
    assert decoded[name]["tone_snr_db"] == pytest.approx(
        EXPECTED[name]["tone_snr_db"], abs=3.0)


@pytest.mark.parametrize("name", capture_names())
def test_how_much_work_the_error_correction_actually_had_to_do(name):
    """The margin, made visible rather than assumed.

    The strong leg arrived with no raw bit errors at all.  The weak one --
    the leg HC1 could not carry -- arrived with 2 in 1,132, which is 0.18%
    against a rate-1/2 K=7 code that only starts failing somewhere near
    8%.  So neither of these squeaked through on the Viterbi decoder: the
    tone detector had already done the work, and the coding is still
    entirely in hand for a far worse path than this bench can produce.
    """
    audio = np.load(CAPTURES / f"{name}.npy")
    expected = (CAPTURES / f"{name}.bin").read_bytes()
    errors = hc0.demodulate_debug(audio, expected)["total_bit_errors"]
    assert errors <= EXPECTED[name]["raw_errors"]
    assert errors / hc0.PAYLOAD_BITS < 0.01


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
