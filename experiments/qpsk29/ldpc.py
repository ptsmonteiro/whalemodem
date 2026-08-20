"""Small, dependency-free IEEE 802.11n QC-LDPC codec.

The matrix is the fixed 648-bit, rate-1/2 matrix from IEEE 802.11-2020,
Table F-1 (expansion factor 27).  Positive LLR means bit zero.  The module
knows nothing about OFDM, framing, whitening, or constellations.
"""

from functools import lru_cache

import numpy as np


N = 648
K = 324  # backwards-compatible default (rate 1/2)
RATE = "1/2"
Z = 27

# -1 is an all-zero ZxZ block; every other entry is a right-circulant
# identity shift.  Keeping the standard base matrix visible makes accidental
# replacement with a generated/design-time matrix difficult.
BASE = np.array([
    [0,-1,-1,-1,0,0,-1,-1,0,-1,-1,0,1,0,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1],
    [22,0,-1,-1,17,-1,0,0,12,-1,-1,-1,-1,0,0,-1,-1,-1,-1,-1,-1,-1,-1,-1],
    [6,-1,0,-1,10,-1,-1,-1,24,-1,0,-1,-1,-1,0,0,-1,-1,-1,-1,-1,-1,-1,-1],
    [2,-1,-1,0,20,-1,-1,-1,25,0,-1,-1,-1,-1,-1,0,0,-1,-1,-1,-1,-1,-1,-1],
    [23,-1,-1,-1,3,-1,-1,-1,0,-1,9,11,-1,-1,-1,-1,0,0,-1,-1,-1,-1,-1,-1],
    [24,-1,23,1,17,-1,3,-1,10,-1,-1,-1,-1,-1,-1,-1,-1,0,0,-1,-1,-1,-1,-1],
    [25,-1,-1,-1,8,-1,-1,-1,7,18,-1,-1,0,-1,-1,-1,-1,-1,0,0,-1,-1,-1,-1],
    [13,24,-1,-1,0,-1,8,-1,6,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,0,0,-1,-1,-1],
    [7,20,-1,16,22,10,-1,-1,23,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,0,0,-1,-1],
    [11,-1,-1,-1,19,-1,-1,-1,13,-1,3,17,-1,-1,-1,-1,-1,-1,-1,-1,-1,0,0,-1],
    [25,-1,8,-1,23,18,-1,14,9,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,0,0],
    [3,-1,-1,-1,16,-1,-1,2,25,5,-1,-1,1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,0],
], dtype=np.int16)

# IEEE 802.11-2020 Table F-2, length 648, rate 2/3 (8 x 24, Z=27).
BASE_2_3 = np.array([
    [25,26,14,-1,20,-1,2,-1,4,-1,-1,8,-1,16,-1,18,1,0,-1,-1,-1,-1,-1,-1],
    [10,9,15,11,-1,0,-1,1,-1,-1,18,-1,8,-1,10,-1,-1,0,0,-1,-1,-1,-1,-1],
    [16,2,20,26,21,-1,6,-1,1,26,-1,7,-1,-1,-1,-1,-1,-1,0,0,-1,-1,-1,-1],
    [10,13,5,0,-1,3,-1,7,-1,-1,26,-1,-1,13,-1,16,-1,-1,-1,0,0,-1,-1,-1],
    [23,14,24,-1,12,-1,19,-1,17,-1,-1,-1,20,-1,21,-1,0,-1,-1,-1,0,0,-1,-1],
    [6,22,9,20,-1,25,-1,17,-1,8,-1,14,-1,18,-1,-1,-1,-1,-1,-1,-1,0,0,-1],
    [14,23,21,11,20,-1,24,-1,18,-1,19,-1,-1,-1,-1,22,-1,-1,-1,-1,-1,-1,0,0],
    [17,11,11,20,-1,21,-1,26,-1,3,-1,-1,18,-1,26,-1,1,-1,-1,-1,-1,-1,-1,0],
], dtype=np.int16)

# IEEE 802.11-2020 Table F-3, length 648, rate 3/4 (6 x 24, Z=27).
BASE_3_4 = np.array([
    [16,17,22,24,9,3,14,-1,4,2,7,-1,26,-1,2,-1,21,-1,1,0,-1,-1,-1,-1],
    [25,12,12,3,3,26,6,21,-1,15,22,-1,15,-1,4,-1,-1,16,-1,0,0,-1,-1,-1],
    [25,18,26,16,22,23,9,-1,0,-1,4,-1,4,-1,8,23,11,-1,-1,-1,0,0,-1,-1],
    [9,7,0,1,17,-1,-1,7,3,-1,3,23,-1,16,-1,-1,21,-1,0,-1,-1,0,0,-1],
    [24,5,26,7,1,-1,-1,15,24,15,-1,8,-1,13,-1,13,-1,11,-1,-1,-1,-1,0,0],
    [2,2,19,14,24,1,15,19,-1,21,-1,2,-1,24,-1,3,-1,2,1,-1,-1,-1,-1,0],
], dtype=np.int16)

INFORMATION_BITS = {"1/2": 324, "2/3": 432, "3/4": 486}
BASES = {"1/2": BASE, "2/3": BASE_2_3, "3/4": BASE_3_4}


