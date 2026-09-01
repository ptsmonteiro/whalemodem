"""49-subcarrier (and configurable) true-OFDM PHY for the IC-7300 ->
IC-705 audio-coupled path, extending experiments/hf7_ofdm_v3/ofdm.py.

Context / lineage: hf7's ofdm.py found a hard wall going from 6 to 7
active orthogonal subcarriers (0/5, complete failure despite adequate
average SNR), diagnosed as inter-carrier interference (ICI) from the
channel's own frequency-dependent group delay / dispersion, which its
gain-only per-subcarrier equalizer cannot see or correct. This module
is v3's ofdm.py verbatim, PLUS:

  1. A raw pre-CRC recovered-bit path (`raw_bits` / `raw_packet_bits` in
     the demodulate() result) so a test harness can compute BER on
     partial/failed decodes, not just full CRC-pass decodes.
  2. An optional richer equalizer (`equalizer="phase_slope"`) that fits
     a linear phase-vs-bin-index term (a single group-delay/timing-
     offset parameter) on top of the existing per-bin complex gain
     model, on the theory that a large part of "dispersion the
     gain-only equalizer can't see" is actually just a residual sample-
     timing/group-delay error common across bins -- which one extra
     scalar parameter per anchor can remove, unlike N independent gains.
  3. Optional comb-type (frequency-domain) pilot subcarriers
     (`pilot_comb_stride`): a subset of active bins carry a known
     symbol in *every* OFDM symbol (data and pilot alike), giving a
     per-symbol, per-(pilot)-frequency gain sample that is interpolated
     across neighbouring data bins -- catching drift *within* a frame
     at frequencies the whole-symbol time-domain pilots (v3's only
     mechanism) cannot resolve any faster than pilot_interval symbols.
  4. Optional edge guard-band trimming/tapering
     (`edge_guard_bins`, `edge_taper`) to test whether outer-bin
     weakness alone explains part of any 49-bin failure.

None of hf5/hf6/hf7/hf8/path_probe are modified; this is a fresh copy
in this experiment's own directory per the task's constraints.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from whale.dsp import bits as _bits
from experiments.hf5_8psk_4k import sc as _sc

TX_SAMPLE_RATE = _sc.TX_SAMPLE_RATE
RX_SAMPLE_RATE = _sc.RX_SAMPLE_RATE
DESIGN_RATE = RX_SAMPLE_RATE

BAND_LO_HZ = 300.0
BAND_HI_HZ = 2700.0

LENGTH_BYTES = _sc.LENGTH_BYTES
CRC_BYTES = _sc.CRC_BYTES
WHITENER_SEED = 0xBEEF17

SYNC_SEARCH_HZ = 20.0
SYNC_SEARCH_STEP_HZ = 1.0

bits_to_symbols = _sc.bits_to_symbols
symbols_to_bits = _sc.symbols_to_bits
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

    def __post_init__(self):
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
        return self.packet_bytes * 8

    @property
    def n_data_ofdm_symbols(self) -> int:
        n = self.data_bits / self.bits_per_ofdm_symbol
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

    def modulate(self, payload: bytes) -> np.ndarray:
        packet = _pack_packet(payload, self.packet_bytes)
        raw_bits = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
        whitener = _bits.pn_bits(len(raw_bits), WHITENER_SEED)
        data_bits = raw_bits ^ whitener

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

    def demodulate(self, captured_12k: np.ndarray) -> dict:
        x = np.asarray(captured_12k, dtype=np.float64)
        result = {"synced": False, "crc_ok": False, "payload": None,
                  "confidence": 0.0, "freq_offset_hz": None,
                  "channel_snr_db": None, "raw_bits": None,
                  "raw_packet_bits": None, "phase_slope_rad_per_bin": None}

        one_preamble = self._add_cp(self._ifft_symbol(self._preamble_bin_symbols))
        preamble_wave = np.tile(one_preamble, self.n_preamble_symbols) \
            if self.n_preamble_symbols > 1 else one_preamble

        if len(x) < len(preamble_wave) + 10:
            return result

        norm = np.sqrt(np.sum(preamble_wave ** 2)) * (np.std(x) + 1e-12) * np.sqrt(len(preamble_wave))

        best = (-1.0, 0, 0.0)
        for hz in np.arange(-SYNC_SEARCH_HZ, SYNC_SEARCH_HZ + 1e-9, SYNC_SEARCH_STEP_HZ):
            template = _freq_shift_real(preamble_wave, hz, DESIGN_RATE)
            corr = np.correlate(x, template, mode="valid")
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
        for pb in pre_bins_seq:
            eq = pb / gain0
            noise_powers.append(np.mean(np.abs(eq - self._preamble_bin_symbols) ** 2))
            sig_powers.append(np.mean(np.abs(self._preamble_bin_symbols) ** 2))

        anchors_idx = [self.n_preamble_symbols / 2.0 - 0.5]
        anchors_gain = [gain0]

        layout = self._layout()
        eq_data = np.empty((self.n_data_ofdm_symbols, self.n_active), dtype=np.complex128)
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
                noise_powers.append(np.mean(np.abs(eq_pilot - self._pilot_bin_symbols) ** 2))
                sig_powers.append(np.mean(np.abs(self._pilot_bin_symbols) ** 2))
            cursor += count

        anchors_idx = np.array(anchors_idx)
        anchors_gain = np.array(anchors_gain)

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
                if self.n_comb() > 0:
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

        snr_db = 10 * np.log10(np.mean(sig_powers) / (np.mean(noise_powers) + 1e-15))
        result["channel_snr_db"] = float(snr_db)
        result["pilot_symbols"] = len(anchors_idx) - 1

        data_syms_full = (eq_data * np.exp(-1j * self._phase_schedule)[None, :])
        data_syms_flat = data_syms_full[:, self._data_idx].reshape(-1)
        data_bits = symbols_to_bits(data_syms_flat, self.bits_per_symbol)
        data_bits = data_bits[: self.data_bits] if len(data_bits) > self.data_bits else data_bits
        whitener = _bits.pn_bits(len(data_bits), WHITENER_SEED)
        raw_bits = data_bits ^ whitener
        result["raw_packet_bits"] = raw_bits.copy()
        packet = np.packbits(raw_bits).tobytes()
        payload, meta = _unpack_packet(packet, self.max_payload_bytes)
        result.update(meta)
        result["payload"] = payload
        # raw_bits exposed as the payload-region slice using the packet's
        # own recovered length byte when plausible, else the full packet
        # bit array (harness re-slices with ground truth for BER anyway).
        result["raw_bits"] = raw_bits
        return result

    def n_comb(self) -> int:
        return int(np.sum(self._comb_mask))
