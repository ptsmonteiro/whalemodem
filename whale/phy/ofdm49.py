"""49-subcarrier true-OFDM PHY (v6), extending the v5 PHY of the retired
experiments/hf9_ofdm49_v5/.

Developed in the retired `experiments/hf10_ofdm49_v6/` and moved here
unmodified when it became shipped product code; its qualification record is
`experiments/hf10_ofdm49_v6/RESULTS.md`, and the configurations wired on top
of it are measured in `experiments/hf18_ofdm49_vara/RESULTS.md` (HF7) and
`experiments/hf19_ofdm49_8psk/RESULTS.md` (HF8).

v5 established, on real hardware, that 49 contiguous 8PSK subcarriers
spanning the full 300-2700 Hz passband decode reliably at ~4014 bps net
(10/10, zero bit errors), matching the project's single-carrier record.
v6's job is to push further on TWO independent levers the project's own
history flagged as unexploited:

  1. Higher-order modulation (16-QAM) reusing v5's already-4-bit-capable
     `bits_to_symbols`/`symbols_to_bits` (from `whale/phy/sc.py`,
     developed as hf5's sc.py) directly on the 49-bin structure, to
     re-test (on THIS design) the project's repeatedly-confirmed
     real-hardware 16-QAM fragility (hf5, hf7),
     which was never reproduced in this project's own AWGN simulation --
     i.e. simulation cannot be trusted to predict this failure mode, so
     it must be tested for real, on hardware, again, here.
  2. FEC: a rate-1/2, 2/3, or 3/4 IEEE-802.11n QC-LDPC code, reused
     verbatim from `whale/dsp/ldpc.py` (developed in the retired
     experiments/qpsk29/; dependency-free, already used
     successfully with real coding gain in that experiment and in the
     retired experiments/ofdm/'s HF trials), applied across the packet's
     whitened bit stream. New in this module vs v5:

     - `fec_rate` field (None | "1/2" | "2/3" | "3/4"). When set,
       `modulate()` LDPC-encodes the packet bits in fixed 648-bit
       codewords before whitening/mapping, and `demodulate()` computes a
       genuine soft bit LLR per coded bit (generic max-log constellation
       demapper, not just v5's hard `symbols_to_bits`) and runs
       `ldpc.decode_batch` before CRC-checking.
     - A generic `_soft_bit_llrs()` demapper works for any
       `bits_per_symbol` (1-6) by brute-force max-log distance over the
       constellation returned by `_constellation_table()`, built directly
       from `sc.bits_to_symbols` so the mapping is guaranteed
       consistent with the hard-decision path.
     - `raw_bits`/`raw_packet_bits` in the demod result are now the
       POST-FEC-DECODE bits when FEC is enabled (for CRC/payload
       purposes), while a new `pre_fec_bits` field carries the raw
       (uncoded, hard-demapped) bits so the test harness can report BOTH
       raw and residual (post-FEC) BER, per the task's requirement.

  3. Frame/geometry and receiver levers found on hardware on 2026-09-07,
     which together took this mode from 4332 bps to 7213 bps net (see
     `experiments/hf10_ofdm49_v6/RESULTS.md`, "2026-09-07"): a 32-QAM
     mapping at `bits_per_symbol=5` (absent from sc.py's original mapper,
     added above); an `interleave` flag that spreads each LDPC codeword
     over the whole frame; and, as pure parameters needing no code
     change, a larger `fft_size` (the same
     absolute guard time amortized over a longer symbol) and a much
     lower `drive_scale`.

Everything else (preamble/pilot structure, sync, per-bin gain equalizer,
phase_slope/comb-pilot options, edge guard/taper) is v5's code,
unmodified in behaviour when fec_rate=None, interleave=False and
bits_per_symbol<=3.

None of the hf5/hf6/hf7/hf8/hf9/path_probe experiments were modified when
this waveform was written; it began life as a fresh copy in hf10's own
directory per that task's constraints.  `whale/dsp/ldpc.py` is used as a
shared kernel rather than copied, since it is a generic, dependency-free
codec with no qpsk29-specific state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import fftconvolve

from whale.dsp import bits as _bits
from whale.dsp import ldpc as _ldpc
from whale.phy import sc as _sc

TX_SAMPLE_RATE = _sc.TX_SAMPLE_RATE
RX_SAMPLE_RATE = _sc.RX_SAMPLE_RATE
DESIGN_RATE = RX_SAMPLE_RATE

BAND_LO_HZ = 300.0
BAND_HI_HZ = 2700.0

LENGTH_BYTES = _sc.LENGTH_BYTES
CRC_BYTES = _sc.CRC_BYTES
WHITENER_SEED = 0xBEEF17
INTERLEAVER_SEED = 0x5EED1A

SYNC_SEARCH_HZ = 20.0
SYNC_SEARCH_STEP_HZ = 1.0
SYNC_HINT_RADIUS_HZ = 2.0

# 32-QAM (bits_per_symbol=5) is not in hf5's shared mapper, which jumps
# straight from 16-QAM to 64-QAM, and hf5 is not modified by this
# experiment. It is added here as a rectangular 4x8 constellation: 3 Gray
# bits on I over 8 levels (hf5's own 64-QAM axis map) and 2 Gray bits on Q
# over 4 levels (hf5's own 16-QAM axis map), so the labelling is Gray on
# both axes by construction and reuses mappings already validated here.
#
# A 32-cross constellation would carry the same 5 bits at mean energy 20
# instead of this rectangle's 26 -- 0.55 dB better -- but it has no
# per-axis Gray labelling, and the rectangle is the version whose
# correctness is obvious by inspection. If a hardware trial lands just
# short of a target, that 0.55 dB is the known reserve to spend next.
_QAM32_I_LEVELS = np.array([-7.0, -5.0, -3.0, -1.0, 1.0, 3.0, 5.0, 7.0])
_QAM32_I_BINARY_TO_LEVEL = np.array([0, 1, 3, 2, 7, 6, 4, 5])
_QAM32_I_LEVEL_TO_BINARY = np.array([0, 1, 3, 2, 6, 7, 5, 4])
_QAM32_Q_LEVELS = np.array([-3.0, -1.0, 1.0, 3.0])
_QAM32_Q_BINARY_TO_LEVEL = np.array([0, 1, 3, 2])
_QAM32_Q_LEVEL_TO_BINARY = np.array([0, 1, 3, 2])
_QAM32_SCALE = np.sqrt(26.0)   # mean(I^2)=21, mean(Q^2)=5 -> unit mean energy


def bits_to_symbols(bits: np.ndarray, bps: int) -> np.ndarray:
    """hf5's mapper, plus 32-QAM at bps=5 (see _QAM32_* above)."""
    if bps != 5:
        return _sc.bits_to_symbols(bits, bps)
    groups = np.asarray(bits, dtype=np.uint8).reshape(-1, 5)
    i_idx = (groups[:, 0] << 2) | (groups[:, 1] << 1) | groups[:, 2]
    q_idx = (groups[:, 3] << 1) | groups[:, 4]
    re = _QAM32_I_LEVELS[_QAM32_I_BINARY_TO_LEVEL[i_idx]]
    im = _QAM32_Q_LEVELS[_QAM32_Q_BINARY_TO_LEVEL[q_idx]]
    return (re + 1j * im) / _QAM32_SCALE


