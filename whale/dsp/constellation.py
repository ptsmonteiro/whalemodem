"""Bit<->symbol constellation mapping and soft-bit demapping.

`bits_to_symbols`/`symbols_to_bits` (BPSK/QPSK/8PSK/16-QAM/64-QAM, Gray
coded on each axis) moved here from `whale/phy/sc.py`, where they were
developed for hf5's single-carrier PHY (see
`experiments/hf5_8psk_4k/RESULTS.md`) and then reused unmodified by every
later PHY that needs the same mapping.

`constellation_table` and `soft_bit_llrs` moved here from
`whale/phy/ofdm49.py` (developed in `experiments/hf10_ofdm49_v6/`, see its
RESULTS.md) and made public: this is waveform-independent symbol-mapping
math, and it does not belong reached into from another package's private
names (`whale/modes/vf12.py`, an FM mode, used to import
`whale.phy.ofdm49`'s `_constellation_table`/`_soft_bit_llrs` directly).

Both take an optional `mapper` (default: this module's own
`bits_to_symbols`) because `whale/phy/ofdm49.py` extends the base mapping
with a 32-QAM point at bits_per_symbol=5 that is specific to that PHY;
passing that PHY's own `bits_to_symbols` as `mapper` is what keeps the
constellation table -- and the soft-bit LLRs built from it -- consistent
with what actually went on the air.
"""

from __future__ import annotations

import numpy as np

from whale.dsp import bits as _bits


def bits_to_symbols(bits: np.ndarray, bps: int) -> np.ndarray:
    bits = np.asarray(bits, dtype=np.uint8)
    if bps == 1:
        return (1.0 - 2.0 * bits.astype(np.float64)).astype(np.complex128)
    if bps == 2:
        return _bits.qpsk_from_bits(bits)
    if bps == 3:
        groups = bits.reshape(-1, 3)
        idx = (groups[:, 0] << 2) | (groups[:, 1] << 1) | groups[:, 2]
        gray = np.array([0, 1, 3, 2, 6, 7, 5, 4])
        phase = 2 * np.pi * gray[idx] / 8.0
        return np.exp(1j * phase)
    if bps == 4:
        # 16-QAM, Gray-coded per axis, unit average energy.
        groups = bits.reshape(-1, 4)
        def axis(b0, b1):
            # Gray: 00->-3 01->-1 11->+1 10->+3
            level = np.where(b0 == 0,
                              np.where(b1 == 0, -3.0, -1.0),
                              np.where(b1 == 0, 3.0, 1.0))
            return level
        re = axis(groups[:, 0], groups[:, 1])
        im = axis(groups[:, 2], groups[:, 3])
        return (re + 1j * im) / np.sqrt(10.0)
    if bps == 6:
        # 64-QAM, Gray-coded independently on the I and Q axes.
        # 000..100 map to -7,-5,-3,-1,+1,+3,+5,+7.
        groups = bits.reshape(-1, 6)
        levels = np.array([-7.0, -5.0, -3.0, -1.0,
                           1.0, 3.0, 5.0, 7.0])
        # Binary label -> physical level position.  This is the inverse of
        # the Gray sequence: adjacent amplitudes differ by one bit.
        binary_to_level = np.array([0, 1, 3, 2, 7, 6, 4, 5])
        re = levels[binary_to_level[(groups[:, 0] << 2) | (groups[:, 1] << 1) | groups[:, 2]]]
        im = levels[binary_to_level[(groups[:, 3] << 2) | (groups[:, 4] << 1) | groups[:, 5]]]
        return (re + 1j * im) / np.sqrt(42.0)
    raise ValueError(f"unsupported bits_per_symbol={bps}")


