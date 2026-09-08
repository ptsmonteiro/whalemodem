"""HC0 replay against two recorded IC-7300/IC-705 HF paths.

The captures exercise both directions, including the weaker 14 dB tone-SNR
leg. Software only: the radios were needed to make the files, not replay them.
"""

import pathlib

import numpy as np
import pytest

from whale import rx_audio
from whale.modes import hc0
from whale.modes.hc0_mode import HC0

CAPTURES = pathlib.Path(__file__).parent / "data" / "hc0_captures"

#: What each leg measured on the day, as the yardstick for "still working
#: for the right reason".  The weak one is the interesting number.
EXPECTED = {
    "ic7300_to_ic705": {"cfo_hz": -8.0, "tone_snr_db": 33.0, "raw_errors": 0},
    "ic705_to_ic7300": {"cfo_hz": +8.9, "tone_snr_db": 14.5, "raw_errors": 2},
}


def capture_names():
    return sorted(path.stem for path in CAPTURES.glob("*.npy"))


@pytest.fixture(scope="module")
def decoded():
    return {name: HC0.decode(rx_audio.downsample(np.load(CAPTURES / f"{name}.npy")))
            for name in capture_names()}


@pytest.mark.parametrize("name", capture_names())
def test_the_capture_decodes_to_the_bytes_that_were_transmitted(name, decoded):
    expected = (CAPTURES / f"{name}.bin").read_bytes()
    result = decoded[name]
    assert result["synced"] is True
    assert result["crc_ok"] is True
    assert result["payload"] == expected


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

    The strong leg arrived with no raw bit errors at all. The weak one
    arrived with 2 in 1,132, which is 0.18%
    against a rate-1/2 K=7 code that only starts failing somewhere near
    8%.  So neither of these squeaked through on the Viterbi decoder: the
    tone detector had already done the work, and the coding is still
    entirely in hand for a far worse path than this bench can produce.
    """
    audio = np.load(CAPTURES / f"{name}.npy")
    expected = (CAPTURES / f"{name}.bin").read_bytes()
    errors = hc0.demodulate_debug(rx_audio.downsample(audio), expected)["total_bit_errors"]
    assert errors <= EXPECTED[name]["raw_errors"]
    assert errors / hc0.PAYLOAD_BITS < 0.01


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
