"""Prototype: fused FFT-based sync search for SingleCarrierMode.demodulate().

Read-only investigation. Does NOT modify experiments/hf5_8psk_4k/sc.py.
Reimplements only the sync-search inner loop (lines ~330-348 of sc.py) using:
  - one FFT of the captured signal x, shared across all frequency hypotheses
  - FFT-based correlation instead of np.correlate direct/time-domain
  - fusion of the correlation-IFFT and the Hilbert-envelope FFT/IFFT into a
    single per-hypothesis forward+inverse FFT pair, at a fast (5-smooth)
    padded length chosen once for the whole search
Everything else (packet framing, matched filter, equalizer, bit mapping) is
imported unmodified from sc.py and reused as-is.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
from scipy.fft import next_fast_len

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "experiments" / "hf5_8psk_4k"))

import sc  # noqa: E402


def fast_sync_search(mode: "sc.SingleCarrierMode", x: np.ndarray):
    """Reimplementation of the sc.py demodulate() sync loop (lines 330-348).

    Returns (best_confidence, best_start, best_freq_hz, norm, pre_shaped)
    matching what the original loop computes, using a fused FFT approach.
    """
    sps = mode.sps
    taps = sc.rrc_taps(sps, mode.span_symbols, mode.beta)

    pre_up = np.zeros(sc.PREAMBLE_CHIPS * sps, dtype=np.complex128)
    pre_up[::sps] = sc.PREAMBLE_SYMBOLS
    pre_shaped = np.convolve(pre_up, taps, mode="full")
    n = np.arange(len(pre_shaped))

    if len(x) < len(pre_shaped) + 10:
        return -1.0, 0, 0.0, None, pre_shaped

    norm = (np.sqrt(np.sum(np.abs(pre_shaped) ** 2)) * (np.std(x) + 1e-12)
            * np.sqrt(len(pre_shaped)))

    L = len(pre_shaped)
    M = len(x) - L + 1  # 'valid' correlation output length (matches np.correlate mode='valid')
    N = next_fast_len(len(x) + L - 1)  # fast length, no circular wraparound

    # One FFT of the capture, shared across every frequency hypothesis.
    Xf = np.fft.fft(x, N)

    # Hilbert one-sided mask at length N (applied to the *full* linear
    # correlation spectrum, so the inverse FFT directly yields the analytic
    # correlation trace -- no second FFT/IFFT round trip needed).
    h = np.zeros(N)
    if N % 2 == 0:
        h[0] = h[N // 2] = 1.0
        h[1:N // 2] = 2.0
    else:
        h[0] = 1.0
        h[1:(N + 1) // 2] = 2.0

    best = (-1.0, 0, 0.0)
    # np.correlate(x, y, 'valid')[k] = sum_i x[k+i] * y[i]
    # = (linear correlation of x and y)[len(y)-1 + k]
    # linear correlation of x,y == real(ifft(fft(x,N) * conj(fft(y,N))))[0:len(x)+len(y)-1]
    valid_offset = 0
    for hz in np.arange(-sc.SYNC_SEARCH_HZ, sc.SYNC_SEARCH_HZ + 1e-9, sc.SYNC_SEARCH_STEP_HZ):
        pre_carrier = np.exp(1j * 2 * np.pi * (sc.CARRIER_HZ + hz) * n / sc.DESIGN_RATE)
        pre_passband = np.real(pre_shaped * pre_carrier)

        Yf = np.fft.fft(pre_passband, N)
        corr_spec = Xf * np.conj(Yf)
        analytic_full = np.fft.ifft(corr_spec * h)
        analytic = analytic_full[valid_offset:valid_offset + M]

        env = np.abs(analytic)
        peak = int(np.argmax(env))
        conf = float(env[peak] / (norm + 1e-12))
        if conf > best[0]:
            best = (conf, peak, float(hz))

    return best[0], best[1], best[2], norm, pre_shaped


def reference_sync_search(mode: "sc.SingleCarrierMode", x: np.ndarray):
    """Verbatim copy of the sc.py demodulate() sync loop, for comparison."""
    sps = mode.sps
    taps = sc.rrc_taps(sps, mode.span_symbols, mode.beta)
    pre_up = np.zeros(sc.PREAMBLE_CHIPS * sps, dtype=np.complex128)
    pre_up[::sps] = sc.PREAMBLE_SYMBOLS
    pre_shaped = np.convolve(pre_up, taps, mode="full")
    n = np.arange(len(pre_shaped))
    if len(x) < len(pre_shaped) + 10:
        return -1.0, 0, 0.0
    norm = np.sqrt(np.sum(np.abs(pre_shaped) ** 2)) * (np.std(x) + 1e-12) * np.sqrt(len(pre_shaped))
    best = (-1.0, 0, 0.0)
    for hz in np.arange(-sc.SYNC_SEARCH_HZ, sc.SYNC_SEARCH_HZ + 1e-9, sc.SYNC_SEARCH_STEP_HZ):
        pre_carrier = np.exp(1j * 2 * np.pi * (sc.CARRIER_HZ + hz) * n / sc.DESIGN_RATE)
        pre_passband = np.real(pre_shaped * pre_carrier)
        corr = np.correlate(x, pre_passband, mode="valid")
        env = np.abs(sc._hilbert_envelope(corr))
        peak = int(np.argmax(env))
        conf = float(env[peak] / (norm + 1e-12))
        if conf > best[0]:
            best = (conf, peak, float(hz))
    return best


class PatchedMode(sc.SingleCarrierMode):
    """Subclass of SingleCarrierMode whose demodulate() uses fast_sync_search
    for the sync stage and is otherwise byte-identical to sc.py's demodulate().
    Used to run the *entire* pipeline (including packet decode) through the
    optimized sync path for end-to-end correctness comparison."""

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
        return result


def main():
    mode = sc.SingleCarrierMode(baud=1500.0, bits_per_symbol=3, packet_bytes=2988,
                                 pilot_interval=400)
    payload = bytes((i * 37 + 5) % 256 for i in range(mode.max_payload_bytes))
    tx48 = mode.modulate(payload)

    # Decimate 48k -> 12k the same crude way (just take every 4th sample;
    # good enough for a synthetic CPU-timing/correctness harness -- we are
    # not modeling the real radio path here).
    tx12 = tx48[::4].astype(np.float64)

    rng = np.random.default_rng(42)

    print(f"{'SNR(dB)':>8} {'orig_conf':>10} {'fast_conf':>10} {'orig_start':>11} "
          f"{'fast_start':>10} {'orig_freq':>10} {'fast_freq':>10} {'match':>6}")

    for snr_db in [30, 15, 8, 5]:
        sig_power = np.mean(tx12 ** 2)
        noise_power = sig_power / (10 ** (snr_db / 10))
        noise = rng.normal(0, np.sqrt(noise_power), size=len(tx12))
        # pad with some silence before/after like a real capture
        pad = rng.normal(0, np.sqrt(noise_power), size=2000)
        x = np.concatenate([pad, tx12 + noise[:len(tx12)], pad])

        ref = reference_sync_search(mode, x)
        fast = fast_sync_search(mode, x)

        match = (ref[1] == fast[1] and abs(ref[2] - fast[2]) < 1e-9
                  and abs(ref[0] - fast[0]) < 1e-6)
        print(f"{snr_db:8d} {ref[0]:10.5f} {fast[0]:10.5f} {ref[1]:11d} "
              f"{fast[1]:10d} {ref[2]:10.2f} {fast[2]:10.2f} {str(match):>6}")

    # End-to-end decode comparison (payload/CRC/confidence) with the patched
    # full demodulate() pipeline vs. the original sc.py implementation.
    print("\nEnd-to-end demodulate() comparison:")
    for snr_db in [30, 15, 8, 6]:
        sig_power = np.mean(tx12 ** 2)
        noise_power = sig_power / (10 ** (snr_db / 10))
        noise = rng.normal(0, np.sqrt(noise_power), size=len(tx12))
        pad = rng.normal(0, np.sqrt(noise_power), size=2000)
        x = np.concatenate([pad, tx12 + noise[:len(tx12)], pad])

        orig_result = mode.demodulate(x)
        patched = PatchedMode(baud=mode.baud, bits_per_symbol=mode.bits_per_symbol,
                               packet_bytes=mode.packet_bytes, beta=mode.beta,
                               span_symbols=mode.span_symbols,
                               pilot_interval=mode.pilot_interval)
        fast_result = patched.demodulate(x)

        same_payload = orig_result["payload"] == fast_result["payload"]
        print(f"SNR={snr_db:3d}dB  orig: synced={orig_result['synced']} "
              f"crc_ok={orig_result['crc_ok']} conf={orig_result['confidence']:.5f} "
              f"freq={orig_result['freq_offset_hz']}  |  fast: synced={fast_result['synced']} "
              f"crc_ok={fast_result['crc_ok']} conf={fast_result['confidence']:.5f} "
              f"freq={fast_result['freq_offset_hz']}  payload_match={same_payload}")

    # Timing comparison (sync-search stage only), averaged over a few runs.
    print("\nTiming (sync-search stage only, averaged over 3 runs):")
    x = np.concatenate([np.zeros(2000), tx12, np.zeros(2000)])
    for label, fn in [("original (np.correlate + per-hz FFT hilbert)", reference_sync_search),
                       ("fused FFT (shared FFT(x), fused hilbert)", fast_sync_search)]:
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            fn(mode, x)
            times.append(time.perf_counter() - t0)
        print(f"  {label:55s}: {min(times):.3f}s (min of 3)")


if __name__ == "__main__":
    main()
