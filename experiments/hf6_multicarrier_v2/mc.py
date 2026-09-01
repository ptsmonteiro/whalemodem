"""Small-N non-orthogonal multicarrier extension of experiments/hf5_8psk_4k/sc.py.

Motivation (see experiments/hf5_8psk_4k/RESULTS.md and
experiments/hc2_32qam, experiments/hf4): summing MANY simultaneous tones
(32-49 carriers) into one composite audio signal drove the IC-7300's SSB
ALC/compressor into intermodulation distortion and killed per-tone SNR,
even though the same signals worked fine in simulation. A SINGLE carrier
(sc.py) works great: 8PSK @ 1500 baud, ~4050 bps net. This module asks
whether a SMALL number of carriers (2, maybe 3-4) can be summed without
re-triggering that IMD, to push net throughput above the single-carrier
baseline.

Design, reusing sc.py wherever possible rather than reinventing it:

  - Each carrier is its own independent RRC-shaped PSK/QAM link, with its
    own centre frequency, its own PN preamble (different seed per carrier
    so preambles don't cross-correlate), its own optional mid-frame pilot
    sequence (different seed too), and its own length+CRC32 framed
    payload -- i.e. N independent copies of sc.py's frame format running
    at N different frequencies, not one OFDM-style joint symbol.
  - No orthogonality requirement: carriers don't need exact subcarrier
    spacing because each one self-synchronizes via its own preamble and
    is demodulated by mixing down to its own frequency and RRC matched
    filtering, which rejects energy well outside its own occupied
    bandwidth as long as centre-frequency separation is large enough.
  - TX: each carrier's passband waveform (at the 12 kHz design rate, same
    as sc.py) is generated independently, then all carriers are summed
    and the *combined* signal is renormalized to the same peak-drive
    convention sc.py uses (peak-normalize then apply the same 0.5
    headroom factor) -- so summing carriers does not, by itself, raise
    the drive level into the ALC compared to a single carrier. This is
    the key precaution against reintroducing the IMD failure mode.
  - RX: the receiver does not need to separate carriers before
    demodulating -- each carrier's own sync/matched-filter stage already
    operates on the *full* composite captured signal and pulls out only
    its own frequency's content (mirroring sc.py's demodulate exactly,
    just parameterized by centre frequency and PN seeds). Results are a
    list of per-carrier dicts, structurally identical to sc.py's single
    result dict, so the same success/SNR bookkeeping applies per-carrier.

All RRC pulse shaping, bit<->symbol mapping, framing (length+CRC32+PN
whitening), and the joint time/frequency preamble-search + phase-ramp
refinement + pilot-interpolated channel tracking are reused verbatim from
sc.py (imported, not copied) -- only the per-carrier centre frequency and
PN seeds are parameterized here.
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
from experiments.hf5_8psk_4k import sc

DESIGN_RATE = sc.DESIGN_RATE
TX_SAMPLE_RATE = sc.TX_SAMPLE_RATE


def _pilot_layout(data_symbols: int, pilot_interval: int):
    return sc._pilot_layout(data_symbols, pilot_interval)


@dataclass(frozen=True)
class CarrierSpec:
    """One independent sc.py-style link at its own centre frequency."""

    carrier_hz: float
    baud: float
    bits_per_symbol: int
    packet_bytes: int
    preamble_seed: int
    pilot_seed: int
    beta: float = 0.35
    span_symbols: int = 6
    preamble_chips: int = 63
    pilot_len: int = 15
    pilot_interval: int = 0

    def __post_init__(self):
        preamble_bits = sc._pn_chips(self.preamble_chips, self.preamble_seed)
        pilot_bits = sc._pn_chips(self.pilot_len, self.pilot_seed, taps=(1, 4))
        object.__setattr__(self, "_preamble_symbols",
                            (1.0 - 2.0 * preamble_bits.astype(np.float64)).astype(np.complex128))
        object.__setattr__(self, "_pilot_symbols",
                            (1.0 - 2.0 * pilot_bits.astype(np.float64)).astype(np.complex128))
        object.__setattr__(self, "_whitener_seed", sc.WHITENER_SEED ^ (self.preamble_seed << 8))

    @property
    def sps(self) -> int:
        sps = DESIGN_RATE / self.baud
        if abs(sps - round(sps)) > 1e-6:
            raise ValueError(f"baud {self.baud} does not divide {DESIGN_RATE} evenly")
        return int(round(sps))

    @property
    def max_payload_bytes(self) -> int:
        return self.packet_bytes - sc.LENGTH_BYTES - sc.CRC_BYTES

    @property
    def data_bits(self) -> int:
        return self.packet_bytes * 8

    @property
    def data_symbols(self) -> int:
        n = self.data_bits / self.bits_per_symbol
        assert abs(n - round(n)) < 1e-9
        return int(round(n))

    @property
    def n_pilot_blocks(self) -> int:
        return sum(1 for kind, _ in _pilot_layout(self.data_symbols, self.pilot_interval)
                   if kind == "pilot")

    def frame_seconds(self) -> float:
        total_symbols = (self.preamble_chips + self.data_symbols
                          + self.n_pilot_blocks * self.pilot_len)
        return total_symbols / self.baud

    # -- TX: produce the 12 kHz-design-rate passband contribution, NOT yet
    # peak-normalized (normalization happens once, after summing all
    # carriers, in MultiCarrierMode.modulate). ------------------------------

    def _passband_component(self, payload: bytes) -> np.ndarray:
        packet = sc._pack_packet(payload, self.packet_bytes)
        raw_bits = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
        whitener = _bits.pn_bits(len(raw_bits), self._whitener_seed)
        data_bits = raw_bits ^ whitener
        data_symbols = sc.bits_to_symbols(data_bits, self.bits_per_symbol)

        pieces = [self._preamble_symbols]
        cursor = 0
        for kind, count in _pilot_layout(self.data_symbols, self.pilot_interval):
            if kind == "data":
                pieces.append(data_symbols[cursor:cursor + count])
                cursor += count
            else:
                pieces.append(self._pilot_symbols)
        symbols = np.concatenate(pieces)

        sps = self.sps
        taps = sc.rrc_taps(sps, self.span_symbols, self.beta)
        upsampled = np.zeros(len(symbols) * sps, dtype=np.complex128)
        upsampled[::sps] = symbols
        shaped = np.convolve(upsampled, taps, mode="full")

        n = np.arange(len(shaped))
        carrier = np.exp(1j * 2 * np.pi * self.carrier_hz * n / DESIGN_RATE)
        return np.real(shaped * carrier)

    # -- RX: demodulate this carrier out of a full composite 12 kHz capture,
    # mirroring sc.SingleCarrierMode.demodulate exactly but parameterized. --

    def demodulate_component(self, captured_12k: np.ndarray) -> dict:
        x = np.asarray(captured_12k, dtype=np.float64)
        result = {"carrier_hz": self.carrier_hz, "synced": False, "crc_ok": False,
                  "payload": None, "confidence": 0.0, "freq_offset_hz": None,
                  "channel_snr_db": None}
        sps = self.sps
        taps = sc.rrc_taps(sps, self.span_symbols, self.beta)

        pre_up = np.zeros(self.preamble_chips * sps, dtype=np.complex128)
        pre_up[::sps] = self._preamble_symbols
        pre_shaped = np.convolve(pre_up, taps, mode="full")
        n = np.arange(len(pre_shaped))

        if len(x) < len(pre_shaped) + 10:
            return result
        norm = (np.sqrt(np.sum(np.abs(pre_shaped) ** 2)) * (np.std(x) + 1e-12)
                * np.sqrt(len(pre_shaped)))

        best = (-1.0, 0, 0.0)
        for hz in np.arange(-sc.SYNC_SEARCH_HZ, sc.SYNC_SEARCH_HZ + 1e-9,
                             sc.SYNC_SEARCH_STEP_HZ):
            pre_carrier = np.exp(1j * 2 * np.pi * (self.carrier_hz + hz) * n / DESIGN_RATE)
            pre_passband = np.real(pre_shaped * pre_carrier)
            corr = np.correlate(x, pre_passband, mode="valid")
            env = np.abs(sc._hilbert_envelope(corr))
            peak = int(np.argmax(env))
            conf = float(env[peak] / (norm + 1e-12))
            if conf > best[0]:
                best = (conf, peak, float(hz))

        confidence, start, freq_offset = best
        result["confidence"] = confidence
        result["freq_offset_hz"] = freq_offset

        layout = _pilot_layout(self.data_symbols, self.pilot_interval)
        total_symbols = self.preamble_chips + self.data_symbols + self.n_pilot_blocks * self.pilot_len

        needed = start + int(total_symbols * sps)
        if confidence < 0.12 or needed > len(x):
            return result
        result["synced"] = True

        span = x[start:start + int((total_symbols + self.span_symbols * 2) * sps)]
        sample0 = (len(taps) - 1)

        def _matched_filter_symbols(offset_hz: float) -> np.ndarray:
            nn = np.arange(len(span))
            mix = np.exp(-1j * 2 * np.pi * (self.carrier_hz + offset_hz) * nn / DESIGN_RATE)
            baseband = span * mix * 2.0
            filtered = np.convolve(baseband, taps, mode="full")
            idx = sample0 + sps * np.arange(total_symbols)
            idx = idx[idx < len(filtered)]
            return filtered[idx]

        symbols = _matched_filter_symbols(freq_offset)
        if len(symbols) < total_symbols:
            result["synced"] = False
            return result
        pre_rx0 = symbols[:self.preamble_chips]
        derot = pre_rx0 * np.conj(self._preamble_symbols)
        phase = np.unwrap(np.angle(derot))
        t = np.arange(self.preamble_chips) * sps / DESIGN_RATE
        slope = np.polyfit(t, phase, 1)[0]
        refine_hz = slope / (2 * np.pi)
        freq_offset += refine_hz
        result["freq_offset_hz"] = float(freq_offset)

        symbols = _matched_filter_symbols(freq_offset)
        if len(symbols) < total_symbols:
            result["synced"] = False
            return result

        pre_rx = symbols[:self.preamble_chips]
        pre_gain = np.sum(pre_rx * np.conj(self._preamble_symbols)) / np.sum(
            np.abs(self._preamble_symbols) ** 2)
        if np.abs(pre_gain) < 1e-9:
            return result
        anchors = [(self.preamble_chips / 2.0, pre_gain)]
        noise_powers = [np.mean(np.abs(pre_rx / pre_gain - self._preamble_symbols) ** 2)]
        sig_powers = [np.mean(np.abs(self._preamble_symbols) ** 2)]

        eq_data = np.empty(self.data_symbols, dtype=np.complex128)
        cursor = self.preamble_chips
        data_cursor = 0
        pending_data: list[tuple[int, int, int]] = []
        for kind, count in layout:
            if kind == "data":
                pending_data.append((cursor, data_cursor, count))
                data_cursor += count
            else:
                pilot_rx = symbols[cursor:cursor + count]
                gain = np.sum(pilot_rx * np.conj(self._pilot_symbols)) / np.sum(
                    np.abs(self._pilot_symbols) ** 2)
                if np.abs(gain) < 1e-9:
                    return result
                anchors.append((cursor + count / 2.0, gain))
                noise_powers.append(np.mean(np.abs(pilot_rx / gain - self._pilot_symbols) ** 2))
                sig_powers.append(np.mean(np.abs(self._pilot_symbols) ** 2))
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
        whitener = _bits.pn_bits(len(data_bits), self._whitener_seed)
        raw_bits = data_bits ^ whitener
        packet = np.packbits(raw_bits).tobytes()
        payload, meta = sc._unpack_packet(packet, self.max_payload_bytes)
        result.update(meta)
        result["payload"] = payload
        return result


@dataclass(frozen=True)
class MultiCarrierMode:
    """N independent CarrierSpec links summed into one composite TX signal
    and demodulated independently on RX. Non-orthogonal by design: no
    exact subcarrier spacing, just enough centre-frequency separation for
    each carrier's own RRC matched filter to reject the others."""

    carriers: tuple[CarrierSpec, ...]
    drive_scale: float = 0.5  # same headroom factor sc.py uses for one carrier

    @property
    def n_carriers(self) -> int:
        return len(self.carriers)

    def frame_seconds(self) -> float:
        return max(c.frame_seconds() for c in self.carriers)

    def total_payload_bytes(self) -> int:
        return sum(c.max_payload_bytes for c in self.carriers)

    # -- TX -----------------------------------------------------------------

    def modulate(self, payloads: list[bytes]) -> np.ndarray:
        if len(payloads) != self.n_carriers:
            raise ValueError("need one payload per carrier")
        components = [c._passband_component(p) for c, p in zip(self.carriers, payloads)]
        max_len = max(len(comp) for comp in components)
        combined = np.zeros(max_len, dtype=np.float64)
        for comp in components:
            combined[:len(comp)] += comp

        combined /= (np.max(np.abs(combined)) + 1e-12)
        combined *= self.drive_scale

        up = 4
        stuffed = np.zeros(len(combined) * up, dtype=np.float64)
        stuffed[::up] = combined
        lpf = sc._design_interp_lpf(up)
        tx = np.convolve(stuffed, lpf, mode="same") * up
        return tx.astype(np.float32)

    # -- RX -----------------------------------------------------------------

    def demodulate(self, captured_12k: np.ndarray) -> list[dict]:
        return [c.demodulate_component(captured_12k) for c in self.carriers]
