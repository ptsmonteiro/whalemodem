"""Invariants for the IEEE 802.11n QC-LDPC codec in `whale/dsp/ldpc.py`.

The codec is on the shipped decode path: `whale/phy/ofdm49.py` calls
`decode_batch` for every HF6, HF7 and HF8 frame.  It used to be asserted
from `experiments/qpsk29/test_qpsk29.py`, which was retired with the rest
of that experiment; these are the invariants that were worth keeping, moved
under `tests/` where the suite actually runs them.

The batch/scalar equivalence in particular is not a tidy-up: `decode_batch`
is a separate vectorized implementation of the same normalized min-sum
iteration, and nothing else would notice the two drifting apart.
"""

from __future__ import annotations

import numpy as np
import pytest

from whale.dsp import ldpc

RATES = ("1/2", "2/3", "3/4")


@pytest.mark.parametrize("rate", RATES)
def test_encode_is_systematic_and_satisfies_its_parity_checks(rate):
    rng = np.random.default_rng(1)
    information = rng.integers(0, 2, ldpc.INFORMATION_BITS[rate],
                               dtype=np.uint8)
    coded = ldpc.encode(information, rate)
    assert np.array_equal(coded[:len(information)], information)
    assert not np.any(ldpc.syndrome(coded, rate=rate))


@pytest.mark.parametrize("rate", RATES)
def test_clean_llrs_decode_back_to_the_information_bits(rate):
    rng = np.random.default_rng(2)
    information = rng.integers(0, 2, ldpc.INFORMATION_BITS[rate],
                               dtype=np.uint8)
    llr = 1.0 - 2.0 * ldpc.encode(information, rate)
    decoded, _iterations, ok = ldpc.decode(llr, rate=rate)
    assert ok
    assert np.array_equal(decoded, information)


@pytest.mark.parametrize("rate", RATES)
def test_the_code_corrects_noise_that_flips_hard_decisions(rate):
    rng = np.random.default_rng(3)
    information = rng.integers(0, 2, ldpc.INFORMATION_BITS[rate],
                               dtype=np.uint8)
    coded = ldpc.encode(information, rate)
    llr = (1.0 - 2.0 * coded) * 2.0 + rng.normal(0.0, 1.0, coded.shape)
    # The point of the exercise: hard-slicing the channel alone is wrong.
    assert np.any((llr < 0).astype(np.uint8) != coded)
    decoded, _iterations, ok = ldpc.decode(llr, rate=rate)
    assert ok
    assert np.array_equal(decoded, information)


@pytest.mark.parametrize("rate", RATES)
def test_batch_decoding_is_bit_identical_to_decoding_one_at_a_time(rate):
    rng = np.random.default_rng(7)
    information = rng.integers(0, 2, (5, ldpc.INFORMATION_BITS[rate]),
                               dtype=np.uint8)
    coded = np.vstack([ldpc.encode(row, rate) for row in information])
    llr = (1.0 - 2.0 * coded) * 2.0 + rng.normal(0.0, 1.0, coded.shape)

    batch = ldpc.decode_batch(llr, rate=rate)
    scalar = [ldpc.decode(row, rate=rate) for row in llr]

    assert np.array_equal(batch[0], np.vstack([item[0] for item in scalar]))
    assert np.array_equal(batch[1], [item[1] for item in scalar])
    assert np.array_equal(batch[2], [item[2] for item in scalar])
