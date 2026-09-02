"""Minimal IEEE 802.11n length-648, rate-1/2 QC-LDPC codec for HR0.

This is intentionally private to the experiment.  The visible base matrix is
IEEE 802.11-2020 Table F-1 (Z=27); positive LLR means bit zero.
"""

from functools import lru_cache

import numpy as np


N = 648
K = 324
Z = 27

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


@lru_cache(maxsize=1)
def parity_check():
    h = np.zeros((N - K, N), dtype=np.uint8)
    eye = np.eye(Z, dtype=np.uint8)
    for row in range(BASE.shape[0]):
        for column in range(BASE.shape[1]):
            shift = int(BASE[row, column])
            if shift >= 0:
                h[row*Z:(row+1)*Z, column*Z:(column+1)*Z] = np.roll(
                    eye, shift, axis=1)
    return h


def _gf2_solve(a, b):
    aug = np.concatenate((np.asarray(a, np.uint8).copy(),
                          np.asarray(b, np.uint8).copy()), axis=1)
    n = len(a)
    for column in range(n):
        choices = np.flatnonzero(aug[column:, column])
        if not len(choices):
            raise ValueError("singular LDPC parity submatrix")
        pivot = column + int(choices[0])
        aug[[column, pivot]] = aug[[pivot, column]]
        rows = np.flatnonzero(aug[:, column])
        aug[rows[rows != column]] ^= aug[column]
    return aug[:, n:]


@lru_cache(maxsize=1)
def _parity_map():
    h = parity_check()
    return _gf2_solve(h[:, K:], h[:, :K])


def encode(information_bits):
    information = np.asarray(information_bits, dtype=np.uint8)
    if information.shape != (K,):
        raise ValueError(f"expected {K} information bits")
    parity = (_parity_map() @ information) & 1
    return np.concatenate((information, parity)).astype(np.uint8)


@lru_cache(maxsize=1)
def _checks():
    return tuple(np.flatnonzero(row).astype(np.int32) for row in parity_check())


def syndrome(bits):
    return (parity_check() @ np.asarray(bits, dtype=np.uint8)) & 1


def decode(llr, max_iterations=50, alpha=0.8):
    channel = np.asarray(llr, dtype=float)
    if channel.shape != (N,):
        raise ValueError(f"expected {N} LLRs")
    checks = _checks()
    check_messages = [np.zeros(len(variables)) for variables in checks]
    posterior = channel.copy()
    for iteration in range(max_iterations + 1):
        hard = (posterior < 0).astype(np.uint8)
        if not np.any(syndrome(hard)):
            return hard[:K], iteration, True
        if iteration == max_iterations:
            break
        extrinsic = channel.copy()
        for variables, message in zip(checks, check_messages):
            extrinsic[variables] += message
        next_messages = []
        for index, variables in enumerate(checks):
            incoming = extrinsic[variables] - check_messages[index]
            signs = np.where(incoming < 0, -1.0, 1.0)
            magnitudes = np.abs(incoming)
            smallest = int(np.argmin(magnitudes))
            minimum, second = np.partition(magnitudes, 1)[:2]
            total_sign = np.prod(signs)
            outgoing = alpha * total_sign * signs * minimum
            outgoing[smallest] = alpha * total_sign * signs[smallest] * second
            next_messages.append(outgoing)
        check_messages = next_messages
        posterior = channel.copy()
        for variables, message in zip(checks, check_messages):
            posterior[variables] += message
    return (posterior < 0).astype(np.uint8)[:K], max_iterations, False
