"""Configurable 50 Hz OFDM with HF7 constellations for analog FM.

Simulated flat_nbfm C/N floor: not measured.
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
from scipy.signal import correlate, hilbert

from whale import framing, waveform
from whale.dsp import bits, interleave, ldpc
from whale.phy.ofdm49 import _constellation_table, _soft_bit_llrs

MODE_ID = 18
CARRIER_SPACING_HZ = 50.0


def _points(order):
    return _constellation_table(order)[0]


def _fit_channel(observed, reference):
    channel = np.sum(observed * np.conj(reference), axis=0) / np.sum(np.abs(reference) ** 2, axis=0)
    model = channel * reference
    power = float(np.mean(np.abs(model) ** 2))
    residual = float(np.mean(np.abs(observed - model) ** 2))
    return channel, residual / power if power > 0 else np.inf


#: Constellation names by bits per carrier, for `geometry`.
_CONSTELLATION = {1: "BPSK", 2: "QPSK", 3: "8PSK", 4: "16-QAM",
                  5: "32-QAM", 6: "64-QAM"}


@dataclass(frozen=True)
class Vf12Mode(waveform.ModeDescription):
    name: str = "vf12"
    mode_id: int = MODE_ID
    bits_per_carrier: int = 4
    fec_rate: str = "3/4"
    band_lo_hz: float = 500.0
    band_hi_hz: float = 3000.0
    fft_size: int = 240
    cp_len: int = 36
    lead_in_seconds: float = 0.5
    target_airtime_seconds: float = 5.0
    n_codewords: int | None = None
    confidence_threshold: float = 0.7
    pilot_comb_stride: int = 8  # 0 disables comb pilots (today's static-header path)
    pilot_time_span: int = 3  # moving-average window, in symbols, for tracking; 1 disables
    tx_sample_rate: int = field(default=48000, init=False)
    rx_sample_rate: int = field(default=12000, init=False)
    carrier_spacing_hz: float = field(default=50.0, init=False)

    def __post_init__(self):
        if self.fft_size != 240:
            raise ValueError("50 Hz spacing requires fft_size=240 at 12 kHz")
        if self.bits_per_carrier not in range(2, 7):
            raise ValueError("bits_per_carrier must be 2 through 6")
        if self.fec_rate not in ldpc.INFORMATION_BITS:
            raise ValueError("unsupported FEC rate")
        if not isinstance(self.cp_len, int) or not 0 <= self.cp_len <= 240:
            raise ValueError("cp_len must be an integer from 0 to 240")
        if not (50 <= self.band_lo_hz <= self.band_hi_hz < 6000):
            raise ValueError("passband must lie within 50..5950 Hz")
        if self.band_lo_hz % 50 or self.band_hi_hz % 50:
            raise ValueError("passband edges must be on the 50 Hz grid")
        if not np.isfinite(self.lead_in_seconds) or self.lead_in_seconds < 0:
            raise ValueError("lead_in_seconds must be finite and nonnegative")
        if not np.isfinite(self.target_airtime_seconds) or self.target_airtime_seconds <= 0:
            raise ValueError("target_airtime_seconds must be finite and positive")
        if (not isinstance(self.pilot_comb_stride, int) or self.pilot_comb_stride < 0
                or self.pilot_comb_stride == 1):
            raise ValueError("pilot_comb_stride must be 0 or an int >= 2")
        if not isinstance(self.pilot_time_span, int) or self.pilot_time_span < 1:
            raise ValueError("pilot_time_span must be an int >= 1")
        if self.n_codewords is None:
            available = self.target_airtime_seconds - self.lead_in_seconds - 0.1
            symbols = int(np.floor(available * 12000 / self.symbol_samples)) - 8
            count = symbols * self.bits_per_symbol // ldpc.N
            object.__setattr__(self, "n_codewords", count)
        if not isinstance(self.n_codewords, int) or self.n_codewords < 1:
            raise ValueError("frame budget cannot hold one codeword")
        if self.chunk_size <= 0 or self.max_payload_bytes > 65535:
            raise ValueError("payload capacity is outside native framing limits")

    @cached_property
    def active_bins(self):
        return tuple(range(int(self.band_lo_hz / 50), int(self.band_hi_hz / 50) + 1))

    @property
    def n_carriers(self):
        return len(self.active_bins)

    @cached_property
    def pilot_positions(self):
        """Indices into active_bins carrying comb pilots, band edges always included."""
        if self.pilot_comb_stride == 0:
            return np.array([], dtype=int)
        n = self.n_carriers
        return np.array(sorted(set(range(0, n, self.pilot_comb_stride)) | {n - 1}))

    @cached_property
    def data_positions(self):
        pilot = set(self.pilot_positions.tolist())
        return np.array([i for i in range(self.n_carriers) if i not in pilot])

    @cached_property
    def pilot_values(self):
        return 1.0 - 2.0 * bits.pn_bits(len(self.pilot_positions), 0x2C7B).astype(float)

    @property
    def bits_per_symbol(self):
        return len(self.data_positions) * self.bits_per_carrier

    @property
    def packet_bytes(self):
        return self.n_codewords * ldpc.INFORMATION_BITS[self.fec_rate] // 8

    @property
    def max_payload_bytes(self):
        return self.packet_bytes - 6  # native length and CRC32

    @property
    def chunk_size(self):
        return self.max_payload_bytes - framing.AIR_HEADER_BYTES

    @property
    def coded_bits(self):
        return self.n_codewords * ldpc.N

    @property
    def payload_symbols(self):
        return (self.coded_bits + self.bits_per_symbol - 1) // self.bits_per_symbol

    @property
    def total_symbols(self):
        return 8 + self.payload_symbols

    @property
    def symbol_samples(self):
        return self.fft_size + self.cp_len

    @cached_property
    def interleaver(self):
        return interleave.multiplicative(self.coded_bits, 8101)

    @cached_property
    def whitener(self):
        return bits.pn_bits(self.coded_bits, 0x17E35)

    @cached_property
    def points(self):
        return _points(self.bits_per_carrier)

    def _known(self, seed, count):
        labels = bits.pn_bits(count * self.n_carriers * 2, seed).reshape(-1, 2)
        return _points(2)[labels @ np.array([2, 1])].reshape(count, self.n_carriers)

    @cached_property
    def header(self):
        sync = self._known(0x5104, 1)
        return np.vstack((np.array([1, 1, -1, 1])[:, None] * sync,
                          self._known(0xE57A, 4)))

    def _build(self, values, factor=1, guard=True):
        core = self.fft_size * factor
        cp = self.cp_len * factor if guard else 0
        spectrum = np.zeros((len(values), core // 2 + 1), dtype=complex)
        spectrum[:, self.active_bins] = values
        body = np.fft.irfft(spectrum, n=core, axis=1)
        return (np.concatenate((body[:, -cp:], body), axis=1)
                if cp else body).reshape(-1)

    def _pack(self, payload):
        if len(payload) > self.max_payload_bytes:
            raise ValueError(f"payload too large for {self.name}: {len(payload)}")
        packet = (len(payload).to_bytes(2, "big") + payload
                  + (binascii.crc32(payload) & 0xffffffff).to_bytes(4, "big"))
        return packet.ljust(self.packet_bytes, b"\0")

    def encode(self, payload):
        payload = bytes(payload)
        info = np.unpackbits(np.frombuffer(self._pack(payload), np.uint8))
        k = ldpc.INFORMATION_BITS[self.fec_rate]
        info = np.pad(info, (0, self.n_codewords * k - len(info)))
        coded = np.concatenate([ldpc.encode(row, self.fec_rate)
                                for row in info.reshape(self.n_codewords, k)])
        stream = self.interleaver.spread(coded) ^ self.whitener
        stream = np.pad(stream, (0, self.payload_symbols * self.bits_per_symbol - len(stream)))
        labels = stream.reshape(-1, self.bits_per_carrier)
        data_syms = self.points[labels @ (1 << np.arange(self.bits_per_carrier - 1, -1, -1))]
        data_syms = data_syms.reshape(self.payload_symbols, len(self.data_positions))
        values = np.empty((self.payload_symbols, self.n_carriers), dtype=complex)
        values[:, self.data_positions] = data_syms
        values[:, self.pilot_positions] = self.pilot_values
        symbols = self._build(np.vstack((self.header, values)), factor=4)
        lead = np.resize(self._build(self._known(0x1EAD, 1), factor=4, guard=False),
                         round(self.lead_in_seconds * 48000)).copy()
        fade = min(240, len(lead))
        lead[:fade] *= np.linspace(0, 1, fade)
        audio = np.concatenate((lead, symbols, np.zeros(4800)))
        peak = np.max(np.abs(audio))
        return (audio / peak).astype(np.float32)

    def _extract(self, audio, start, count):
        if start < 0 or start + count * self.symbol_samples > len(audio):
            return None
        block = audio[start:start + count * self.symbol_samples].reshape(count, self.symbol_samples)
        return np.fft.rfft(block[:, self.cp_len:], axis=1)[:, self.active_bins]

    def decode(self, audio):
        result = {"synced": False, "payload": None, "crc_ok": False, "confidence": 0.0}
        audio = np.asarray(audio, dtype=float)
        if audio.ndim != 1 or not np.all(np.isfinite(audio)):
            return result
        reference = self._build(self.header[:4])
        if len(audio) < 2 * len(reference):
            return result
        coarse = int(np.argmax(np.abs(correlate(hilbert(audio), hilbert(reference), mode="valid"))))
        best = None
        for offset in range(-56, 9):
            start = coarse + offset
            observed = self._extract(audio, start + 4 * self.symbol_samples, 4)
            if observed is not None:
                _, evm = _fit_channel(observed, self.header[4:])
                if best is None or evm < best[1]:
                    best = (start, evm)
        if best is None:
            return result
        start, evm = best
        confidence = 1 / (1 + evm)
        result.update(confidence=confidence, start_index=start)
        if confidence < self.confidence_threshold:
            return result
        result["synced"] = True
        block = self._extract(audio, start + 4 * self.symbol_samples, 4 + self.payload_symbols)
        if block is None:
            return result
        channel, _ = _fit_channel(block[:4], self.header[4:])
        safe = np.where(np.abs(channel) > 1e-12, channel, 1e-12)
        payload = block[4:]
        if self.pilot_comb_stride:
            pilot_idx, data_idx, pv = self.pilot_positions, self.data_positions, self.pilot_values
            carriers = np.arange(self.n_carriers)
            # Drift of each payload symbol relative to the header-fitted channel, at pilot bins.
            resid = payload[:, pilot_idx] / (pv * safe[pilot_idx])
            mag = np.abs(resid)
            phase = np.unwrap(np.angle(resid), axis=1)
            R = (np.array([np.interp(carriers, pilot_idx, row) for row in mag])
                 * np.exp(1j * np.array([np.interp(carriers, pilot_idx, row) for row in phase])))
            if self.pilot_time_span > 1:
                kernel = np.ones(self.pilot_time_span)
                # Normalise by the number of symbols actually in the window so the
                # first and last symbols are not averaged against zero padding.
                taps = np.convolve(np.ones(R.shape[0]), kernel, mode="same")[:, None]
                R = np.apply_along_axis(
                    lambda col: np.convolve(col, kernel, mode="same"), 0, R) / taps
            equalized = payload[:, data_idx] / (safe[data_idx] * R[:, data_idx])
            # Noise from consecutive-symbol differences of the pre-smoothing pilot
            # residual: the (slowly varying) channel cancels in the difference,
            # leaving twice the noise power. This avoids the self-referential bias
            # of comparing against the tracked channel R, which at pilot_time_span
            # == 1 reproduces the observed pilot exactly and floors the estimate.
            if resid.shape[0] >= 2:
                noise_pilot = np.mean(np.abs(np.diff(resid, axis=0)) ** 2, axis=0) / 2
            else:
                noise_pilot = np.mean(np.abs(resid - np.mean(resid, axis=0)) ** 2, axis=0)
            noise = np.maximum(np.interp(carriers, pilot_idx, noise_pilot)[data_idx], 1e-5)
        else:
            equalized = payload / safe
            # Four independent training symbols: correct the fitted residual's 3/4 bias.
            noise = np.maximum(np.mean(np.abs(block[:4] / safe - self.header[4:]) ** 2,
                                       axis=0) * 4 / 3, 1e-5)
        llr = _soft_bit_llrs(equalized.reshape(-1), self.bits_per_carrier,
                             np.tile(noise, self.payload_symbols))[:self.coded_bits]
        llr = self.interleaver.gather(llr * (1 - 2 * self.whitener.astype(float)))
        info, iterations, ok = ldpc.decode_batch(llr.reshape(self.n_codewords, ldpc.N), rate=self.fec_rate)
        packet = np.packbits(info.reshape(-1)[:self.packet_bytes * 8]).tobytes()
        size = int.from_bytes(packet[:2], "big")
        payload = packet[2:2 + size]
        crc = int.from_bytes(packet[2 + size:6 + size], "big")
        good = size <= self.max_payload_bytes and crc == (binascii.crc32(payload) & 0xffffffff)
        result.update(payload=payload if good else None, crc_ok=good, decoded_length=size,
                      codewords_ok=int(np.count_nonzero(ok)),
                      snr_db=float(10 * np.log10(np.median(1 / noise))),
                      carrier_snr_db=10 * np.log10(1 / noise),
                      end_index=start + self.total_symbols * self.symbol_samples)
        return result

    def airtime(self, payload_len):
        return (round(self.lead_in_seconds * 48000) + self.total_symbols * self.symbol_samples * 4 + 4800) / 48000

    @property
    def bits_per_second(self):
        return self.chunk_size * 8 / self.airtime(self.max_payload_bytes)

    def geometry(self):
        return {"carrier_spacing_hz": 50, "carriers": self.n_carriers,
                "band_lo_hz": self.band_lo_hz, "band_hi_hz": self.band_hi_hz,
                "bits_per_carrier": self.bits_per_carrier, "cp_len": self.cp_len,
                "fec_rate": self.fec_rate, "lead_in_seconds": self.lead_in_seconds,
                "n_codewords": self.n_codewords,
                "pilot_comb_stride": self.pilot_comb_stride, "pilot_time_span": self.pilot_time_span,
                "net_bps": self.bits_per_second}

    @property
    def band_hz(self) -> tuple[float, float]:
        bins = self.active_bins
        return (bins[0] * self.carrier_spacing_hz,
                bins[-1] * self.carrier_spacing_hz)

    @property
    def modulation(self) -> str:
        constellation = _CONSTELLATION.get(self.bits_per_carrier,
                                           f"{2 ** self.bits_per_carrier}-ary")
        return (f"{self.n_carriers}-carrier {constellation} OFDM "
                f"({len(self.data_positions)} data, "
                f"{len(self.pilot_positions)} comb pilots)")

    @property
    def fec(self) -> str:
        return f"QC-LDPC {self.fec_rate}"


def mode_for(**kwargs):
    for key in ("carrier_offset_hz", "carrier_spacing_hz"):
        if key in kwargs and kwargs.pop(key) != 50:
            raise ValueError("carrier spacing is fixed at 50 Hz")
    if "bits_per_symbol" in kwargs:
        order = kwargs.pop("bits_per_symbol")
        if "bits_per_carrier" in kwargs and kwargs["bits_per_carrier"] != order:
            raise ValueError("conflicting constellation orders")
        kwargs["bits_per_carrier"] = order
    return Vf12Mode(**kwargs)


VF12 = Vf12Mode()
MODES = (VF12,)
