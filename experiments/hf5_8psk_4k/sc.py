"""From-scratch single-carrier audio-passband mode for the IC-7300 -> IC-705
audio-coupled path.

Independent of whale/modes/*. Reuses only whale.dsp.bits (PN whitening) as
a generic primitive. Everything else -- pulse shaping, sync, carrier
mixing, symbol mapping, framing -- is written fresh here.

Design, built up in small, hardware-validated steps (see RESULTS.md):

  - One sinusoidal carrier in the middle of the 300-2700 Hz passband
    (1500 Hz), single-sideband-friendly (real passband signal, no image
    concerns because the whole chain is already real audio).
  - Root-raised-cosine pulse shaping at a chosen symbol rate.
  - A known BPSK preamble (a maximal-length PN sequence) used for
    matched-filter frame-start correlation and a single-tap channel
    (gain + phase) estimate. No OFDM, no equalizer bank -- one carrier,
    one channel tap.
  - Frame body: [16-bit length][payload][32-bit CRC32], whitened with
    whale.dsp.bits.pn_bits, then mapped to BPSK/QPSK/8PSK/16-QAM symbols
    depending on `bits_per_symbol`.
  - No FEC. The point of this experiment is to find the real-hardware
    ceiling of a clean single-carrier link first; FEC is a lever to pull
    afterward if headroom is spent on burst errors rather than steady SNR.
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

# ---------------------------------------------------------------------------
# Fixed channel constants
# ---------------------------------------------------------------------------

TX_SAMPLE_RATE = 48000          # whale.transport.TX_SAMPLE_RATE
RX_SAMPLE_RATE = 12000          # bench.snapshot_rx() decimates to this
DESIGN_RATE = RX_SAMPLE_RATE    # all DSP below is designed at 12 kHz and
                                 # upsampled x4 for TX
CARRIER_HZ = 1500.0             # centre of the 300-2700 Hz audio passband

LENGTH_BYTES = 2
CRC_BYTES = 4
WHITENER_SEED = 0xACE1

# Preamble: 63-chip PN sequence (order-6 LFSR), one BPSK symbol/chip.
PREAMBLE_CHIPS = 63
PREAMBLE_SEED = 0x2E


def _pn_chips(count: int, seed: int, taps=(0, 1)) -> np.ndarray:
    """Small maximal-length-ish LFSR, independent of whale.dsp.bits.pn_bits
    (that one is order-17 / meant for whitening long payloads); this is a
    short deterministic chip sequence for the preamble."""
    state = seed & 0x3F
    if state == 0:
        state = 1
    out = np.empty(count, dtype=np.uint8)
    for i in range(count):
        out[i] = state & 1
        fb = ((state >> taps[0]) ^ (state >> taps[1])) & 1
        state = ((state >> 1) | (fb << 5)) & 0x3F
    return out


PREAMBLE_BITS = _pn_chips(PREAMBLE_CHIPS, PREAMBLE_SEED)
PREAMBLE_SYMBOLS = (1.0 - 2.0 * PREAMBLE_BITS.astype(np.float64))  # +/-1 BPSK

# Joint acquisition search grid: audio-path frequency offset budget.
SYNC_SEARCH_HZ = 20.0
SYNC_SEARCH_STEP_HZ = 1.0

# Mid-frame pilot block: a short known BPSK sequence dropped into the data
# stream every `pilot_interval` data symbols. The preamble gives one phase
# anchor at t=0; over a long frame the audio path's residual phase drifts
# slowly (see RESULTS.md round 3), so additional anchors let the receiver
# track (interpolate) that drift instead of assuming it is zero for the
# whole frame.
PILOT_LEN = 15
PILOT_SEED = 0x15
PILOT_BITS = _pn_chips(PILOT_LEN, PILOT_SEED, taps=(1, 4))
PILOT_SYMBOLS = (1.0 - 2.0 * PILOT_BITS.astype(np.float64))  # +/-1 BPSK


def _pilot_layout(data_symbols: int, pilot_interval: int) -> list[tuple[str, int]]:
    """Segment order after the preamble: alternating ('data', n) / ('pilot',
    PILOT_LEN) chunks, one pilot block after every `pilot_interval` data
    symbols (including a trailing one after the final, possibly short,
    data chunk -- so every data chunk has a phase anchor at both ends, the
    leading one being the preamble for the first chunk)."""
    if pilot_interval <= 0:
        return [("data", data_symbols)] if data_symbols else []
    segments: list[tuple[str, int]] = []
    remaining = data_symbols
    while remaining > 0:
        chunk = min(pilot_interval, remaining)
        segments.append(("data", chunk))
        segments.append(("pilot", PILOT_LEN))
        remaining -= chunk
    return segments


# ---------------------------------------------------------------------------
# Pulse shaping
# ---------------------------------------------------------------------------

def rrc_taps(sps: int, span_symbols: int, beta: float) -> np.ndarray:
    n = span_symbols * sps
    t = (np.arange(-n, n + 1)) / sps
    taps = np.empty_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-8:
            taps[i] = 1.0 - beta + 4 * beta / np.pi
        elif beta > 0 and abs(abs(4 * beta * ti) - 1.0) < 1e-8:
            taps[i] = (beta / np.sqrt(2)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * beta))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta)))
        else:
            num = (np.sin(np.pi * ti * (1 - beta))
                   + 4 * beta * ti * np.cos(np.pi * ti * (1 + beta)))
            den = np.pi * ti * (1 - (4 * beta * ti) ** 2)
            taps[i] = num / den
    taps /= np.sqrt(np.sum(taps ** 2))
    return taps


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------

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
    raise ValueError(f"unsupported bits_per_symbol={bps}")


# ---------------------------------------------------------------------------
# Frame codec (length + CRC32, whitened, no FEC)
# ---------------------------------------------------------------------------

def _pack_packet(payload: bytes, packet_bytes: int) -> bytes:
    if len(payload) > packet_bytes - LENGTH_BYTES - CRC_BYTES:
        raise ValueError("payload too large for this frame")
    import binascii
    packet = bytearray(packet_bytes)
    packet[0:LENGTH_BYTES] = len(payload).to_bytes(LENGTH_BYTES, "big")
    packet[LENGTH_BYTES:LENGTH_BYTES + len(payload)] = payload
    crc_at = LENGTH_BYTES + len(payload)
    packet[crc_at:crc_at + CRC_BYTES] = (
        binascii.crc32(payload) & 0xFFFFFFFF).to_bytes(CRC_BYTES, "big")
    return bytes(packet)


def _unpack_packet(packet: bytes, max_payload: int) -> tuple[bytes | None, dict]:
    import binascii
    length = int.from_bytes(packet[:LENGTH_BYTES], "big")
    meta = {"decoded_length": length, "crc_ok": False}
    if length > max_payload:
        meta["failure"] = "invalid length"
        return None, meta
    payload = packet[LENGTH_BYTES:LENGTH_BYTES + length]
    crc_at = LENGTH_BYTES + length
    received_crc = int.from_bytes(packet[crc_at:crc_at + CRC_BYTES], "big")
    computed_crc = binascii.crc32(payload) & 0xFFFFFFFF
    meta.update(received_crc32=received_crc, computed_crc32=computed_crc,
                crc_ok=received_crc == computed_crc)
    if not meta["crc_ok"]:
        meta["failure"] = "CRC mismatch"
        return None, meta
    return payload, meta


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SingleCarrierMode:
    baud: float
    bits_per_symbol: int
    packet_bytes: int
    beta: float = 0.35
    span_symbols: int = 6
    pilot_interval: int = 0  # data symbols between pilot blocks; 0 disables

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
    def data_symbols(self) -> int:
        n = self.data_bits / self.bits_per_symbol
        assert abs(n - round(n)) < 1e-9, "packet_bytes*8 must divide bits_per_symbol"
        return int(round(n))

    @property
    def n_pilot_blocks(self) -> int:
        return sum(1 for kind, _ in _pilot_layout(self.data_symbols, self.pilot_interval)
                   if kind == "pilot")

    def frame_seconds(self) -> float:
        total_symbols = (PREAMBLE_CHIPS + self.data_symbols
                          + self.n_pilot_blocks * PILOT_LEN)
        return total_symbols / self.baud

    # -- TX ---------------------------------------------------------------

    def modulate(self, payload: bytes) -> np.ndarray:
        packet = _pack_packet(payload, self.packet_bytes)
        raw_bits = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
        whitener = _bits.pn_bits(len(raw_bits), WHITENER_SEED)
        data_bits = raw_bits ^ whitener
        data_symbols = bits_to_symbols(data_bits, self.bits_per_symbol)

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
        passband *= 0.5  # conservative drive level (see hardware_test.py note)

        # Upsample 12 kHz design -> 48 kHz TX by simple zero-stuff + FIR LPF.
        up = 4
        stuffed = np.zeros(len(passband) * up, dtype=np.float64)
        stuffed[::up] = passband
        lpf = _design_interp_lpf(up)
        tx = np.convolve(stuffed, lpf, mode="same") * up
        return tx.astype(np.float32)

    # -- RX ---------------------------------------------------------------

    def demodulate(self, captured_12k: np.ndarray) -> dict:
        x = np.asarray(captured_12k, dtype=np.float64)
        result = {"synced": False, "crc_ok": False, "payload": None,
                  "confidence": 0.0, "freq_offset_hz": None,
                  "channel_snr_db": None}
        sps = self.sps
        taps = rrc_taps(sps, self.span_symbols, self.beta)

        # Joint (start-time, frequency-offset) search: audio-path carrier
        # frequency is not exact (soundcard clocks, SSB BFO offsets can be
        # several Hz), and a single-shot fixed-frequency correlation over a
        # preamble long enough to be noise-robust self-cancels under even a
        # few Hz of offset (the phase rotates across the correlation
        # window). So correlate against a small bank of frequency-shifted
        # preamble templates and take the best (time, freq) jointly.
        pre_up = np.zeros(PREAMBLE_CHIPS * sps, dtype=np.complex128)
        pre_up[::sps] = PREAMBLE_SYMBOLS
        pre_shaped = np.convolve(pre_up, taps, mode="full")
        n = np.arange(len(pre_shaped))

        if len(x) < len(pre_shaped) + 10:
            return result
        norm = np.sqrt(np.sum(np.abs(pre_shaped) ** 2)) * (np.std(x) + 1e-12) * np.sqrt(len(pre_shaped))

        best = (-1.0, 0, 0.0)  # (confidence, start, freq_hz)
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

        # Refine the 1 Hz-grid coarse offset with a linear phase-ramp fit
        # over the (known) preamble symbols -- residual offset within one
        # grid step still rotates the constellation over a multi-second
        # frame otherwise.
        symbols = _matched_filter_symbols(freq_offset)
        if len(symbols) < total_symbols:
            result["synced"] = False
            return result
        pre_rx0 = symbols[:PREAMBLE_CHIPS]
        derot = pre_rx0 * np.conj(PREAMBLE_SYMBOLS)
        phase = np.unwrap(np.angle(derot))
        t = np.arange(PREAMBLE_CHIPS) * sps / DESIGN_RATE
        slope = np.polyfit(t, phase, 1)[0]  # rad/s
        refine_hz = slope / (2 * np.pi)
        freq_offset += refine_hz
        result["freq_offset_hz"] = float(freq_offset)

        symbols = _matched_filter_symbols(freq_offset)
        if len(symbols) < total_symbols:
            result["synced"] = False
            return result

        pre_rx = symbols[:PREAMBLE_CHIPS]

        # Channel-gain anchors: the preamble, plus one per mid-frame pilot
        # block (if any). Each anchor is (symbol-index at its centre,
        # complex gain estimated there). Data symbols between anchors get a
        # linearly-interpolated gain instead of the single frame-wide
        # estimate -- this is what lets frames run past the point where the
        # audio path's own phase drifts (see RESULTS.md round 3).
        pre_gain = np.sum(pre_rx * np.conj(PREAMBLE_SYMBOLS)) / np.sum(np.abs(PREAMBLE_SYMBOLS) ** 2)
        if np.abs(pre_gain) < 1e-9:
            return result
        anchors = [(PREAMBLE_CHIPS / 2.0, pre_gain)]

        noise_powers = [np.mean(np.abs(pre_rx / pre_gain - PREAMBLE_SYMBOLS) ** 2)]
        sig_powers = [np.mean(np.abs(PREAMBLE_SYMBOLS) ** 2)]

        eq_data = np.empty(self.data_symbols, dtype=np.complex128)
        cursor = PREAMBLE_CHIPS       # position in `symbols`
        data_cursor = 0                # position in eq_data / data_rx
        pending_data: list[tuple[int, int]] = []  # (start_in_symbols, start_in_eq_data, count) awaiting the next anchor
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

        # Fill in gain for every data chunk once both its bounding anchors
        # (or, for a trailing chunk past the last pilot, one anchor) exist.
        anchor_idx = np.array([a[0] for a in anchors])
        anchor_gain = np.array([a[1] for a in anchors])
        for start_in_symbols, start_in_eq, count in pending_data:
            idx = start_in_symbols + np.arange(count)
            re = np.interp(idx, anchor_idx, anchor_gain.real)
            im = np.interp(idx, anchor_idx, anchor_gain.imag)
            gain_trace = re + 1j * im
            chunk_rx = symbols[start_in_symbols:start_in_symbols + count]
            eq_data[start_in_eq:start_in_eq + count] = chunk_rx / gain_trace

        snr_db = 10 * np.log10(np.mean(sig_powers) / (np.mean(noise_powers) + 1e-15))
        result["channel_snr_db"] = float(snr_db)
        result["pilot_blocks"] = len(anchors) - 1

        data_bits = symbols_to_bits(eq_data, self.bits_per_symbol)
        whitener = _bits.pn_bits(len(data_bits), WHITENER_SEED)
        raw_bits = data_bits ^ whitener
        packet = np.packbits(raw_bits).tobytes()
        payload, meta = _unpack_packet(packet, self.max_payload_bytes)
        result.update(meta)
        result["payload"] = payload
        return result


def _design_interp_lpf(up: int) -> np.ndarray:
    # Simple windowed-sinc low-pass at Nyquist/up for zero-stuffed upsampling.
    numtaps = 8 * up + 1
    t = np.arange(numtaps) - numtaps // 2
    cutoff = 1.0 / up
    h = cutoff * np.sinc(cutoff * t)
    h *= np.hanning(numtaps)
    h /= np.sum(h)
    return h


def _hilbert_envelope(x: np.ndarray) -> np.ndarray:
    # Envelope of a real correlation trace via FFT-based Hilbert transform.
    n = len(x)
    xf = np.fft.fft(x)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1
        h[1:n // 2] = 2
    else:
        h[0] = 1
        h[1:(n + 1) // 2] = 2
    analytic = np.fft.ifft(xf * h)
    return analytic


