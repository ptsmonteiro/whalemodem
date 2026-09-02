"""Single-carrier PHY with optional LDPC FEC, built on top of the v1
baseline (`experiments/hf5_8psk_4k/sc.py`, read-only reference).

Reuses sc.py's carrier, RRC pulse shaping, PN preamble, mid-frame pilot
tracking (linear complex-gain interpolation), and symbol mapping tables
verbatim (imported, not copied) -- everything below is additive: an
optional LDPC codec stage (IEEE 802.11n QC-LDPC, `experiments/qpsk29/ldpc.py`,
also read-only) inserted between the whitened packet bitstream and the
symbol mapper, plus an optional block interleaver to spread each LDPC
codeword's bits across the whole frame in time (the ALC/compression
corruption this channel shows on high-order constellations is expected to
be time-local/bursty rather than i.i.d., so LDPC -- which assumes
roughly-independent bit errors -- needs bits from one codeword spread out
in time to have a fair chance; see RESULTS.md).

fec_rate: None | "1/2" | "2/3" | "3/4". When None this mode is numerically
identical to sc.SingleCarrierMode (same preamble, same pilot mechanic).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from whale.dsp import bits as _bits
from experiments.hf5_8psk_4k import sc as _sc
from experiments.qpsk29 import ldpc as _ldpc

# Re-exported constants (identical to sc.py; kept as names here so
# hardware_test.py can refer to one module).
TX_SAMPLE_RATE = _sc.TX_SAMPLE_RATE
RX_SAMPLE_RATE = _sc.RX_SAMPLE_RATE
DESIGN_RATE = _sc.DESIGN_RATE
CARRIER_HZ = _sc.CARRIER_HZ
LENGTH_BYTES = _sc.LENGTH_BYTES
CRC_BYTES = _sc.CRC_BYTES
WHITENER_SEED = _sc.WHITENER_SEED
PREAMBLE_CHIPS = _sc.PREAMBLE_CHIPS
PREAMBLE_SYMBOLS = _sc.PREAMBLE_SYMBOLS
PILOT_LEN = _sc.PILOT_LEN
PILOT_SYMBOLS = _sc.PILOT_SYMBOLS

bits_to_symbols = _sc.bits_to_symbols
symbols_to_bits = _sc.symbols_to_bits
rrc_taps = _sc.rrc_taps
_pilot_layout = _sc._pilot_layout
_pack_packet = _sc._pack_packet
_unpack_packet = _sc._unpack_packet
_design_interp_lpf = _sc._design_interp_lpf
_hilbert_envelope = _sc._hilbert_envelope

SYNC_SEARCH_HZ = _sc.SYNC_SEARCH_HZ
SYNC_SEARCH_STEP_HZ = _sc.SYNC_SEARCH_STEP_HZ


# ---------------------------------------------------------------------------
# Generic soft-bit LLR demapper (adapted from experiments/hf10_ofdm49_v6's
# _soft_bit_llrs / _constellation_table pattern -- max-log-MAP distance over
# the brute-forced constellation table, positive LLR = bit zero, matching
# ldpc.py's convention).
# ---------------------------------------------------------------------------

_CONSTELLATION_CACHE: dict[int, tuple[np.ndarray, np.ndarray]] = {}


def _constellation_table(bps: int) -> tuple[np.ndarray, np.ndarray]:
    cached = _CONSTELLATION_CACHE.get(bps)
    if cached is not None:
        return cached
    m = 1 << bps
    idx = np.arange(m)
    tbl_bits = ((idx[:, None] >> np.arange(bps - 1, -1, -1)) & 1).astype(np.uint8)
    syms = bits_to_symbols(tbl_bits.reshape(-1), bps)
    _CONSTELLATION_CACHE[bps] = (syms, tbl_bits)
    return syms, tbl_bits


def _soft_bit_llrs(rx_syms: np.ndarray, bps: int, noise_var: float) -> np.ndarray:
    rx_syms = np.asarray(rx_syms)
    syms_table, bits_table = _constellation_table(bps)
    nv = max(float(noise_var), 1e-9)
    d2 = np.abs(rx_syms[:, None] - syms_table[None, :]) ** 2 / nv
    llrs = np.empty((rx_syms.shape[0], bps), dtype=np.float64)
    for j in range(bps):
        is0 = bits_table[:, j] == 0
        llrs[:, j] = np.min(d2[:, ~is0], axis=1) - np.min(d2[:, is0], axis=1)
    return llrs.reshape(-1)


# ---------------------------------------------------------------------------
# Block interleaver: spreads each codeword's N=648 coded bits evenly across
# the whole coded-bit stream (a rectangular write-rows/read-columns
# interleaver over the full frame), so a time-local burst of symbol errors
# (e.g. ALC/compression transients) hits only a few bits of any one
# codeword instead of a contiguous run within it.
# ---------------------------------------------------------------------------

def _interleave(bits: np.ndarray, n_rows: int) -> np.ndarray:
    n = len(bits)
    n_cols = -(-n // n_rows)  # ceil
    padded = np.zeros(n_rows * n_cols, dtype=bits.dtype)
    padded[:n] = bits
    grid = padded.reshape(n_rows, n_cols)
    return grid.T.reshape(-1)[: n_rows * n_cols]


def _deinterleave(bits: np.ndarray, n_rows: int, n_valid: int) -> np.ndarray:
    n_cols = -(-n_valid // n_rows)
    grid = bits[: n_rows * n_cols].reshape(n_cols, n_rows).T
    return grid.reshape(-1)[:n_valid]


def _deinterleave_soft(llrs: np.ndarray, n_rows: int, n_valid: int) -> np.ndarray:
    return _deinterleave(llrs, n_rows, n_valid)


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SingleCarrierFecMode:
    baud: float
    bits_per_symbol: int
    packet_bytes: int
    beta: float = 0.35
    span_symbols: int = 6
    pilot_interval: int = 150
    fec_rate: str | None = None   # None | "1/2" | "2/3" | "3/4"
    interleave: bool = True
    ldpc_max_iterations: int = 30

    @property
    def sps(self) -> int:
        sps = DESIGN_RATE / self.baud
        if abs(sps - round(sps)) > 1e-6:
            raise ValueError(f"baud {self.baud} does not divide {DESIGN_RATE} evenly")
        return int(round(sps))

    @property
    def max_payload_bytes(self) -> int:
        return self.packet_bytes - LENGTH_BYTES - CRC_BYTES

    @property
    def data_bits(self) -> int:
        return self.packet_bytes * 8

    @property
    def n_codewords(self) -> int:
        if not self.fec_rate:
            return 0
        k = _ldpc.INFORMATION_BITS[self.fec_rate]
        return -(-self.data_bits // k)

    @property
    def coded_bits(self) -> int:
        if not self.fec_rate:
            return self.data_bits
        return self.n_codewords * _ldpc.N

    @property
    def data_symbols(self) -> int:
        n = self.coded_bits / self.bits_per_symbol
        return int(-(-self.coded_bits // self.bits_per_symbol)) if abs(n - round(n)) > 1e-9 else int(round(n))

    @property
    def n_pilot_blocks(self) -> int:
        return sum(1 for kind, _ in _pilot_layout(self.data_symbols, self.pilot_interval)
                   if kind == "pilot")

    def frame_seconds(self) -> float:
        total_symbols = (PREAMBLE_CHIPS + self.data_symbols
                          + self.n_pilot_blocks * PILOT_LEN)
        return total_symbols / self.baud

    def net_bps(self) -> float:
        return (self.max_payload_bytes * 8) / self.frame_seconds()

    # -- bit-domain coding (whitening + optional LDPC + optional interleave)

    def _encode_bits(self, raw_bits: np.ndarray) -> np.ndarray:
        whitener = _bits.pn_bits(len(raw_bits), WHITENER_SEED)
        whitened = raw_bits ^ whitener
        if not self.fec_rate:
            return whitened
        k = _ldpc.INFORMATION_BITS[self.fec_rate]
        n_cw = self.n_codewords
        padded = np.zeros(n_cw * k, dtype=np.uint8)
        padded[: len(whitened)] = whitened
        blocks = padded.reshape(n_cw, k)
        coded = np.concatenate([_ldpc.encode(row, self.fec_rate) for row in blocks])
        if self.interleave:
            coded = _interleave(coded, n_cw)
        # pad up to a whole number of symbols
        pad = (-len(coded)) % self.bits_per_symbol
        if pad:
            coded = np.concatenate([coded, np.zeros(pad, dtype=np.uint8)])
        return coded

    def _decode_bits(self, coded_llrs_or_bits, *, soft: bool):
        """Returns (info_bits_uint8, meta) where meta carries LDPC iteration
        counts / per-codeword convergence, or (None, meta) if FEC is off (in
        which case the input is already the raw whitened hard bits)."""
        if not self.fec_rate:
            whitener = _bits.pn_bits(len(coded_llrs_or_bits), WHITENER_SEED)
            return (np.asarray(coded_llrs_or_bits, dtype=np.uint8) ^ whitener), {}
        k = _ldpc.INFORMATION_BITS[self.fec_rate]
        n_cw = self.n_codewords
        n_coded_valid = n_cw * _ldpc.N
        arr = np.asarray(coded_llrs_or_bits)
        if len(arr) < n_coded_valid and not self.interleave:
            arr = np.concatenate([arr, np.zeros(n_coded_valid - len(arr))])
        if self.interleave:
            arr = _deinterleave(arr, n_cw, n_coded_valid) if not soft else \
                  _deinterleave_soft(arr, n_cw, n_coded_valid)
        else:
            arr = arr[:n_coded_valid]
        blocks = arr.reshape(n_cw, _ldpc.N)
        if soft:
            info, iterations, oks = _ldpc.decode_batch(
                blocks.astype(np.float64), max_iterations=self.ldpc_max_iterations,
                rate=self.fec_rate)
        else:
            # Hard-bit path unused in practice (soft LLRs always available at
            # RX) but kept for completeness/testing.
            llrs = np.where(blocks == 0, 10.0, -10.0)
            info, iterations, oks = _ldpc.decode_batch(
                llrs, max_iterations=self.ldpc_max_iterations, rate=self.fec_rate)
        info_bits = info.reshape(-1)[: self.data_bits]
        whitener = _bits.pn_bits(len(info_bits), WHITENER_SEED)
        raw_bits = info_bits.astype(np.uint8) ^ whitener
        meta = {"ldpc_iterations": [int(i) for i in iterations],
                "ldpc_codeword_ok": [bool(o) for o in oks],
                "ldpc_codewords_ok": int(np.sum(oks)),
                "ldpc_codewords_total": int(n_cw),
                "ldpc_ok": bool(np.all(oks))}
        return raw_bits, meta

    def pack_and_encode_bits(self, payload: bytes) -> tuple[np.ndarray, np.ndarray]:
        """(raw pre-whitening packed-packet bits -- same domain
        `_decode_bits` reconstructs into `result["raw_bits"]` -- and the
        fully coded+interleaved bit stream as transmitted) for ground-truth
        BER comparisons in hardware_test.py."""
        packet = _pack_packet(payload, self.packet_bytes)
        raw_bits = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
        return raw_bits, self._encode_bits(raw_bits)

    # -- TX -----------------------------------------------------------------

    def modulate(self, payload: bytes) -> np.ndarray:
        packet = _pack_packet(payload, self.packet_bytes)
        raw_bits = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
        coded_bits = self._encode_bits(raw_bits)
        data_symbols = bits_to_symbols(coded_bits, self.bits_per_symbol)
        # data_symbols length should equal self.data_symbols; pad if the
        # coded-bit padding above rounded up.
        if len(data_symbols) < self.data_symbols:
            data_symbols = np.concatenate([
                data_symbols,
                np.zeros(self.data_symbols - len(data_symbols), dtype=np.complex128)])

        pieces = [PREAMBLE_SYMBOLS.astype(np.complex128)]
        cursor = 0
        for kind, count in _pilot_layout(self.data_symbols, self.pilot_interval):
            if kind == "data":
                pieces.append(data_symbols[cursor:cursor + count])
                cursor += count
            else:
                pieces.append(PILOT_SYMBOLS.astype(np.complex128))
        symbols = np.concatenate(pieces)

        sps = self.sps
        taps = rrc_taps(sps, self.span_symbols, self.beta)
        upsampled = np.zeros(len(symbols) * sps, dtype=np.complex128)
        upsampled[::sps] = symbols
        shaped = np.convolve(upsampled, taps, mode="full")

        n = np.arange(len(shaped))
        carrier = np.exp(1j * 2 * np.pi * CARRIER_HZ * n / DESIGN_RATE)
        passband = np.real(shaped * carrier)
        passband /= (np.max(np.abs(passband)) + 1e-12)
        passband *= 0.5

        up = 4
        stuffed = np.zeros(len(passband) * up, dtype=np.float64)
        stuffed[::up] = passband
        lpf = _design_interp_lpf(up)
        tx = np.convolve(stuffed, lpf, mode="same") * up
        return tx.astype(np.float32)

    # -- RX -------------------------------------------------------------

    def demodulate(self, captured_12k: np.ndarray) -> dict:
        x = np.asarray(captured_12k, dtype=np.float64)
        result = {"synced": False, "crc_ok": False, "payload": None,
                  "confidence": 0.0, "freq_offset_hz": None,
                  "channel_snr_db": None, "raw_bits": None}
        sps = self.sps
        taps = rrc_taps(sps, self.span_symbols, self.beta)

        pre_up = np.zeros(PREAMBLE_CHIPS * sps, dtype=np.complex128)
        pre_up[::sps] = PREAMBLE_SYMBOLS
        pre_shaped = np.convolve(pre_up, taps, mode="full")
        n = np.arange(len(pre_shaped))

        if len(x) < len(pre_shaped) + 10:
            return result
        norm = np.sqrt(np.sum(np.abs(pre_shaped) ** 2)) * (np.std(x) + 1e-12) * np.sqrt(len(pre_shaped))

        best = (-1.0, 0, 0.0)
        for hz in np.arange(-SYNC_SEARCH_HZ, SYNC_SEARCH_HZ + 1e-9, SYNC_SEARCH_STEP_HZ):
            pre_carrier = np.exp(1j * 2 * np.pi * (CARRIER_HZ + hz) * n / DESIGN_RATE)
            pre_passband = np.real(pre_shaped * pre_carrier)
            corr = np.correlate(x, pre_passband, mode="valid")
            env = np.abs(_hilbert_envelope(corr))
            peak = int(np.argmax(env))
            conf = float(env[peak] / (norm + 1e-12))
            if conf > best[0]:
                best = (conf, peak, float(hz))

        confidence, start, freq_offset = best
        result["confidence"] = confidence
        result["freq_offset_hz"] = freq_offset

        layout = _pilot_layout(self.data_symbols, self.pilot_interval)
        total_symbols = PREAMBLE_CHIPS + self.data_symbols + self.n_pilot_blocks * PILOT_LEN

        needed = start + int(total_symbols * sps)
        if confidence < 0.12 or needed > len(x):
            return result
        result["synced"] = True

        span = x[start:start + int((total_symbols + self.span_symbols * 2) * sps)]
        sample0 = (len(taps) - 1)

        def _matched_filter_symbols(offset_hz: float) -> np.ndarray:
            nn = np.arange(len(span))
            mix = np.exp(-1j * 2 * np.pi * (CARRIER_HZ + offset_hz) * nn / DESIGN_RATE)
            baseband = span * mix * 2.0
            filtered = np.convolve(baseband, taps, mode="full")
            idx = sample0 + sps * np.arange(total_symbols)
            idx = idx[idx < len(filtered)]
            return filtered[idx]

        symbols = _matched_filter_symbols(freq_offset)
        if len(symbols) < total_symbols:
            result["synced"] = False
            return result
        pre_rx0 = symbols[:PREAMBLE_CHIPS]
        derot = pre_rx0 * np.conj(PREAMBLE_SYMBOLS)
        phase = np.unwrap(np.angle(derot))
        t = np.arange(PREAMBLE_CHIPS) * sps / DESIGN_RATE
        slope = np.polyfit(t, phase, 1)[0]
        refine_hz = slope / (2 * np.pi)
        freq_offset += refine_hz
        result["freq_offset_hz"] = float(freq_offset)

        symbols = _matched_filter_symbols(freq_offset)
        if len(symbols) < total_symbols:
            result["synced"] = False
            return result

        pre_rx = symbols[:PREAMBLE_CHIPS]
        pre_gain = np.sum(pre_rx * np.conj(PREAMBLE_SYMBOLS)) / np.sum(np.abs(PREAMBLE_SYMBOLS) ** 2)
        if np.abs(pre_gain) < 1e-9:
            return result
        anchors = [(PREAMBLE_CHIPS / 2.0, pre_gain)]

        noise_powers = [np.mean(np.abs(pre_rx / pre_gain - PREAMBLE_SYMBOLS) ** 2)]
        sig_powers = [np.mean(np.abs(PREAMBLE_SYMBOLS) ** 2)]

        eq_data = np.empty(self.data_symbols, dtype=np.complex128)
        cursor = PREAMBLE_CHIPS
        data_cursor = 0
        pending_data: list[tuple[int, int, int]] = []
        for kind, count in layout:
            if kind == "data":
                pending_data.append((cursor, data_cursor, count))
                data_cursor += count
            else:
                pilot_rx = symbols[cursor:cursor + count]
                gain = np.sum(pilot_rx * np.conj(PILOT_SYMBOLS)) / np.sum(np.abs(PILOT_SYMBOLS) ** 2)
                if np.abs(gain) < 1e-9:
                    return result
                anchors.append((cursor + count / 2.0, gain))
                noise_powers.append(np.mean(np.abs(pilot_rx / gain - PILOT_SYMBOLS) ** 2))
                sig_powers.append(np.mean(np.abs(PILOT_SYMBOLS) ** 2))
            cursor += count

        anchor_idx = np.array([a[0] for a in anchors])
        anchor_gain = np.array([a[1] for a in anchors])
        for start_in_symbols, start_in_eq, count in pending_data:
            idx = start_in_symbols + np.arange(count)
            re = np.interp(idx, anchor_idx, anchor_gain.real)
            im = np.interp(idx, anchor_idx, anchor_gain.imag)
            gain_trace = re + 1j * im
            chunk_rx = symbols[start_in_symbols:start_in_symbols + count]
            eq_data[start_in_eq:start_in_eq + count] = chunk_rx / gain_trace

        noise_var = float(np.mean(noise_powers))
        snr_db = 10 * np.log10(np.mean(sig_powers) / (noise_var + 1e-15))
        result["channel_snr_db"] = float(snr_db)
        result["pilot_blocks"] = len(anchors) - 1

        # Raw (pre-FEC) hard-decision bits, for BER reporting -- always
        # computable regardless of whether FEC is on.
        raw_hard_bits = symbols_to_bits(eq_data, self.bits_per_symbol)
        result["pre_fec_bits"] = raw_hard_bits

        if not self.fec_rate:
            packet_bits, _ = self._decode_bits(raw_hard_bits, soft=False)
            result["raw_bits"] = packet_bits
            packet = np.packbits(packet_bits).tobytes()
            payload, meta = _unpack_packet(packet, self.max_payload_bytes)
            result.update(meta)
            result["payload"] = payload
            return result

        llrs = _soft_bit_llrs(eq_data, self.bits_per_symbol, noise_var)
        packet_bits, ldpc_meta = self._decode_bits(llrs, soft=True)
        result.update(ldpc_meta)
        result["raw_bits"] = packet_bits
        packet = np.packbits(packet_bits).tobytes()
        payload, meta = _unpack_packet(packet, self.max_payload_bytes)
        result.update(meta)
        result["payload"] = payload
        return result
