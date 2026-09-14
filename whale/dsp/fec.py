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

#: Standard rate-1/2-mother puncturing patterns (period given in mother
#: x,y-interleaved bit order; True = transmitted, False = dropped).  These
#: are the widely published K=7 puncturing tables (802.11a/DVB-S lineage),
#: not derived here -- only checked for output length against the target
#: rate.  Puncturing removes coded bits from the mother rate-1/2 stream to
#: raise the effective code rate; depuncturing reinserts zero-LLR
#: (erasure) at the dropped positions so `ConvolutionalCode.decode_soft`
#: sees a full-length mother-rate soft-bit array again.
PUNCTURE_PATTERNS: dict[str, np.ndarray | None] = {
    "1/2": None,
    "2/3": np.array([1, 1, 1, 0], dtype=bool),
    "3/4": np.array([1, 1, 1, 0, 0, 1], dtype=bool),
    "5/6": np.array([1, 1, 0, 1, 1, 0, 0, 1, 1, 0], dtype=bool),
    "7/8": np.array([1, 1, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0], dtype=bool),
}

#: code_rate = info_bits / transmitted_bits for each pattern above (and for
#: "1/2", the mother rate itself).
PUNCTURE_RATE_VALUE: dict[str, float] = {
    rate: (0.5 if pattern is None else (len(pattern) / 2) / int(pattern.sum()))
    for rate, pattern in PUNCTURE_PATTERNS.items()
}


def _pattern(rate: str) -> np.ndarray | None:
    try:
        return PUNCTURE_PATTERNS[rate]
    except KeyError:
        raise ValueError(
            f"unknown puncture rate {rate!r}; have {sorted(PUNCTURE_PATTERNS)}"
        ) from None


def puncture(mother_bits: np.ndarray, rate: str) -> np.ndarray:
    """Drop the bits `rate`'s pattern marks False from a mother (rate-1/2)
    coded-bit stream, tiling the pattern across its whole length."""
    pattern = _pattern(rate)
    mother_bits = np.asarray(mother_bits).reshape(-1)
    if pattern is None:
        return mother_bits
    if len(mother_bits) % len(pattern):
        raise ValueError(
            f"{len(mother_bits)} mother bits is not a multiple of the "
            f"rate-{rate} puncture period {len(pattern)}")
    mask = np.tile(pattern, len(mother_bits) // len(pattern))
    return mother_bits[mask]


def depuncture(coded_bits: np.ndarray, rate: str, mother_length: int,
               erasure: float = 0.0) -> np.ndarray:
    """Inverse of `puncture`: reinsert `erasure` (zero LLR by default) at
    the dropped positions so the result is `mother_length` long again."""
    pattern = _pattern(rate)
    coded_bits = np.asarray(coded_bits, dtype=np.float64).reshape(-1)
    if pattern is None:
        if len(coded_bits) != mother_length:
            raise ValueError("rate-1/2 coded bits must equal mother_length")
        return coded_bits
    if mother_length % len(pattern):
        raise ValueError(
            f"mother_length {mother_length} is not a multiple of the "
            f"rate-{rate} puncture period {len(pattern)}")
    mask = np.tile(pattern, mother_length // len(pattern))
    if int(mask.sum()) != len(coded_bits):
        raise ValueError(
            f"expected {int(mask.sum())} punctured coded bits for "
            f"mother_length={mother_length} at rate {rate}, got "
            f"{len(coded_bits)}")
    out = np.full(mother_length, erasure, dtype=np.float64)
    out[mask] = coded_bits
    return out

#: Rate-1/2, K=9 (561, 753) -- standard octal generators, one more bit of
#: constraint length than K7.  Free distance 12 against K7's 10, roughly
#: 0.6-1 dB more coding gain in the regime these modes operate in, at 4x the
#: trellis states (256 vs 64) and therefore roughly 4x the decode work per
#: coded bit.  Built for HC2, the retired differential-8-PSK candidate,
#: which spends that extra margin buying back some of what 8-PSK costs
#: against QPSK; `experiments/hc2/RESULTS.md` records the decode-time cost
#: and why HC2 itself did not clear the bar.
K9_POLYNOMIALS = (0o561, 0o753)
K9_CONSTRAINT = 9
K9 = ConvolutionalCode(polynomials=K9_POLYNOMIALS, constraint=K9_CONSTRAINT)