def symbols_to_bits(symbols: np.ndarray, bps: int) -> np.ndarray:
    """Inverse of `bits_to_symbols`, including bps=5."""
    if bps != 5:
        return _sc.symbols_to_bits(symbols, bps)
    symbols = np.asarray(symbols)
    re = symbols.real * _QAM32_SCALE
    im = symbols.imag * _QAM32_SCALE
    i_lvl = np.argmin(np.abs(re[:, None] - _QAM32_I_LEVELS[None, :]), axis=1)
    q_lvl = np.argmin(np.abs(im[:, None] - _QAM32_Q_LEVELS[None, :]), axis=1)
    i_val = _QAM32_I_LEVEL_TO_BINARY[i_lvl]
    q_val = _QAM32_Q_LEVEL_TO_BINARY[q_lvl]
    return np.stack(((i_val >> 2) & 1, (i_val >> 1) & 1, i_val & 1,
                     (q_val >> 1) & 1, q_val & 1),
                    axis=-1).astype(np.uint8).reshape(-1)


_pack_packet = _sc._pack_packet
_unpack_packet = _sc._unpack_packet
_pn_chips = _sc._pn_chips


def bins_in_band(fft_size: int, lo_hz: float = BAND_LO_HZ, hi_hz: float = BAND_HI_HZ) -> list[int]:
    spacing = DESIGN_RATE / fft_size
    lo_bin = int(np.ceil(lo_hz / spacing))
    hi_bin = int(np.floor(hi_hz / spacing))
    hi_bin = min(hi_bin, fft_size // 2 - 1)
    lo_bin = max(lo_bin, 1)
    return list(range(lo_bin, hi_bin + 1))


def _newman_phases(n: int) -> np.ndarray:
    k = np.arange(n)
    return np.pi * k * k / max(n, 1)


def _hilbert_analytic(x: np.ndarray) -> np.ndarray:
    n = len(x)
    xf = np.fft.fft(x)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1
        h[1:n // 2] = 2
    else:
        h[0] = 1
        h[1:(n + 1) // 2] = 2
    return np.fft.ifft(xf * h)


def _freq_shift_real(x: np.ndarray, hz: float, rate: float) -> np.ndarray:
    analytic = _hilbert_analytic(x)
    n = np.arange(len(x))
    shifted = analytic * np.exp(1j * 2 * np.pi * hz * n / rate)
    return shifted.real


_CONSTELLATION_CACHE: dict[int, tuple[np.ndarray, np.ndarray]] = {}


def _constellation_table(bps: int) -> tuple[np.ndarray, np.ndarray]:
    """All 2**bps constellation points for `bits_to_symbols(., bps)`, built
    by brute force from that same function so the table is guaranteed
    consistent with the hard-decision mapping used elsewhere. Returns
    (symbols[2**bps], bits[2**bps, bps]) with bits[:, j] the j-th bit
    `bits_to_symbols` consumes for that point (column 0 = first/MSB bit)."""
    cached = _CONSTELLATION_CACHE.get(bps)
    if cached is not None:
        return cached
    m = 1 << bps
    idx = np.arange(m)
    bits = ((idx[:, None] >> np.arange(bps - 1, -1, -1)) & 1).astype(np.uint8)
    syms = bits_to_symbols(bits.reshape(-1), bps)
    _CONSTELLATION_CACHE[bps] = (syms, bits)
    return syms, bits


def _soft_bit_llrs(rx_syms: np.ndarray, bps: int, noise_var) -> np.ndarray:
    """Generic max-log-MAP soft bit LLR demapper: LLR = (min dist^2 over
    constellation points with bit=1) - (min dist^2 over points with bit=0),
    scaled by per-symbol noise variance, matching qpsk29's LDPC
    convention that a positive LLR means bit zero. Works for any
    `bits_per_symbol` supported by `bits_to_symbols` (1-6 here) via
    brute-force distance over the (<=16-point) constellation -- no
    per-constellation closed form needed."""
    rx_syms = np.asarray(rx_syms)
    syms_table, bits_table = _constellation_table(bps)
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


@dataclass(frozen=True)
class OFDM49Mode:
    fft_size: int
    cp_len: int
    active_bins: tuple[int, ...]
    bits_per_symbol: int
    packet_bytes: int
    pilot_interval: int = 0
    n_preamble_symbols: int = 2
    drive_scale: float = 0.5
    preamble_seed: int = 0x33
    pilot_seed: int = 0x51
    equalizer: str = "gain"           # "gain" | "phase_slope"
    pilot_comb_stride: int = 0        # 0 disables; else every Nth active bin (by
                                       # position in sorted active_bins) is a
                                       # comb pilot present in every OFDM symbol
    edge_guard_bins: int = 0          # trim this many bins off each end of
                                       # active_bins at construction time
    edge_taper: int = 0               # taper (reduce power on) this many bins
                                       # at each end of the surviving active set
    fec_rate: str | None = None       # None | "1/2" | "2/3" | "3/4" -- IEEE
                                       # 802.11n QC-LDPC (whale/dsp/ldpc.py),
                                       # applied to the whitened packet bit
                                       # stream before symbol mapping.

    interleave: bool = False          # spread each LDPC codeword's bits over
                                       # the whole frame (see _interleaver)

    comb_tracking: str = "legacy"  # legacy | common | confidence | residual | off

    def __post_init__(self):
        if self.comb_tracking not in ("legacy", "common", "confidence", "residual", "off"):
            raise ValueError("unknown comb tracking method")
        bins = tuple(sorted(self.active_bins))
        if self.edge_guard_bins:
            g = self.edge_guard_bins
            if len(bins) > 2 * g:
                bins = bins[g:len(bins) - g]
        object.__setattr__(self, "active_bins", bins)
        n_active = len(bins)

        comb_idx = np.zeros(n_active, dtype=bool)
        if self.pilot_comb_stride and self.pilot_comb_stride > 0:
            comb_idx[::self.pilot_comb_stride] = True
        object.__setattr__(self, "_comb_mask", comb_idx)
        object.__setattr__(self, "_data_idx", np.where(~comb_idx)[0])
        object.__setattr__(self, "_comb_idx", np.where(comb_idx)[0])

        phases = _newman_phases(n_active)

        # per-bin TX amplitude taper (edge weighting experiment); 1.0 by
        # default (no-op).
        amp = np.ones(n_active)
        if self.edge_taper:
            t = self.edge_taper
            floor = 0.2  # never fully zero: a bin at exactly 0 amplitude
            # divides by zero in the receiver's per-bin gain estimate
            raw_ramp = 0.5 * (1 - np.cos(np.linspace(0, np.pi, min(t, n_active // 2 + 1))))
            ramp = floor + (1.0 - floor) * raw_ramp
            k = len(ramp)
            amp[:k] = np.minimum(amp[:k], ramp)
            amp[-k:] = np.minimum(amp[-k:], ramp[::-1])
        object.__setattr__(self, "_amp_taper", amp)

        pre_bits = _pn_chips(n_active, self.preamble_seed)
        pre_bpsk = (1.0 - 2.0 * pre_bits.astype(np.float64))
        object.__setattr__(self, "_preamble_bin_symbols",
                            (pre_bpsk * np.exp(1j * phases) * amp).astype(np.complex128))
        pilot_bits = _pn_chips(n_active, self.pilot_seed, taps=(1, 4))
        pilot_bpsk = (1.0 - 2.0 * pilot_bits.astype(np.float64))
        object.__setattr__(self, "_pilot_bin_symbols",
                            (pilot_bpsk * np.exp(1j * phases) * amp).astype(np.complex128))
        # comb pilot bin symbols reuse the pilot BPSK sequence (same n_active
        # length; only entries at comb positions are actually used)
        object.__setattr__(self, "_comb_bin_symbols",
                            (pilot_bpsk * np.exp(1j * phases) * amp).astype(np.complex128))
        object.__setattr__(self, "_phase_schedule", phases)

    @property
    def n_active(self) -> int:
        return len(self.active_bins)

    @property
    def n_data_bins(self) -> int:
        return int(np.sum(~self._comb_mask))

    @property
    def symbol_len(self) -> int:
        return self.fft_size + self.cp_len

    @property
    def bits_per_ofdm_symbol(self) -> int:
        return self.n_data_bins * self.bits_per_symbol

    @property
    def max_payload_bytes(self) -> int:
        return self.packet_bytes - LENGTH_BYTES - CRC_BYTES

    @property
    def data_bits(self) -> int:
        """Raw (uncoded) packet bit count: [length][payload][crc32]."""
        return self.packet_bytes * 8

    @property
    def n_codewords(self) -> int:
        if not self.fec_rate:
            return 0
        k = _ldpc.INFORMATION_BITS[self.fec_rate]
        return int(np.ceil(self.data_bits / k))

    @property
    def coded_bit_count(self) -> int:
        """Bits actually carried over the air per frame: equals data_bits
        when FEC is off, else n_codewords * 648 (LDPC codeword length)."""
        if not self.fec_rate:
            return self.data_bits
        return self.n_codewords * _ldpc.N

    @property
    def n_data_ofdm_symbols(self) -> int:
        n = self.coded_bit_count / self.bits_per_ofdm_symbol
        return int(np.ceil(n))

    @property
    def n_pilot_symbols(self) -> int:
        if self.pilot_interval <= 0:
            return 0
        return int(np.ceil(self.n_data_ofdm_symbols / self.pilot_interval))

    def _layout(self) -> list[tuple[str, int]]:
        if self.pilot_interval <= 0:
            return [("data", self.n_data_ofdm_symbols)] if self.n_data_ofdm_symbols else []
        segments: list[tuple[str, int]] = []
        remaining = self.n_data_ofdm_symbols
        while remaining > 0:
            chunk = min(self.pilot_interval, remaining)
            segments.append(("data", chunk))
            segments.append(("pilot", 1))
            remaining -= chunk
        return segments

    def total_ofdm_symbols(self) -> int:
        return self.n_preamble_symbols + self.n_data_ofdm_symbols + self.n_pilot_symbols

    def frame_seconds(self) -> float:
        return self.total_ofdm_symbols() * self.symbol_len / DESIGN_RATE

    def crest_factor_db(self) -> float:
        rng = np.random.default_rng(1234)
        bits = rng.integers(0, 2, self.bits_per_ofdm_symbol, dtype=np.uint8)
        syms = np.zeros(self.n_active, dtype=np.complex128)
        data_syms = bits_to_symbols(bits, self.bits_per_symbol)
        syms[self._data_idx] = data_syms
        syms[self._comb_idx] = self._comb_bin_symbols[self._comb_idx]
        syms = syms * np.exp(1j * self._phase_schedule) * self._amp_taper
        wave = self._ifft_symbol(syms)
        peak = np.max(np.abs(wave))
        rms = np.sqrt(np.mean(wave ** 2))
        return 20 * np.log10(peak / (rms + 1e-15))

    def _ifft_symbol(self, bin_symbols: np.ndarray) -> np.ndarray:
        spec = np.zeros(self.fft_size, dtype=np.complex128)
        for b, s in zip(self.active_bins, bin_symbols):
            spec[b] = s
            spec[self.fft_size - b] = np.conj(s)
        return np.real(np.fft.ifft(spec)) * self.fft_size

    def _fft_bins(self, time_symbol: np.ndarray) -> np.ndarray:
        spec = np.fft.fft(time_symbol) / self.fft_size
        return np.array([spec[b] for b in self.active_bins])

    def _add_cp(self, symbol: np.ndarray) -> np.ndarray:
        return np.concatenate([symbol[-self.cp_len:], symbol]) if self.cp_len else symbol

    # -- TX -------------------------------------------------------------------

    def _interleaver(self) -> np.ndarray:
        """Permutation applied to the coded bit stream before mapping.

        Without it each 648-bit codeword occupies a short, contiguous run
        of the frame -- at 97 data bins and 5 bits/symbol, barely more
        than one OFDM symbol -- so a transient that lasts a few symbols
        lands entirely inside one or two codewords and swamps them while
        every other codeword decodes in one or two iterations. That is
        exactly the failure seen on hardware at fft 480 / 32-QAM: a mean
        raw BER of 0.6%, well inside what rate 3/4 corrects, yet whole
        frames lost to a handful of adjacent non-convergent codewords.

        A fixed pseudo-random permutation over the whole coded stream
        spreads every codeword's bits across all subcarriers and the
        entire frame, so a burst is shared thinly among all codewords
        instead of destroying a few. It costs no airtime: the same bits
        are sent, in a different order. Both ends derive it from
        `coded_bit_count`, which is fixed by the mode's parameters."""
        n = self.coded_bit_count
        cached = self.__dict__.get("_interleaver_cache")
        if cached is not None and len(cached[0]) == n:
            return cached[0]
        perm = np.random.default_rng(INTERLEAVER_SEED).permutation(n)
        inverse = np.argsort(perm)
        object.__setattr__(self, "_interleaver_cache", (perm, inverse))
        return perm

    def _deinterleaver(self) -> np.ndarray:
        self._interleaver()
        return self.__dict__["_interleaver_cache"][1]

    def pack_and_encode_bits(self, payload: bytes) -> tuple[np.ndarray, np.ndarray]:
        """Returns (raw_packet_bits, coded_bits) exactly as `modulate()`
        generates them pre-whitening, so a test harness can independently
        reconstruct ground-truth coded bits for raw (pre-FEC) BER, without
        duplicating the LDPC framing logic."""
        packet = _pack_packet(payload, self.packet_bytes)
        raw_bits = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
        if not self.fec_rate:
            return raw_bits, raw_bits
        k = _ldpc.INFORMATION_BITS[self.fec_rate]
        n_cw = self.n_codewords
        info_padded = np.zeros(n_cw * k, dtype=np.uint8)
        info_padded[:len(raw_bits)] = raw_bits
        coded = np.concatenate([_ldpc.encode(row, self.fec_rate)
                                 for row in info_padded.reshape(n_cw, k)])
        return raw_bits, coded

    def modulate(self, payload: bytes) -> np.ndarray:
        _, coded_bits = self.pack_and_encode_bits(payload)
        coded_bits = coded_bits[self._interleaver()] if self.interleave else coded_bits
        whitener = _bits.pn_bits(len(coded_bits), WHITENER_SEED)
        data_bits = coded_bits ^ whitener

        needed_bits = self.n_data_ofdm_symbols * self.bits_per_ofdm_symbol
        if len(data_bits) < needed_bits:
            pad = np.zeros(needed_bits - len(data_bits), dtype=np.uint8)
            data_bits = np.concatenate([data_bits, pad])
        data_syms_flat = bits_to_symbols(data_bits, self.bits_per_symbol)
        data_syms = data_syms_flat.reshape(self.n_data_ofdm_symbols, self.n_data_bins)

        full_syms = np.zeros((self.n_data_ofdm_symbols, self.n_active), dtype=np.complex128)
        full_syms[:, self._data_idx] = data_syms
        full_syms[:, self._comb_idx] = self._comb_bin_symbols[self._comb_idx]
        full_syms = full_syms * (np.exp(1j * self._phase_schedule) * self._amp_taper)[None, :]

        pieces = []
        for _ in range(self.n_preamble_symbols):
            pieces.append(self._add_cp(self._ifft_symbol(self._preamble_bin_symbols)))

        cursor = 0
        for kind, count in self._layout():
            if kind == "data":
                for i in range(count):
                    pieces.append(self._add_cp(self._ifft_symbol(full_syms[cursor + i])))
                cursor += count
            else:
                pieces.append(self._add_cp(self._ifft_symbol(self._pilot_bin_symbols)))

        passband = np.concatenate(pieces)
        passband = passband / (np.max(np.abs(passband)) + 1e-12)
        passband = passband * self.drive_scale

        up = 4
        stuffed = np.zeros(len(passband) * up, dtype=np.float64)
        stuffed[::up] = passband
        lpf = _sc._design_interp_lpf(up)
        tx = np.convolve(stuffed, lpf, mode="same") * up
        # The interpolation filter can overshoot the pre-filter peak by a few
        # percent.  Normalize the actual DAC waveform so drive_scale=1.0 is
        # genuinely full-scale without asking the audio backend to clip.
        tx = tx / (np.max(np.abs(tx)) + 1e-12) * self.drive_scale
        return tx.astype(np.float32)

    # -- RX -------------------------------------------------------------------

    def _apply_phase_slope(self, gain: np.ndarray) -> tuple[np.ndarray, float]:
        """Fit gain = |gain| * exp(j*(a + b*bin_index)) across active bins
        (a common-across-bins group-delay/timing-offset term b), and return
        the gain array with that linear phase term removed on top of the
        original per-bin gain (i.e. residual phase noise only), plus the
        fitted slope in radians/bin for diagnostics. This targets a
        systematic linear phase ramp across the band (a timing offset /
        constant group delay) that a purely independent per-bin gain model
        represents but does not explicitly separate from bin-to-bin noise;
        making it explicit lets the fit be robust-averaged over many bins
        rather than relying on each bin's own noisy phase estimate.
        """
        idx = np.arange(len(gain), dtype=np.float64)
        phase = np.unwrap(np.angle(gain))
        # simple least squares fit phase ~ a + b*idx
        A = np.vstack([np.ones_like(idx), idx]).T
        coef, *_ = np.linalg.lstsq(A, phase, rcond=None)
        a, b = coef
        return gain, float(b)

    def demodulate(self, captured_12k: np.ndarray, *, diagnostics=False,
                   gain_smoothing=1, noise_estimator="legacy",
                   ldpc_max_iterations=30, refine_iterations=0,
                   freq_hint_hz=None) -> dict:
        """Decode; optional HF17 diagnostics and training-only receiver trials.

        gain_smoothing is an odd carrier-window width (default 1 disables).
        noise_estimator='repeat' uses repeated-preamble differences for LLR
        weighting and per-bin diagnostics. channel_snr_db retains its legacy
        meaning and must not be interpreted as calibrated RF SNR.
        refine_iterations>0 enables the decision-directed residual-gain passes
        documented at `_refine_decode`; it is receiver-only and needs FEC.
        """
        if gain_smoothing < 1 or gain_smoothing % 2 != 1:
            raise ValueError("gain_smoothing must be a positive odd width")
        if noise_estimator not in ("legacy", "repeat"):
            raise ValueError("unknown noise estimator")
        x = np.asarray(captured_12k, dtype=np.float64)
        result = {"synced": False, "crc_ok": False, "payload": None,
                  "confidence": 0.0, "freq_offset_hz": None,
                  "channel_snr_db": None, "raw_bits": None,
                  "raw_packet_bits": None, "phase_slope_rad_per_bin": None,
                  "pre_fec_bits": None, "ldpc_ok": None, "ldpc_iterations": None}

        one_preamble = self._add_cp(self._ifft_symbol(self._preamble_bin_symbols))
        preamble_wave = np.tile(one_preamble, self.n_preamble_symbols) \
            if self.n_preamble_symbols > 1 else one_preamble

        if len(x) < len(preamble_wave) + 10:
            return result

        norm = np.sqrt(np.sum(preamble_wave ** 2)) * (np.std(x) + 1e-12) * np.sqrt(len(preamble_wave))

        best = (-1.0, 0, 0.0)
        if freq_hint_hz is None:
            search_hz = np.arange(-SYNC_SEARCH_HZ, SYNC_SEARCH_HZ + 1e-9,
                                  SYNC_SEARCH_STEP_HZ)
        else:
            if not np.isfinite(freq_hint_hz):
                raise ValueError("frequency hint must be finite")
            hint = float(np.clip(freq_hint_hz, -SYNC_SEARCH_HZ, SYNC_SEARCH_HZ))
            search_hz = hint + np.arange(-SYNC_HINT_RADIUS_HZ,
                                         SYNC_HINT_RADIUS_HZ + 1e-9,
                                         SYNC_SEARCH_STEP_HZ)
            search_hz = np.clip(search_hz, -SYNC_SEARCH_HZ, SYNC_SEARCH_HZ)
            search_hz = np.unique(search_hz)
        for hz in search_hz:
            template = _freq_shift_real(preamble_wave, hz, DESIGN_RATE)
            corr = fftconvolve(x, template[::-1], mode="valid")
            env = np.abs(_sc._hilbert_envelope(corr))
            peak = int(np.argmax(env))
            conf = float(env[peak] / (norm + 1e-12))
            if conf > best[0]:
                best = (conf, peak, float(hz))

        confidence, start, freq_offset = best
        result["confidence"] = confidence
        result["freq_offset_hz"] = freq_offset

        total_symbols = self.total_ofdm_symbols()
        symlen = self.symbol_len
        needed = start + total_symbols * symlen
        if confidence < 0.12 or needed > len(x):
            return result
        result["synced"] = True
        # Keep the checked OFDM start available on the normal decode path; the
        # large diagnostics arrays remain opt-in below.
        result["start_sample"] = int(start)

        span = x[start:start + total_symbols * symlen + symlen]

        def _corrected(offset_hz: float) -> np.ndarray:
            return _freq_shift_real(span, -offset_hz, DESIGN_RATE)

        corrected = _corrected(freq_offset)

        def _symbol_bins(idx: int, sig: np.ndarray) -> np.ndarray:
            s = idx * symlen + self.cp_len
            seg = sig[s:s + self.fft_size]
            if len(seg) < self.fft_size:
                seg = np.concatenate([seg, np.zeros(self.fft_size - len(seg))])
            return self._fft_bins(seg)

        if self.n_preamble_symbols >= 2:
            pre_bins_seq = [_symbol_bins(i, corrected) for i in range(self.n_preamble_symbols)]
            dt = symlen / DESIGN_RATE
            rotations = []
            for i in range(1, self.n_preamble_symbols):
                ratio = pre_bins_seq[i] * np.conj(pre_bins_seq[i - 1])
                rotations.append(np.angle(np.sum(ratio)))
            mean_rot = float(np.mean(rotations))
            refine_hz = mean_rot / (2 * np.pi * dt)
            freq_offset += refine_hz
            result["freq_offset_hz"] = float(freq_offset)
            corrected = _corrected(freq_offset)

        pre_bins_seq = [_symbol_bins(i, corrected) for i in range(self.n_preamble_symbols)]
        pre_avg = np.mean(pre_bins_seq, axis=0)
        gain0 = pre_avg / self._preamble_bin_symbols
        if np.any(np.abs(gain0) < 1e-9):
            return result

        phase_slope = None
        if self.equalizer == "phase_slope":
            gain0, phase_slope = self._apply_phase_slope(gain0)
            result["phase_slope_rad_per_bin"] = phase_slope

        noise_powers = []
        sig_powers = []
        bin_noise_list = []  # per-bin residual power, for the LLR demapper
        bin_sig_list = []    # per-bin reference power, for per_bin_snr_db only
        for pb in pre_bins_seq:
            eq = pb / gain0
            resid = np.abs(eq - self._preamble_bin_symbols) ** 2
            noise_powers.append(np.mean(resid))
            sig_powers.append(np.mean(np.abs(self._preamble_bin_symbols) ** 2))
            bin_noise_list.append(resid)
            bin_sig_list.append(np.abs(self._preamble_bin_symbols) ** 2)

        anchors_idx = [self.n_preamble_symbols / 2.0 - 0.5]
        anchors_gain = [gain0]

        layout = self._layout()
        eq_data = np.empty((self.n_data_ofdm_symbols, self.n_active), dtype=np.complex128)
        data_gain = np.empty_like(eq_data)
        cursor = self.n_preamble_symbols
        data_cursor = 0
        pending: list[tuple[int, int, int]] = []
        for kind, count in layout:
            if kind == "data":
                pending.append((cursor, data_cursor, count))
                data_cursor += count
            else:
                pilot_bins = _symbol_bins(cursor, corrected)
                gain = pilot_bins / self._pilot_bin_symbols
                if self.equalizer == "phase_slope":
                    gain, _ = self._apply_phase_slope(gain)
                if np.any(np.abs(gain) < 1e-9):
                    return result
                anchors_idx.append(float(cursor))
                anchors_gain.append(gain)
                eq_pilot = pilot_bins / gain
                pilot_resid = np.abs(eq_pilot - self._pilot_bin_symbols) ** 2
                noise_powers.append(np.mean(pilot_resid))
                sig_powers.append(np.mean(np.abs(self._pilot_bin_symbols) ** 2))
                bin_noise_list.append(pilot_resid)
                bin_sig_list.append(np.abs(self._pilot_bin_symbols) ** 2)
            cursor += count

        anchors_idx = np.array(anchors_idx)
        anchors_gain = np.array(anchors_gain)
        if gain_smoothing > 1:
            # Local complex-gain average after removing the dominant delay
            # ramp. Uses training symbols only, never transmitted data.
            positions = np.asarray(self.active_bins, dtype=float)
            radius = int(gain_smoothing) // 2
            for row in range(len(anchors_gain)):
                g = anchors_gain[row]
                slope = np.polyfit(positions, np.unwrap(np.angle(g)), 1)[0]
                flat = g * np.exp(-1j * slope * positions)
                smooth = np.array([np.mean(flat[max(0,b-radius):b+radius+1])
                                   for b in range(self.n_active)])
                anchors_gain[row] = smooth * np.exp(1j * slope * positions)

        for start_sym, start_eq, count in pending:
            idx = start_sym + np.arange(count)
            gain_trace = np.empty((count, self.n_active), dtype=np.complex128)
            for b in range(self.n_active):
                re = np.interp(idx, anchors_idx, anchors_gain[:, b].real)
                im = np.interp(idx, anchors_idx, anchors_gain[:, b].imag)
                gain_trace[:, b] = re + 1j * im
            for i in range(count):
                bins = _symbol_bins(start_sym + i, corrected)
                gt = gain_trace[i]
                if self.n_comb() > 0 and self.comb_tracking in ("common", "confidence", "residual"):
                    ci = self._comb_idx
                    # TX multiplies the whole grid by phase_schedule/taper,
                    # including comb symbols which already contain that phase.
                    reference = (self._comb_bin_symbols[ci]
                                 * np.exp(1j*self._phase_schedule[ci])
                                 * self._amp_taper[ci])
                    predicted = gt[ci] * reference
                    common = np.vdot(predicted, bins[ci]) / max(
                        float(np.vdot(predicted, predicted).real), 1e-18)
                    if self.comb_tracking == "confidence":
                        # Pilot disagreement estimates uncertainty in the common
                        # complex correction. Shrink insignificant corrections
                        # toward unity (the block-training channel estimate).
                        error = bins[ci] - common * predicted
                        variance = float(np.vdot(error,error).real) / max(len(ci)-1,1)
                        variance /= max(float(np.vdot(predicted,predicted).real),1e-18)
                        change = abs(common-1)**2
                        trust = max(0.0, 1.0-variance/max(change,1e-18))
                        gt = gt * (1 + trust*(common-1))
                    elif self.comb_tracking == "common":
                        gt = gt * common
                    else:
                        residual = bins[ci] / predicted
                        positions = np.asarray(self.active_bins)
                        corr = (np.interp(positions, positions[ci], residual.real)
                                + 1j*np.interp(positions, positions[ci], residual.imag))
                        gt = gt * corr
                elif self.n_comb() > 0 and self.comb_tracking == "legacy":
                    # comb-pilot refinement: re-estimate gain at comb bin
                    # positions from this symbol's own known comb symbols,
                    # and blend/interpolate across frequency onto the data
                    # bins -- gives an extra per-symbol frequency-domain
                    # correction beyond the time-only interpolation above.
                    comb_gain_now = bins[self._comb_idx] / self._comb_bin_symbols[self._comb_idx]
                    comb_pos = self._comb_idx.astype(np.float64)
                    all_pos = np.arange(self.n_active, dtype=np.float64)
                    re_i = np.interp(all_pos, comb_pos, comb_gain_now.real)
                    im_i = np.interp(all_pos, comb_pos, comb_gain_now.imag)
                    comb_interp = re_i + 1j * im_i
                    # blend: trust comb (this symbol, this freq) 50/50 with
                    # the time-interpolated anchor gain
                    gt = 0.5 * gt + 0.5 * comb_interp
                eq_data[start_eq + i] = bins / gt
                data_gain[start_eq + i] = gt

        snr_db = 10 * np.log10(np.mean(sig_powers) / (np.mean(noise_powers) + 1e-15))
        result["channel_snr_db"] = float(snr_db)
        result["pilot_symbols"] = len(anchors_idx) - 1

        bin_noise_var = np.mean(bin_noise_list, axis=0)  # (n_active,)
        if noise_estimator == "repeat" and len(pre_bins_seq) >= 2:
            # Difference independent repetitions; do not fit a pilot to itself.
            differences = np.diff(np.asarray(pre_bins_seq), axis=0)
            raw_variance = np.mean(np.abs(differences)**2, axis=0) / 2
            raw_variance = np.array([np.mean(raw_variance[max(0,b-1):b+2])
                                     for b in range(self.n_active)])
            bin_noise_var = raw_variance / np.maximum(np.abs(gain0)**2, 1e-18)

        # Per-carrier post-equalization SNR, reported additively as a
        # diagnostic (hf14's hardware phase). bin_noise_list holds, for
        # every known-symbol OFDM symbol (preamble + time pilots), the
        # per-bin squared error of that symbol AFTER dividing by the gain
        # estimate, and bin_sig_list the matching per-bin reference power.
        # The ratio is therefore the SNR each subcarrier actually presents
        # to the slicer: a deeply faded bin shows up here because its gain
        # estimate is itself noisy, which the scalar channel_snr_db above
        # averages away. Nothing below consumes this -- channel_snr_db and
        # every decode path are unchanged.
        bin_sig_power = np.mean(bin_sig_list, axis=0)  # (n_active,)
        per_bin_snr_db = 10 * np.log10(bin_sig_power / (bin_noise_var + 1e-15))
        result["per_bin_snr_db"] = [float(v) for v in per_bin_snr_db]
        result["per_bin_snr_db_min"] = float(np.min(per_bin_snr_db))
        result["per_bin_snr_db_median"] = float(np.median(per_bin_snr_db))
        result["per_bin_snr_db_max"] = float(np.max(per_bin_snr_db))
        result["noise_estimator"] = noise_estimator

        data_syms_full = (eq_data * np.exp(-1j * self._phase_schedule)[None, :])
        data_syms_flat = data_syms_full[:, self._data_idx].reshape(-1)
        if diagnostics:
            result["equalized_symbols"] = data_syms_full[:, self._data_idx].copy()
            result["anchor_gain"] = anchors_gain.copy()
            result["anchor_index"] = anchors_idx.copy()

        # Hard-decision, pre-FEC bits in the coded-bit domain (== payload
        # domain when fec_rate is None): this is v5's original path,
        # preserved unconditionally so BER can always be reported even
        # when LDPC decode fails outright.
        coded_hard = symbols_to_bits(data_syms_flat, self.bits_per_symbol)
        coded_hard = coded_hard[: self.coded_bit_count] if len(coded_hard) > self.coded_bit_count else coded_hard
        whitener_coded = _bits.pn_bits(len(coded_hard), WHITENER_SEED)
        pre_fec_bits = coded_hard ^ whitener_coded
        if self.interleave and len(pre_fec_bits) == self.coded_bit_count:
            # back to transmit (codeword) order, so raw BER stays
            # comparable with pack_and_encode_bits()' ground truth
            pre_fec_bits = pre_fec_bits[self._deinterleaver()]
        result["pre_fec_bits"] = pre_fec_bits.copy()

        if self.fec_rate:
            if noise_estimator == "repeat" and len(pre_bins_seq) >= 2:
                # The repeat estimator measures variance in raw FFT-bin units.
                # Convert each value with the same final gain used to equalize
                # that data symbol, including any per-symbol comb correction.
                # Selecting data bins before row-major flattening preserves the
                # exact symbol order used by data_syms_flat.
                variance = raw_variance[None, :] / np.maximum(
                    np.abs(data_gain) ** 2, 1e-18)
                data_bin_noise = variance[:, self._data_idx].reshape(-1)
            else:
                data_bin_noise = np.tile(
                    bin_noise_var[self._data_idx], self.n_data_ofdm_symbols)
            info, iterations, oks = self._decode_blocks(
                data_syms_flat, data_bin_noise, ldpc_max_iterations)
            if refine_iterations:
                info, iterations, oks, refinement = self._refine_decode(
                    data_syms_flat, data_bin_noise, info, oks, iterations,
                    ldpc_max_iterations, refine_iterations)
                result.update(refinement)
            result["ldpc_ok"] = bool(np.all(oks))
            result["ldpc_codeword_ok"] = np.atleast_1d(oks).astype(bool).tolist()
            result["ldpc_iterations"] = [int(i) for i in np.atleast_1d(iterations)]
            raw_bits = info.reshape(-1)[: self.data_bits]
        else:
            raw_bits = pre_fec_bits[: self.data_bits] if len(pre_fec_bits) > self.data_bits else pre_fec_bits

        result["raw_packet_bits"] = raw_bits.copy()
        packet = np.packbits(raw_bits).tobytes()
        payload, meta = _unpack_packet(packet, self.max_payload_bytes)
        result.update(meta)
        result["payload"] = payload
        # raw_bits exposed as the (post-FEC-decode, when applicable)
        # payload-region bit array; harness re-slices with ground truth for
        # BER anyway. pre_fec_bits carries the raw hard-decision, coded-
        # domain bits for raw (uncoded) BER even when FEC is enabled.
        result["raw_bits"] = raw_bits
        return result

    # -- decision-directed refinement -----------------------------------------
    #
    # The block equalizer estimates one complex gain per carrier from the
    # preamble and interpolates it between time pilots.  Two things it cannot
    # see are measurable on this bench and cost real margin at 32-QAM:
    #
    #   * the receiver AGC moves between pilots.  Measured against known
    #     payloads over the IC-705/IC-7300 pair, the residual per-symbol
    #     amplitude wanders 0.82-1.18 with the IC-7300 on AGC-SLOW and
    #     0.70-1.54 on AGC-FAST -- interpolation over a 20-symbol pilot
    #     spacing cannot follow it.
    #   * the preamble's own per-carrier gain estimate has an error floor of
    #     its own, which sits under every data symbol in the frame.
    #
    # Removing both against the known payload was worth 1.5 dB and 2.8 dB of
    # data EVM respectively on those captures.  Neither needs the payload:
    # every LDPC codeword that decodes and passes its own parity check is a
    # block of *certain* transmitted bits, and the interleaver spreads each
    # codeword across the whole frame, so even a partial first pass hands back
    # references at every symbol and every carrier.  Re-encoding those
    # codewords rebuilds their transmitted symbols exactly; fitting the
    # residual gain against them and decoding again picks up the codewords
    # that first missed.
    #
    # This is receiver-only.  It changes nothing on the air and a frame that
    # already decoded takes the early exit below, so it can only turn failures
    # into successes -- the refined pass is kept only when it fixes strictly
    # more codewords than the pass before it.

    MIN_REFERENCES = 8   # per symbol or carrier, below which the fit is noise

    def _decode_blocks(self, syms_flat, bin_noise, max_iterations):
        """Symbols -> LLRs -> de-whiten -> de-interleave -> LDPC."""
        llrs = _soft_bit_llrs(syms_flat, self.bits_per_symbol, bin_noise)
        llrs = llrs[: self.coded_bit_count] if len(llrs) > self.coded_bit_count else llrs
        whitener_llr = _bits.pn_bits(len(llrs), WHITENER_SEED)
        # whitening is XOR with a known bit; flipping a bit flips the
        # sign of its LLR (positive LLR == bit zero), so this is
        # equivalent to de-whitening the soft channel output.
        llrs = np.where(whitener_llr == 1, -llrs, llrs)
        if self.interleave and len(llrs) == self.coded_bit_count:
            llrs = llrs[self._deinterleaver()]
        n_cw = self.n_codewords
        pad = n_cw * _ldpc.N - len(llrs)
        if pad > 0:
            llrs = np.concatenate([llrs, np.zeros(pad)])
        blocks = llrs[:n_cw * _ldpc.N].reshape(n_cw, _ldpc.N)
        return _ldpc.decode_batch(blocks, max_iterations=max_iterations,
                                  rate=self.fec_rate)

    def _reference_symbols(self, info, oks):
        """Transmitted symbols and a known-mask, rebuilt from the codewords
        that decoded.  Mirrors modulate()'s encode -> interleave -> whiten ->
        map chain exactly, so a reference is the symbol that was sent."""
        n_cw = self.n_codewords
        coded = np.zeros(n_cw * _ldpc.N, dtype=np.uint8)
        known = np.zeros(n_cw * _ldpc.N, dtype=bool)
        info = np.atleast_2d(info)
        for i, ok in enumerate(np.atleast_1d(oks)):
            if ok:
                coded[i * _ldpc.N:(i + 1) * _ldpc.N] = _ldpc.encode(
                    info[i], rate=self.fec_rate)
                known[i * _ldpc.N:(i + 1) * _ldpc.N] = True
        coded = coded[: self.coded_bit_count]
        known = known[: self.coded_bit_count]
        if self.interleave:
            order = self._interleaver()
            coded, known = coded[order], known[order]
        whitener = _bits.pn_bits(len(coded), WHITENER_SEED)
        bits = coded ^ whitener

        needed = self.n_data_ofdm_symbols * self.bits_per_ofdm_symbol
        if len(bits) < needed:
            # modulate() pads with zeros, which are as known as any other bit.
            bits = np.concatenate([bits, np.zeros(needed - len(bits), np.uint8)])
            known = np.concatenate([known, np.ones(needed - len(known), bool)])
        bits, known = bits[:needed], known[:needed]
        syms = bits_to_symbols(bits, self.bits_per_symbol).reshape(
            self.n_data_ofdm_symbols, self.n_data_bins)
        # A symbol is usable only when every bit that maps into it is known.
        sym_known = known.reshape(-1, self.bits_per_symbol).all(axis=1).reshape(
            self.n_data_ofdm_symbols, self.n_data_bins)
        return syms, sym_known

    def _residual_gain(self, got, ref, known):
        """Least-squares per-symbol then per-carrier residual complex gain,
        each fitted only where enough references exist and held at unity
        elsewhere."""
        def fit(axis):
            weight = np.where(known, np.abs(ref) ** 2, 0.0).sum(axis=axis)
            product = np.where(known, got * np.conj(ref), 0.0).sum(axis=axis)
            counts = known.sum(axis=axis)
            gain = np.divide(product, weight, out=np.ones_like(product),
                             where=weight > 1e-18)
            return np.where(counts >= self.MIN_REFERENCES, gain, 1.0)

        per_symbol = fit(1)[:, None]
        per_carrier = fit(0)[None, :]
        return per_symbol, per_carrier

    def _residual_noise(self, corrected, ref, known, fallback):
        """Separable per-symbol x per-carrier noise variance measured from the
        references themselves.

        The variance the first pass hands the demapper comes from the preamble,
        and on this bench the data symbols carry roughly 8 dB more error than
        the preamble does -- guard leakage and AGC movement, neither of which
        touches a repeated constant preamble symbol.  A uniform scale error
        would not matter, since the LDPC decoder normalizes it away, but the
        *shape* does: a symbol the AGC was moving through, or a carrier at the
        band edge, deserves less weight than the preamble fit gives it.
        """
        error = np.where(known, np.abs(corrected - ref) ** 2, 0.0)
        total = float(error.sum())
        references = int(known.sum())
        if references < self.MIN_REFERENCES or total <= 0:
            return fallback
        overall = total / references

        def profile(axis):
            counts = known.sum(axis=axis)
            usable = counts >= self.MIN_REFERENCES
            if not np.any(usable):
                return None
            mean = np.divide(error.sum(axis=axis), counts,
                             out=np.full(counts.shape, overall, float),
                             where=usable)
            # A row or column with too few references keeps the frame-wide
            # variance rather than a fit nobody can stand behind.
            return np.where(usable, mean, overall)

        per_symbol = profile(1)
        per_carrier = profile(0)
        if per_symbol is None or per_carrier is None:
            return fallback
        return np.maximum(
            per_symbol[:, None] * per_carrier[None, :] / overall, 1e-18)

    def _refine_decode(self, syms_flat, bin_noise, info, oks, iterations,
                       max_iterations, passes):
        """Re-fit the residual gain against the codewords that decoded and
        decode again, keeping a pass only when it recovers more codewords."""
        shape = (self.n_data_ofdm_symbols, self.n_data_bins)
        syms = np.asarray(syms_flat)[: shape[0] * shape[1]].reshape(shape)
        noise = np.asarray(bin_noise)[: shape[0] * shape[1]].reshape(shape)
        diagnostics = {"refine_passes_run": 0,
                       "refine_codewords_ok": [int(np.sum(oks))]}
        best = (info, iterations, oks)
        for _ in range(int(passes)):
            ok_count = int(np.sum(best[2]))
            if ok_count == self.n_codewords or ok_count == 0:
                # Nothing left to fix, or nothing trustworthy to fit against.
                break
            ref, known = self._reference_symbols(best[0], best[2])
            per_symbol, per_carrier = self._residual_gain(syms, ref, known)
            gain = per_symbol * per_carrier
            corrected = syms / gain
            # Dividing the symbols by the gain divides their noise by its
            # squared magnitude; the LLR scaling has to follow.
            variance = self._residual_noise(
                corrected, ref, known, noise / np.abs(gain) ** 2)
            candidate = self._decode_blocks(
                corrected.reshape(-1), variance.reshape(-1), max_iterations)
            diagnostics["refine_passes_run"] += 1
            diagnostics["refine_codewords_ok"].append(int(np.sum(candidate[2])))
            if int(np.sum(candidate[2])) <= ok_count:
                break
            best = candidate
        return best[0], best[1], best[2], diagnostics

    def n_comb(self) -> int:
        return int(np.sum(self._comb_mask))