def symbols_to_bits(symbols: np.ndarray, bps: int) -> np.ndarray:
    symbols = np.asarray(symbols)
    if bps == 1:
        return (symbols.real < 0.0).astype(np.uint8)
    if bps == 2:
        return _bits.bits_from_qpsk(symbols)
    if bps == 3:
        phase = np.mod(np.angle(symbols), 2 * np.pi)
        idx = np.round(phase / (2 * np.pi / 8)).astype(int) % 8
        inv_gray = np.array([0, 1, 3, 2, 7, 6, 4, 5])  # inverse of gray table
        val = inv_gray[idx]
        b0 = (val >> 2) & 1
        b1 = (val >> 1) & 1
        b2 = val & 1
        return np.stack((b0, b1, b2), axis=-1).astype(np.uint8).reshape(-1)
    if bps == 4:
        re = symbols.real * np.sqrt(10.0)
        im = symbols.imag * np.sqrt(10.0)
        def axis_bits(v):
            b0 = (v >= 0.0).astype(np.uint8)
            b1 = (np.abs(v) <= 2.0).astype(np.uint8)
            return b0, b1
        b0r, b1r = axis_bits(re)
        b0i, b1i = axis_bits(im)
        return np.stack((b0r, b1r, b0i, b1i), axis=-1).astype(np.uint8).reshape(-1)
    if bps == 6:
        # Nearest-neighbour decision followed by the inverse Gray map.
        levels = np.array([-7.0, -5.0, -3.0, -1.0,
                           1.0, 3.0, 5.0, 7.0])
        level_to_binary = np.array([0, 1, 3, 2, 6, 7, 5, 4])
        def axis_bits(v):
            idx = np.argmin(np.abs(v[:, None] - levels[None, :]), axis=1)
            val = level_to_binary[idx]
            return ((val >> 2) & 1, (val >> 1) & 1, val & 1)
        re = symbols.real * np.sqrt(42.0)
        im = symbols.imag * np.sqrt(42.0)
        br = axis_bits(re)
        bi = axis_bits(im)
        return np.stack((*br, *bi), axis=-1).astype(np.uint8).reshape(-1)
    raise ValueError(f"unsupported bits_per_symbol={bps}")


# Keyed on the mapper itself, not `id(mapper)`: a collected function's
# address can be reused by a later one, which would serve that mapper a
# constellation built for a different mapping.
_CONSTELLATION_CACHE: dict[tuple[object, int], tuple[np.ndarray, np.ndarray]] = {}


def constellation_table(bps: int, mapper=bits_to_symbols) -> tuple[np.ndarray, np.ndarray]:
    """All 2**bps constellation points for `mapper(., bps)`, built by
    brute force from that same function so the table is guaranteed
    consistent with the hard-decision mapping used elsewhere. Returns
    (symbols[2**bps], bits[2**bps, bps]) with bits[:, j] the j-th bit
    `mapper` consumes for that point (column 0 = first/MSB bit)."""
    cache_key = (mapper, bps)
    cached = _CONSTELLATION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    m = 1 << bps
    idx = np.arange(m)
    bits = ((idx[:, None] >> np.arange(bps - 1, -1, -1)) & 1).astype(np.uint8)
    syms = mapper(bits.reshape(-1), bps)
    _CONSTELLATION_CACHE[cache_key] = (syms, bits)
    return syms, bits


def soft_bit_llrs(rx_syms: np.ndarray, bps: int, noise_var, mapper=bits_to_symbols) -> np.ndarray:
    """Generic max-log-MAP soft bit LLR demapper: LLR = (min dist^2 over
    constellation points with bit=1) - (min dist^2 over points with bit=0),
    scaled by per-symbol noise variance, matching qpsk29's LDPC
    convention that a positive LLR means bit zero. Works for any
    `bits_per_symbol` `mapper` supports (1-6 for the mappers in this
    codebase) via brute-force distance over the (<=64-point) constellation
    -- no per-constellation closed form needed."""
    rx_syms = np.asarray(rx_syms)
    syms_table, bits_table = constellation_table(bps, mapper=mapper)
    nv = np.atleast_1d(np.asarray(noise_var, dtype=np.float64))
    if nv.shape[0] == 1:
        nv = np.full(rx_syms.shape[0], nv[0])
    nv = np.maximum(nv, 1e-9)
    d2 = np.abs(rx_syms[:, None] - syms_table[None, :]) ** 2 / nv[:, None]
    llrs = np.empty((rx_syms.shape[0], bps), dtype=np.float64)
    for j in range(bps):
        is0 = bits_table[:, j] == 0
        llrs[:, j] = np.min(d2[:, ~is0], axis=1) - np.min(d2[:, is0], axis=1)
    return llrs.reshape(-1)
