"""Drop-in, fast-sync variant of experiments/hf5_8psk_4k/sc.py's
SingleCarrierMode.

Same PHY as hf5 (8PSK@1500baud-class single-carrier, no FEC) -- this is a
pure CPU optimization of the sync-search stage, not a waveform change.
Validated against real over-the-air captures in RESULTS.md (0/10
discrepancies vs. the original sc.py, ~4.8x real measured speedup).

Public API is identical to sc.SingleCarrierMode: modulate(), demodulate(),
max_payload_bytes, frame_seconds(), so this is a drop-in replacement.
Internally it delegates everything except the sync-search stage to sc.py
(imported read-only, never modified) and reuses the fused-FFT
fast_sync_search from experiments/hf5_8psk_4k_profiling/fast_sync.py
(also read-only).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

from experiments.hf5_8psk_4k import sc
from experiments.hf5_8psk_4k_profiling.fast_sync import fast_sync_search

# Re-exported so callers of this module see the same constants as sc.py.
CARRIER_HZ = sc.CARRIER_HZ
DESIGN_RATE = sc.DESIGN_RATE
TX_SAMPLE_RATE = sc.TX_SAMPLE_RATE
RX_SAMPLE_RATE = sc.RX_SAMPLE_RATE


class SingleCarrierMode(sc.SingleCarrierMode):
    """sc.SingleCarrierMode with a fused-FFT sync search in demodulate().

    Everything after acquisition (matched filter, phase-ramp refinement,
    pilot-anchored equalization, bit demapping, packet unpack) is identical
    to sc.py's demodulate() -- only the sync-search inner loop differs.
    """

    def demodulate(self, captured_12k: np.ndarray) -> dict:
        x = np.asarray(captured_12k, dtype=np.float64)
        result = {"synced": False, "crc_ok": False, "payload": None,
                  "confidence": 0.0, "freq_offset_hz": None,
                  "channel_snr_db": None}
        sps = self.sps
        taps = sc.rrc_taps(sps, self.span_symbols, self.beta)

        confidence, start, freq_offset, norm, pre_shaped = fast_sync_search(self, x)
        if norm is None:
            return result
        result["confidence"] = confidence
        result["freq_offset_hz"] = freq_offset

        layout = sc._pilot_layout(self.data_symbols, self.pilot_interval)
        total_symbols = sc.PREAMBLE_CHIPS + self.data_symbols + self.n_pilot_blocks * sc.PILOT_LEN

        needed = start + int(total_symbols * sps)
        if confidence < 0.12 or needed > len(x):
            return result
        result["synced"] = True

        span = x[start:start + int((total_symbols + self.span_symbols * 2) * sps)]
        sample0 = (len(taps) - 1)

        def _matched_filter_symbols(offset_hz: float) -> np.ndarray:
            nn = np.arange(len(span))
            mix = np.exp(-1j * 2 * np.pi * (sc.CARRIER_HZ + offset_hz) * nn / sc.DESIGN_RATE)
            baseband = span * mix * 2.0
            filtered = np.convolve(baseband, taps, mode="full")
            idx = sample0 + sps * np.arange(total_symbols)
            idx = idx[idx < len(filtered)]
            return filtered[idx]

        symbols = _matched_filter_symbols(freq_offset)
        if len(symbols) < total_symbols:
            result["synced"] = False
            return result
        pre_rx0 = symbols[:sc.PREAMBLE_CHIPS]
        derot = pre_rx0 * np.conj(sc.PREAMBLE_SYMBOLS)
        phase = np.unwrap(np.angle(derot))
        t = np.arange(sc.PREAMBLE_CHIPS) * sps / sc.DESIGN_RATE
        slope = np.polyfit(t, phase, 1)[0]
        refine_hz = slope / (2 * np.pi)
        freq_offset += refine_hz
        result["freq_offset_hz"] = float(freq_offset)

        symbols = _matched_filter_symbols(freq_offset)
        if len(symbols) < total_symbols:
            result["synced"] = False
            return result

        pre_rx = symbols[:sc.PREAMBLE_CHIPS]
        pre_gain = np.sum(pre_rx * np.conj(sc.PREAMBLE_SYMBOLS)) / np.sum(np.abs(sc.PREAMBLE_SYMBOLS) ** 2)
        if np.abs(pre_gain) < 1e-9:
            return result
        anchors = [(sc.PREAMBLE_CHIPS / 2.0, pre_gain)]
        noise_powers = [np.mean(np.abs(pre_rx / pre_gain - sc.PREAMBLE_SYMBOLS) ** 2)]
        sig_powers = [np.mean(np.abs(sc.PREAMBLE_SYMBOLS) ** 2)]

        eq_data = np.empty(self.data_symbols, dtype=np.complex128)
        cursor = sc.PREAMBLE_CHIPS
        data_cursor = 0
        pending_data = []
        for kind, count in layout:
            if kind == "data":
                pending_data.append((cursor, data_cursor, count))
                data_cursor += count
            else:
                pilot_rx = symbols[cursor:cursor + count]
                gain = np.sum(pilot_rx * np.conj(sc.PILOT_SYMBOLS)) / np.sum(np.abs(sc.PILOT_SYMBOLS) ** 2)
                if np.abs(gain) < 1e-9:
                    return result
                anchors.append((cursor + count / 2.0, gain))
                noise_powers.append(np.mean(np.abs(pilot_rx / gain - sc.PILOT_SYMBOLS) ** 2))
                sig_powers.append(np.mean(np.abs(sc.PILOT_SYMBOLS) ** 2))
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

        snr_db = 10 * np.log10(np.mean(sig_powers) / (np.mean(noise_powers) + 1e-15))
        result["channel_snr_db"] = float(snr_db)
        result["pilot_blocks"] = len(anchors) - 1

        data_bits = sc.symbols_to_bits(eq_data, self.bits_per_symbol)
        whitener = sc._bits.pn_bits(len(data_bits), sc.WHITENER_SEED)
        raw_bits = data_bits ^ whitener
        packet = np.packbits(raw_bits).tobytes()
        payload, meta = sc._unpack_packet(packet, self.max_payload_bytes)
        result.update(meta)
        result["payload"] = payload
        # Diagnostic-only addition (not present in sc.py's result dict, but
        # additive so this stays a drop-in): the full received packet bit
        # stream (length+payload+CRC, pre-CRC-verdict), for raw-BER
        # computation by hardware_test.py. No FEC exists in this mode, so
        # there is no separate pre-FEC/post-FEC distinction here.
        result["raw_bits"] = raw_bits
        return result