@lru_cache(maxsize=None)
def parity_check(rate=RATE):
    base = BASES[rate]
    k = INFORMATION_BITS[rate]
    h = np.zeros((N - k, N), dtype=np.uint8)
    eye = np.eye(Z, dtype=np.uint8)
    for r in range(base.shape[0]):
        for c in range(base.shape[1]):
            shift = int(base[r, c])
            if shift >= 0:
                h[r*Z:(r+1)*Z, c*Z:(c+1)*Z] = np.roll(eye, shift, axis=1)
    return h


def _gf2_solve(a, b):
    """Solve A X=B over GF(2), accepting one or many RHS columns."""
    a = np.asarray(a, dtype=np.uint8).copy()
    b = np.asarray(b, dtype=np.uint8).copy()
    one_dim = b.ndim == 1
    if one_dim:
        b = b[:, None]
    aug = np.concatenate((a, b), axis=1)
    n = len(a)
    for col in range(n):
        pivots = np.flatnonzero(aug[col:, col])
        if not len(pivots):
            raise ValueError("LDPC parity submatrix is singular")
        pivot = col + int(pivots[0])
        aug[[col, pivot]] = aug[[pivot, col]]
        rows = np.flatnonzero(aug[:, col])
        rows = rows[rows != col]
        aug[rows] ^= aug[col]
    out = aug[:, n:]
    return out[:, 0] if one_dim else out


@lru_cache(maxsize=None)
def _parity_map(rate=RATE):
    h = parity_check(rate)
    k = INFORMATION_BITS[rate]
    return _gf2_solve(h[:, k:], h[:, :k])


def encode(information_bits, rate=RATE):
    """Encode one systematic IEEE 802.11n length-648 codeword."""
    k = INFORMATION_BITS[rate]
    u = np.asarray(information_bits, dtype=np.uint8)
    if u.shape != (k,):
        raise ValueError(f"expected {k} information bits")
    return np.concatenate((u, (_parity_map(rate) @ u) & 1)).astype(np.uint8)


@lru_cache(maxsize=None)
def _checks(rate=RATE):
    return tuple(np.flatnonzero(row).astype(np.int32) for row in parity_check(rate))


def syndrome(bits, rate=RATE):
    return (parity_check(rate) @ np.asarray(bits, dtype=np.uint8)) & 1


def decode(llr, max_iterations=30, alpha=0.8, rate=RATE):
    """Normalized min-sum decode; return (information bits, iterations, ok)."""
    channel = np.asarray(llr, dtype=float)
    if channel.shape != (N,):
        raise ValueError(f"expected {N} LLRs")
    k = INFORMATION_BITS[rate]
    checks = _checks(rate)
    c_to_v = [np.zeros(len(v), dtype=float) for v in checks]
    posterior = channel.copy()
    for iteration in range(max_iterations + 1):
        hard = (posterior < 0).astype(np.uint8)
        if not np.any(syndrome(hard, rate)):
            return hard[:k], iteration, True
        if iteration == max_iterations:
            break
        old_posterior = channel.copy()
        for variables, message in zip(checks, c_to_v):
            old_posterior[variables] += message
        new_messages = []
        for ci, variables in enumerate(checks):
            incoming = old_posterior[variables] - c_to_v[ci]
            signs = np.where(incoming < 0, -1.0, 1.0)
            magnitudes = np.abs(incoming)
            smallest = int(np.argmin(magnitudes))
            # A partition finds both check-node minima without allocating the
            # N-1 element temporary that np.delete created for every check on
            # every iteration.
            minima = np.partition(magnitudes, 1)[:2]
            min1, min2 = minima[0], minima[1]
            total_sign = np.prod(signs)
            outgoing = alpha * total_sign * signs * min1
            outgoing[smallest] = alpha * total_sign * signs[smallest] * min2
            new_messages.append(outgoing)
        c_to_v = new_messages
        posterior = channel.copy()
        for variables, message in zip(checks, c_to_v):
            posterior[variables] += message
    hard = (posterior < 0).astype(np.uint8)
    return hard[:k], max_iterations, False


def decode_batch(llrs, max_iterations=30, alpha=0.8, rate=RATE):
    """Decode several codewords at once. Returns (info, iterations, ok).

        llrs        (B, N) float
        info        (B, K) uint8
        iterations  (B,)   int
        ok          (B,)   bool

    Same normalized min-sum as decode(), vectorised over the codeword axis.
    That axis is the one worth vectorising: check-node *degree* varies between
    check nodes but not between codewords, so for each check node the incoming
    messages gather into one (B, degree) array and the whole B-codeword batch
    costs a single pass over the ~324 check nodes instead of B passes.

    qpsk29 always presents exactly qpsk29.CODEWORDS blocks, so this replaces
    a Python loop of that length with one batched sweep.

    Codewords whose syndrome has cleared must be masked out of further
    updates and have their iteration count frozen, so per-codeword early
    termination still applies -- otherwise a single slow codeword makes every
    other one pay max_iterations.

    Must return bit-identical results to calling decode() on each row; that
    equivalence is asserted in test_qpsk29.py, because this is an
    optimisation sitting on the critical decode path and a divergence here
    would quietly invalidate every on-air result.
    """
    raise NotImplementedError
