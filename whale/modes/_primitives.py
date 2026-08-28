"""Bit, QPSK and rate-1/2 convolutional primitives shared by the OFDM modes.

Lifted verbatim from `experiments/vf2/vf2.py`, which is where they were
written and where the VF2/VF3/VF4/VF5 bench results were produced against
them.  The experiment keeps its own copy so it stays standalone; this is
the shipped one.  Any change here must keep both sides bit-identical or
the recorded captures under `experiments/*/results/` stop replaying.
"""

from __future__ import annotations

import numpy as np


def _lfsr_bits(count: int, seed: int) -> np.ndarray:
    """Deterministic order-17 PN sequence, returned as uint8 bits."""
    state = seed & 0x1FFFF
    if state == 0:
        raise ValueError("LFSR seed must be non-zero")
    out = np.empty(count, dtype=np.uint8)
    for i in range(count):
        out[i] = state & 1
        feedback = ((state >> 0) ^ (state >> 3)) & 1
        state = (state >> 1) | (feedback << 16)
    return out


def qpsk_from_bits(bits: np.ndarray) -> np.ndarray:
    """Map pairs [real-sign bit, imag-sign bit] to unit-energy QPSK."""
    bits = np.asarray(bits, dtype=np.uint8)
    if bits.size % 2:
        raise ValueError("QPSK needs an even number of bits")
    pairs = bits.reshape(-1, 2)
    return ((1.0 - 2.0 * pairs[:, 0])
            + 1j * (1.0 - 2.0 * pairs[:, 1])) / np.sqrt(2.0)


def bits_from_qpsk(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    # Keep the final axis carrier-major: [Re0, Im0, Re1, Im1, ...].
    # column_stack would group all real bits before all imaginary bits when
    # `values` is a 2-D symbol/carrier grid.
    return np.stack((values.real < 0.0, values.imag < 0.0), axis=-1).astype(
        np.uint8).reshape(-1)


def slice_qpsk(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values)
    real = np.where(values.real >= 0.0, 1.0, -1.0)
    imag = np.where(values.imag >= 0.0, 1.0, -1.0)
    return (real + 1j * imag) / np.sqrt(2.0)



_CONV_POLYNOMIALS = (0o171, 0o133)
_CONV_STATES = 64


def _parity(value: int) -> int:
    return value.bit_count() & 1


def convolutional_encode(input_bits: np.ndarray) -> np.ndarray:
    input_bits = np.asarray(input_bits, dtype=np.uint8).reshape(-1)
    output = np.empty(2 * len(input_bits), dtype=np.uint8)
    state = 0
    for i, bit_value in enumerate(input_bits):
        bit = int(bit_value)
        register = ((state << 1) | bit) & 0x7F
        output[2 * i] = _parity(register & _CONV_POLYNOMIALS[0])
        output[2 * i + 1] = _parity(register & _CONV_POLYNOMIALS[1])
        state = register & 0x3F
    return output


def convolutional_decode(coded_bits: np.ndarray) -> np.ndarray:
    """Hard-decision Viterbi decoder for the terminated VF2 trellis."""
    coded_bits = np.asarray(coded_bits, dtype=np.uint8).reshape(-1)
    if len(coded_bits) % 2:
        raise ValueError("rate-1/2 code requires an even coded-bit count")
    steps = len(coded_bits) // 2
    infinity = np.int32(1_000_000_000)
    metrics = np.full(_CONV_STATES, infinity, dtype=np.int32)
    metrics[0] = 0
    previous = np.empty((steps, _CONV_STATES), dtype=np.uint8)
    inputs = np.empty((steps, _CONV_STATES), dtype=np.uint8)

    transitions = []
    for state in range(_CONV_STATES):
        for bit in (0, 1):
            register = ((state << 1) | bit) & 0x7F
            next_state = register & 0x3F
            pair = (_parity(register & _CONV_POLYNOMIALS[0]),
                    _parity(register & _CONV_POLYNOMIALS[1]))
            transitions.append((state, bit, next_state, pair))

    for t in range(steps):
        received0, received1 = map(int, coded_bits[2 * t:2 * t + 2])
        new_metrics = np.full(_CONV_STATES, infinity, dtype=np.int32)
        for state, bit, next_state, pair in transitions:
            metric = metrics[state] + (pair[0] != received0) + (pair[1] != received1)
            if metric < new_metrics[next_state]:
                new_metrics[next_state] = metric
                previous[t, next_state] = state
                inputs[t, next_state] = bit
        metrics = new_metrics

    decoded = np.empty(steps, dtype=np.uint8)
    state = 0  # the final six zero inputs terminate here
    for t in range(steps - 1, -1, -1):
        decoded[t] = inputs[t, state]
        state = int(previous[t, state])
    return decoded
