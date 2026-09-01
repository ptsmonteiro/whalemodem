"""From-scratch TRUE OFDM (IFFT-based orthogonal subcarriers + cyclic
prefix) mode for the IC-7300 -> IC-705 audio-coupled path.

Motivation / lineage (see experiments/hf5_8psk_4k/RESULTS.md,
experiments/hf6_multicarrier_v2/RESULTS.md, experiments/path_probe/probe.py):

  - hf5's single carrier (`sc.py`) is the proven baseline: 8PSK @ 1500
    baud, mid-frame pilots, ~4050 bps net, 5/5 hardware decodes.
  - hf6's small-N *non-orthogonal* frequency-division multicarrier did NOT
    beat that baseline: splitting the passband splits available SNR
    roughly proportionally, so more carriers just meant less SNR each.
  - The much earlier many-tone (32-49 carrier) OFDM/multitone attempts
    (`hc2_32qam`, `hf4`) failed completely on real hardware (0/3) despite
    working in simulation, because summing that many simultaneous tones
    drove the IC-7300's SSB ALC/compressor into intermodulation distortion
    (IMD) -- a high-crest-factor problem, not a bandwidth or SNR problem.
    `path_probe.py` used a Newman low-PAPR phase schedule to characterize
    the channel without triggering that failure mode itself.

This module ("v3") builds a *true* OFDM PHY -- proper IFFT synthesis of
orthogonal subcarriers with a cyclic prefix, unlike hf6's ad-hoc
non-orthogonal per-carrier sc.py copies -- but keeps it deliberately
conservative given what's now known about this hardware path:

  - Real-valued IFFT (Hermitian-symmetric spectrum): the FFT bins already
    land at real audio frequencies (bin k -> k * DESIGN_RATE / fft_size
    Hz), so the OFDM symbol is synthesized directly as a real passband
    signal -- no separate carrier-mixing stage is needed the way sc.py
    needed one for its single sinusoid.
  - Small number of active subcarriers (a handful, chosen from bins
    landing inside 300-2700 Hz), not dozens -- the direct lesson from
    hc2_32qam/hf4's IMD collapse.
  - A deterministic Newman-like per-bin phase offset is folded into every
    OFDM symbol (applied identically to preamble, pilot, and data symbols)
    specifically to hold down crest factor; because it's applied
    identically everywhere, the receiver's channel-estimate-based
    per-subcarrier equalization removes it for free, so it costs nothing
    at the receiver.
  - Composite waveform is peak-normalized and then given the *same*
    0.5 headroom backoff sc.py/mc.py use, so this design is never driven
    harder into the ALC than the proven baseline.
  - Cyclic prefix guards against the audio path's reverb/multipath the
    same way it does in any OFDM system; length is a tunable fraction of
    the FFT size.
  - A 2-symbol repeated-content preamble (mirroring sc.py's
    matched-filter + frequency-offset-search-bank acquisition technique,
    generalized to a wideband OFDM waveform via a Hilbert-transform-based
    frequency-shift template) gives coarse timing + a fine per-subcarrier
    channel estimate; a coarse-to-fine CFO refinement uses the phase
    rotation between the two repeated preamble symbols, exactly the same
    problem (several-Hz fixed carrier offset on this audio path) sc.py
    had to solve for its single carrier.
  - Mid-frame pilot OFDM symbols (same idea as sc.py's mid-frame BPSK
    pilot chip blocks, generalized to "one whole known OFDM symbol") give
    additional per-subcarrier (time, complex-gain) anchors so per-carrier
    gain/phase is linearly interpolated across the frame instead of held
    fixed from the preamble alone -- built in from the start rather than
    discovered as a failure mode the hard way, per the task brief.

No FEC, no equalizer bank beyond simple per-subcarrier LS channel
estimation + linear interpolation in time -- same philosophy as sc.py:
find the real-hardware ceiling of a clean design first.
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

# ---------------------------------------------------------------------------
# Fixed channel constants (same convention as sc.py / mc.py)
# ---------------------------------------------------------------------------

TX_SAMPLE_RATE = _sc.TX_SAMPLE_RATE     # 48000, whale.transport.TX_SAMPLE_RATE
RX_SAMPLE_RATE = _sc.RX_SAMPLE_RATE     # 12000, bench.snapshot_rx() decimates to this
DESIGN_RATE = RX_SAMPLE_RATE            # all DSP below is designed at 12 kHz

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
    """Positive-frequency bin indices (1..fft_size/2-1) whose centre
    frequency falls inside [lo_hz, hi_hz] on a real fft_size-point IFFT at
    DESIGN_RATE."""
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
    """Full analytic signal (x + j*Hilbert{x}) via FFT, used to build
    frequency-shifted real templates and to undo a channel frequency
    offset on a captured real signal (mirrors sc.py's frequency-offset
    search but generalized from a single-carrier mixer to any real
    passband signal, since OFDM here has no separate carrier stage)."""
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
    """Shift the frequency content of a real signal by `hz` (can be
    negative) and return the real part -- the inverse of what a fixed
    audio-path carrier/BFO offset does to a transmitted real passband
    signal."""
    analytic = _hilbert_analytic(x)
    n = np.arange(len(x))
    shifted = analytic * np.exp(1j * 2 * np.pi * hz * n / rate)
    return shifted.real


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OFDMMode:
    fft_size: int
    cp_len: int
    active_bins: tuple[int, ...]     # sorted ascending bin indices, 1..fft_size/2-1
    bits_per_symbol: int
    packet_bytes: int
    pilot_interval: int = 0          # data OFDM symbols between pilot OFDM symbols; 0 disables
    n_preamble_symbols: int = 2
    drive_scale: float = 0.5         # same headroom convention as sc.py/mc.py
    preamble_seed: int = 0x33
    pilot_seed: int = 0x51

    def __post_init__(self):
        bins = tuple(sorted(self.active_bins))
        object.__setattr__(self, "active_bins", bins)
        n_active = len(bins)
        phases = _newman_phases(n_active)
        # Deterministic known symbols for preamble/pilot: PN-derived BPSK
        # bits, rotated by the fixed low-PAPR phase schedule. Same
        # rotation is applied to data symbols below, so it cancels out at
        # the receiver's per-bin equalizer.
        pre_bits = _pn_chips(n_active, self.preamble_seed)
        pre_bpsk = (1.0 - 2.0 * pre_bits.astype(np.float64))
        object.__setattr__(self, "_preamble_bin_symbols",
                            (pre_bpsk * np.exp(1j * phases)).astype(np.complex128))
        pilot_bits = _pn_chips(n_active, self.pilot_seed, taps=(1, 4))
        pilot_bpsk = (1.0 - 2.0 * pilot_bits.astype(np.float64))
        object.__setattr__(self, "_pilot_bin_symbols",
                            (pilot_bpsk * np.exp(1j * phases)).astype(np.complex128))
        object.__setattr__(self, "_phase_schedule", phases)

    # -- basic geometry -----------------------------------------------------

    @property
    def n_active(self) -> int:
        return len(self.active_bins)

    @property
    def symbol_len(self) -> int:
        return self.fft_size + self.cp_len

    @property
    def bits_per_ofdm_symbol(self) -> int:
        return self.n_active * self.bits_per_symbol

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
        """Segment order after the preamble: alternating ('data', n) /
        ('pilot', 1) OFDM-symbol chunks, one pilot symbol after every
        `pilot_interval` data symbols (including a trailing one), mirroring
        sc.py's `_pilot_layout`."""
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
        """Peak-to-RMS ratio of one bare data OFDM symbol (worst case:
        random data, no CP-overlap effects), for PAPR reporting."""
        rng = np.random.default_rng(1234)
        bits = rng.integers(0, 2, self.bits_per_ofdm_symbol, dtype=np.uint8)
        syms = bits_to_symbols(bits, self.bits_per_symbol) * np.exp(1j * self._phase_schedule)
        wave = self._ifft_symbol(syms)
        peak = np.max(np.abs(wave))
        rms = np.sqrt(np.mean(wave ** 2))
        return 20 * np.log10(peak / (rms + 1e-15))

    # -- IFFT / FFT machinery -------------------------------------------------

    def _ifft_symbol(self, bin_symbols: np.ndarray) -> np.ndarray:
        """bin_symbols: complex array, one value per active bin (already
        phase-rotated as desired). Returns the real fft_size-length time
        domain OFDM symbol (no CP)."""
        spec = np.zeros(self.fft_size, dtype=np.complex128)
        for b, s in zip(self.active_bins, bin_symbols):
            spec[b] = s
            spec[self.fft_size - b] = np.conj(s)
        return np.real(np.fft.ifft(spec)) * self.fft_size

    def _fft_bins(self, time_symbol: np.ndarray) -> np.ndarray:
        """Inverse of _ifft_symbol: FFT the received (already CP-stripped)
        real fft_size-length time-domain symbol and pick out active bins."""
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
        data_syms = data_syms_flat.reshape(self.n_data_ofdm_symbols, self.n_active)
        data_syms = data_syms * np.exp(1j * self._phase_schedule)[None, :]

        pieces = []
        for _ in range(self.n_preamble_symbols):
            pieces.append(self._add_cp(self._ifft_symbol(self._preamble_bin_symbols)))

        cursor = 0
        for kind, count in self._layout():
            if kind == "data":
                for i in range(count):
                    pieces.append(self._add_cp(self._ifft_symbol(data_syms[cursor + i])))
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

    def demodulate(self, captured_12k: np.ndarray) -> dict:
        x = np.asarray(captured_12k, dtype=np.float64)
        result = {"synced": False, "crc_ok": False, "payload": None,
                  "confidence": 0.0, "freq_offset_hz": None,
                  "channel_snr_db": None}

        one_preamble = self._add_cp(self._ifft_symbol(self._preamble_bin_symbols))
        preamble_wave = np.tile(one_preamble, self.n_preamble_symbols) \
            if self.n_preamble_symbols > 1 else one_preamble
        # (all preamble OFDM symbols carry identical known content, so the
        # template is just the single symbol repeated)

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

        # Fine CFO refinement from the phase rotation between the (n>=2)
        # repeated, identical-content preamble OFDM symbols -- same idea as
        # sc.py's preamble phase-ramp fit, generalized to a per-symbol
        # (rather than per-chip) rotation.
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

        # Channel estimate from the preamble (averaged over repeats).
        pre_bins_seq = [_symbol_bins(i, corrected) for i in range(self.n_preamble_symbols)]
        pre_avg = np.mean(pre_bins_seq, axis=0)
        gain0 = pre_avg / self._preamble_bin_symbols
        if np.any(np.abs(gain0) < 1e-9):
            return result

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
                if np.any(np.abs(gain) < 1e-9):
                    return result
                anchors_idx.append(float(cursor))
                anchors_gain.append(gain)
                eq_pilot = pilot_bins / gain
                noise_powers.append(np.mean(np.abs(eq_pilot - self._pilot_bin_symbols) ** 2))
                sig_powers.append(np.mean(np.abs(self._pilot_bin_symbols) ** 2))
            cursor += count

        anchors_idx = np.array(anchors_idx)
        anchors_gain = np.array(anchors_gain)  # (n_anchors, n_active) complex

        for start_sym, start_eq, count in pending:
            idx = start_sym + np.arange(count)
            gain_trace = np.empty((count, self.n_active), dtype=np.complex128)
            for b in range(self.n_active):
                re = np.interp(idx, anchors_idx, anchors_gain[:, b].real)
                im = np.interp(idx, anchors_idx, anchors_gain[:, b].imag)
                gain_trace[:, b] = re + 1j * im
            for i in range(count):
                bins = _symbol_bins(start_sym + i, corrected)
                eq_data[start_eq + i] = bins / gain_trace[i]

        snr_db = 10 * np.log10(np.mean(sig_powers) / (np.mean(noise_powers) + 1e-15))
        result["channel_snr_db"] = float(snr_db)
        result["pilot_symbols"] = len(anchors_idx) - 1

        data_syms_flat = (eq_data * np.exp(-1j * self._phase_schedule)[None, :]).reshape(-1)
        data_bits = symbols_to_bits(data_syms_flat, self.bits_per_symbol)
        data_bits = data_bits[: self.data_bits] if len(data_bits) > self.data_bits else data_bits
        whitener = _bits.pn_bits(len(data_bits), WHITENER_SEED)
        raw_bits = data_bits ^ whitener
        packet = np.packbits(raw_bits).tobytes()
        payload, meta = _unpack_packet(packet, self.max_payload_bytes)
        result.update(meta)
        result["payload"] = payload
        return result
