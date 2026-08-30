"""Rate-1/2 convolutional coding with hard and soft Viterbi decoding.

Parameterized on the generator polynomials and constraint length, but the
default is the K=7 (171, 133) code every VF mode has used, and the
vectorized soft decoder is required to stay bit-for-bit what the scalar
trellis walk produced -- survivor selection, and therefore the CRC,
hangs off its tie-breaking.  `tests/test_vf3_kernels.py` holds it there.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np

K7_POLYNOMIALS = (0o171, 0o133)
K7_CONSTRAINT = 7


def _parity(value: int) -> int:
    return value.bit_count() & 1


@dataclass(frozen=True)
class ConvolutionalCode:
    """A terminated rate-1/2 convolutional code."""

    polynomials: tuple[int, int] = K7_POLYNOMIALS
    constraint: int = K7_CONSTRAINT

    @property
    def states(self) -> int:
        return 1 << (self.constraint - 1)

    @property
    def tail_bits(self) -> int:
        """Zero inputs needed to drive the encoder back to state 0."""
        return self.constraint - 1

    @property
    def _state_mask(self) -> int:
        return self.states - 1

    @property
    def _register_mask(self) -> int:
        return (1 << self.constraint) - 1

    def encode(self, input_bits: np.ndarray) -> np.ndarray:
        input_bits = np.asarray(input_bits, dtype=np.uint8).reshape(-1)
        output = np.empty(2 * len(input_bits), dtype=np.uint8)
        state = 0
        for i, bit_value in enumerate(input_bits):
            register = ((state << 1) | int(bit_value)) & self._register_mask
            output[2 * i] = _parity(register & self.polynomials[0])
            output[2 * i + 1] = _parity(register & self.polynomials[1])
            state = register & self._state_mask
        return output

    @cached_property
    def _transitions(self) -> list[tuple[int, int, int, tuple[int, int]]]:
        transitions = []
        for state in range(self.states):
            for bit in (0, 1):
                register = ((state << 1) | bit) & self._register_mask
                transitions.append((
                    state, bit, register & self._state_mask,
                    (_parity(register & self.polynomials[0]),
                     _parity(register & self.polynomials[1]))))
        return transitions

    def decode_hard(self, coded_bits: np.ndarray) -> np.ndarray:
        """Hard-decision Viterbi over the terminated trellis."""
        coded_bits = np.asarray(coded_bits, dtype=np.uint8).reshape(-1)
        if len(coded_bits) % 2:
            raise ValueError("rate-1/2 code requires an even coded-bit count")
        steps = len(coded_bits) // 2
        states = self.states
        infinity = np.int32(1_000_000_000)
        metrics = np.full(states, infinity, dtype=np.int32)
        metrics[0] = 0
        previous = np.empty((steps, states), dtype=np.uint8)
        inputs = np.empty((steps, states), dtype=np.uint8)
        transitions = self._transitions

        for t in range(steps):
            received0, received1 = map(int, coded_bits[2 * t:2 * t + 2])
            new_metrics = np.full(states, infinity, dtype=np.int32)
            for state, bit, next_state, pair in transitions:
                metric = (metrics[state] + (pair[0] != received0)
                          + (pair[1] != received1))
                if metric < new_metrics[next_state]:
                    new_metrics[next_state] = metric
                    previous[t, next_state] = state
                    inputs[t, next_state] = bit
            metrics = new_metrics

        decoded = np.empty(steps, dtype=np.uint8)
        state = 0  # the terminating zero inputs land here
        for t in range(steps - 1, -1, -1):
            decoded[t] = inputs[t, state]
            state = int(previous[t, state])
        return decoded

    @cached_property
    def _butterfly(self) -> tuple[np.ndarray, np.ndarray, np.ndarray,
                                  np.ndarray, np.ndarray]:
        """The trellis transposed to be indexed by *next* state.

        Each next state has exactly two predecessors -- `next_state >> 1`
        and that plus half the state count -- both entered on the same
        input bit, `next_state & 1`.  Returning the branch signs already
        negated lets the per-step update be two fused multiply-adds over
        length-`states` vectors instead of a Python loop over transitions.
        """
        states = self.states
        high = states >> 1
        predecessors = np.empty((2, states), dtype=np.intp)
        weights = np.empty((2, states, 2), dtype=np.float64)
        input_bits = np.empty(states, dtype=np.uint8)
        for next_state in range(states):
            bit = next_state & 1
            input_bits[next_state] = bit
            for branch in (0, 1):
                state = (next_state >> 1) | (branch * high)
                predecessors[branch, next_state] = state
                register = ((state << 1) | bit) & self._register_mask
                pair = (_parity(register & self.polynomials[0]),
                        _parity(register & self.polynomials[1]))
                # -signs, so the update is metrics[pred] + w0*r0 + w1*r1.
                weights[branch, next_state] = (2 * pair[0] - 1,
                                               2 * pair[1] - 1)
        return (predecessors[0], predecessors[1],
                weights[0], weights[1], input_bits)

    def decode_soft(self, soft_bits: np.ndarray) -> np.ndarray:
        """Soft Viterbi; input sign is the bit hypothesis, magnitude its
        confidence.  Positive means bit zero."""
        soft_bits = np.asarray(soft_bits, dtype=np.float64).reshape(-1)
        if len(soft_bits) % 2:
            raise ValueError("rate-1/2 code requires an even soft-bit count")
        steps = len(soft_bits) // 2
        received = soft_bits.reshape(steps, 2)
        states = self.states
        pred0, pred1, weight0, weight1, input_bits = self._butterfly
        metrics = np.full(states, np.inf)
        metrics[0] = 0.0
        previous = np.empty((steps, states), dtype=np.uint8)
        branch0 = np.empty(states)
        branch1 = np.empty(states)
        take1 = np.empty(states, dtype=bool)
        for t in range(steps):
            received0, received1 = received[t]
            np.add(metrics[pred0],
                   weight0[:, 0] * received0 + weight0[:, 1] * received1,
                   out=branch0)
            np.add(metrics[pred1],
                   weight1[:, 0] * received0 + weight1[:, 1] * received1,
                   out=branch1)
            # Strict `<` keeps the lower-numbered predecessor on an exact
            # tie, matching the order the scalar trellis walk visited them.
            np.less(branch1, branch0, out=take1)
            metrics = np.where(take1, branch1, branch0)
            previous[t] = np.where(take1, pred1, pred0)
        decoded = np.empty(steps, dtype=np.uint8)
        state = 0
        for t in range(steps - 1, -1, -1):
            decoded[t] = input_bits[state]
            state = int(previous[t, state])
        return decoded


K7 = ConvolutionalCode()

#: Rate-1/2, K=9 (561, 753) -- standard octal generators, one more bit of
#: constraint length than K7.  Free distance 12 against K7's 10, roughly
#: 0.6-1 dB more coding gain in the regime these modes operate in, at 4x the
#: trellis states (256 vs 64) and therefore roughly 4x the decode work per
#: coded bit.  Built for HC2 (`experiments/hc2/hc2.py`), which spends that
#: extra margin buying back some of what 8-PSK costs against QPSK; see that
#: module's docstring for the decode-time measurement that justifies paying
#: for it.
K9_POLYNOMIALS = (0o561, 0o753)
K9_CONSTRAINT = 9
K9 = ConvolutionalCode(polynomials=K9_POLYNOMIALS, constraint=K9_CONSTRAINT)
