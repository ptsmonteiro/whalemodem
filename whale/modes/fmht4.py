"""FMHT rung 4: 31.25 Hz OFDM, 8-PSK, rate-3/4 LDPC, PAPR-limited.

The top rung of the FM-handheld ladder and the only one that is OFDM: rungs
1-3 use SC-FDE to dodge peak-to-average ratio entirely. OFDM is affordable
here because this rung is meant for a line-of-sight path at high audio SNR,
so the waveform is peak-limited on purpose, by Newman phases on every known
symbol plus iterative clip-and-filter on the payload.

Peak limiting here is a lever on average deviation, not a defence against a
mic compressor: on radios, clipping and audio drive move the same single
quantity, and that quantity has an optimum. Under-drive and the path is
noise-limited; over-clip and the waveform's own distortion becomes the
ceiling. See `papr_target_db`.

Geometry (shared by the whole FM-handheld family): FFT block 32 ms, 2 ms
cyclic prefix, 34 ms symbol, 31.25 Hz bin spacing, 64 active bins from
468.75 to 2437.5 Hz. Eight of the 64 bins are comb pilots (12.5%), leaving
56 data bins: 168 raw bits per symbol, 5,640 gross bit/s.

No carrier recovery: an FM discriminator hands the sound card baseband
audio, so there is no carrier frequency offset, only soundcard clock offset.
Per-bin equalisation is measured from the preamble and tracked from the
comb pilots; the pre-emphasis/de-emphasis tilt is never modelled
analytically.

Simulated flat_nbfm C/N floor: 2 dB.
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
from scipy.signal import correlate, hilbert

from whale import framing, waveform
from whale.dsp import bits, interleave, ldpc
from whale.dsp.constellation import constellation_table, soft_bit_llrs
from whale.phy.ofdm49 import bits_to_symbols as _ofdm49_bits_to_symbols

MODE_ID = 27

#: 31.25 Hz spacing needs a 384-point block at the 12 kHz receive rate.
FFT_SIZE = 384
#: 2 ms guard at 12 kHz. Present for clock slip and filter ringing, not for
#: delay spread: an FM audio path has none worth the name.
CP_LEN = 24
#: Receive-rate samples per transmit-rate sample.
TX_FACTOR = 4
RX_RATE = 12000
TX_RATE = 48000
CARRIER_SPACING_HZ = RX_RATE / FFT_SIZE  # 31.25


def _points(order):
    return constellation_table(order, mapper=_ofdm49_bits_to_symbols)[0]


def _newman(count, root=1):
    """Unit-modulus Newman/Zadoff-Chu phases: low PAPR by construction."""
    k = np.arange(count)
    return np.exp(1j * np.pi * root * k * k / count)


def _fit_channel(observed, reference):
    channel = np.sum(observed * np.conj(reference), axis=0) / np.sum(
        np.abs(reference) ** 2, axis=0)
    model = channel * reference
    power = float(np.mean(np.abs(model) ** 2))
    residual = float(np.mean(np.abs(observed - model) ** 2))
    return channel, residual / power if power > 0 else np.inf


def papr_db(audio):
    """Peak-to-average power ratio of a real waveform, in dB."""
    audio = np.asarray(audio, dtype=float)
    mean_power = float(np.mean(audio ** 2))
    if mean_power <= 0:
        return 0.0
    return float(10 * np.log10(np.max(audio ** 2) / mean_power))


@dataclass(frozen=True)
class Fmht4Mode(waveform.ModeDescription):
    name: str = "fmht4"
    mode_id: int = MODE_ID
    bits_per_carrier: int = 3  # 8-PSK. Not 16-QAM: see module docstring.
    fec_rate: str = "3/4"
    lo_bin: int = 15  # 468.75 Hz
    hi_bin: int = 78  # 2437.5 Hz
    lead_in_seconds: float = 0.25
    #: Sync symbols ahead of the four channel-training symbols. Together with
    #: `lead_in_seconds` this is the preamble, and on a handheld it is a
    #: robustness parameter, not overhead to be minimised: see `encode`.
    sync_symbols: int = 4
    target_airtime_seconds: float = 4.5
    n_codewords: int | None = None
    confidence_threshold: float = 0.7
    pilot_comb_stride: int = 9  # 8 pilots of 64 bins = 12.5%
    pilot_time_span: int = 3
    #: Clip threshold for the clip-and-filter loop, in dB above the block RMS.
    #: Filtering regrows peaks, so the PAPR actually achieved lands about
    #: 0.3 dB above this; `modulated_papr_db` is the number that counts.
    #:
    #: 9 dB, measured on radios on 2026-09-22 (14-cell sweep, both
    #: directions, IC-705 <-> digirig). Clipping pulls two ways and 9 dB is
    #: where they balance:
    #:
    #:   * it raises average deviation for a fixed peak (+3.5 dB here), which
    #:     is what rescues a quiet path -- unclipped at half drive delivered
    #:     0/3 where this setting delivered 3/3 at 24.1 dB SINR; and
    #:   * it injects in-band distortion with a hard SINR ceiling, which is
    #:     what sinks an over-clipped one -- 6.5 dB measured 2/3 at 17.2 dB,
    #:     right at its own loopback distortion floor.
    #:
    #: So do not lower it to the 6 dB the family brief asks for: that was
    #: measured losing frames. Do not raise it to unclipped either.
    papr_target_db: float = 9.0
    papr_iterations: int = 12
    #: Straight-line pre-emphasis of the top bin over the bottom one, applied
    #: before the PAPR loop. Zero until a path measurement says otherwise:
    #: tilt is equalised out per bin at the receiver either way, and every dB
    #: of deliberate tilt is a dB of peak the limiter will take back.
    tx_tilt_db: float = 0.0
    tx_sample_rate: int = field(default=TX_RATE, init=False)
    rx_sample_rate: int = field(default=RX_RATE, init=False)
    carrier_spacing_hz: float = field(default=CARRIER_SPACING_HZ, init=False)
    fft_size: int = field(default=FFT_SIZE, init=False)
    cp_len: int = field(default=CP_LEN, init=False)

    def __post_init__(self):
        if self.bits_per_carrier != 3:
            raise ValueError("fmht4 is 8-PSK only")
        if self.fec_rate not in ldpc.INFORMATION_BITS:
            raise ValueError("unsupported FEC rate")
        if not 1 <= self.lo_bin < self.hi_bin < FFT_SIZE // 2:
            raise ValueError("active bins must lie inside the block")
        if not np.isfinite(self.lead_in_seconds) or self.lead_in_seconds < 0:
            raise ValueError("lead_in_seconds must be finite and nonnegative")
        if not np.isfinite(self.target_airtime_seconds) or self.target_airtime_seconds <= 0:
            raise ValueError("target_airtime_seconds must be finite and positive")
        if not isinstance(self.sync_symbols, int) or self.sync_symbols < 1:
            raise ValueError("sync_symbols must be an int >= 1")
        if not isinstance(self.pilot_comb_stride, int) or self.pilot_comb_stride < 2:
            raise ValueError("pilot_comb_stride must be an int >= 2")
        if not isinstance(self.pilot_time_span, int) or self.pilot_time_span < 1:
            raise ValueError("pilot_time_span must be an int >= 1")
        if not np.isfinite(self.papr_target_db) or self.papr_target_db < 3:
            raise ValueError("papr_target_db must be finite and at least 3 dB")
        if not isinstance(self.papr_iterations, int) or self.papr_iterations < 0:
            raise ValueError("papr_iterations must be a nonnegative int")
        if self.n_codewords is None:
            available = self.target_airtime_seconds - self.lead_in_seconds - 0.1
            symbols = (int(np.floor(available * RX_RATE / self.symbol_samples))
                       - self.preamble_symbols)
            object.__setattr__(self, "n_codewords",
                               symbols * self.bits_per_symbol // ldpc.N)
        if not isinstance(self.n_codewords, int) or self.n_codewords < 1:
            raise ValueError("frame budget cannot hold one codeword")
        if self.chunk_size <= 0 or self.max_payload_bytes > 65535:
            raise ValueError("payload capacity is outside native framing limits")

    # -- geometry ---------------------------------------------------------

    @cached_property
    def active_bins(self):
        return tuple(range(self.lo_bin, self.hi_bin + 1))

    @property
    def n_carriers(self):
        return len(self.active_bins)

    @cached_property
    def pilot_positions(self):
        n = self.n_carriers
        return np.array(sorted(set(range(0, n, self.pilot_comb_stride)) | {n - 1}))

    @cached_property
    def data_positions(self):
        pilot = set(self.pilot_positions.tolist())
        return np.array([i for i in range(self.n_carriers) if i not in pilot])

    @cached_property
    def pilot_values(self):
        return _newman(len(self.pilot_positions), root=1)

    @cached_property
    def tx_shape(self):
        """Per-bin transmit amplitude: a straight `tx_tilt_db` line, unit mean power."""
        if self.tx_tilt_db == 0:
            return np.ones(self.n_carriers)
        ramp = np.linspace(0.0, self.tx_tilt_db, self.n_carriers)
        shape = 10 ** (ramp / 20)
        return shape / np.sqrt(np.mean(shape ** 2))

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
    def preamble_symbols(self):
        return self.sync_symbols + 4  # sync burst plus four training symbols

    @property
    def total_symbols(self):
        return self.preamble_symbols + self.payload_symbols

    @property
    def symbol_samples(self):
        return FFT_SIZE + CP_LEN

    @cached_property
    def interleaver(self):
        return interleave.multiplicative(self.coded_bits, 8101)

    @cached_property
    def whitener(self):
        return bits.pn_bits(self.coded_bits, 0x17E35)

    @cached_property
    def points(self):
        return _points(self.bits_per_carrier)

    @cached_property
    def header(self):
        """Four sign-keyed sync symbols then four CAZAC training symbols.

        Every one of them is constant modulus with Newman phases, so the
        preamble -- the part that has to punch the squelch open and settle
        the receive AGC -- is the lowest-PAPR part of the whole frame.
        """
        sync = _newman(self.n_carriers, root=1)[None, :]
        signs = 1.0 - 2.0 * bits.pn_bits(self.sync_symbols, 0x4F1D).astype(float)
        training = np.vstack([_newman(self.n_carriers, root=r)
                              for r in (5, 7, 11, 13)])
        return np.vstack((signs[:, None] * sync, training))

    # -- modulation -------------------------------------------------------

    def _bodies(self, values, factor=1):
        core = FFT_SIZE * factor
        spectrum = np.zeros((len(values), core // 2 + 1), dtype=complex)
        spectrum[:, self.active_bins] = values
        return np.fft.irfft(spectrum, n=core, axis=1)

    def _guard(self, bodies, factor=1, guard=True):
        cp = CP_LEN * factor if guard else 0
        return (np.concatenate((bodies[:, -cp:], bodies), axis=1)
                if cp else bodies).reshape(-1)

    def _build(self, values, factor=1, guard=True):
        return self._guard(self._bodies(values, factor), factor, guard)

    def _clip_and_filter(self, bodies, factor):
        """Hold each block near `papr_target_db` without leaving the band.

        Clip in the oversampled time domain, then throw away everything the
        clip put outside the occupied bins and repeat. Clipping alone would
        splatter into the adjacent audio; filtering alone regrows the peaks.
        The in-band part of what survives is distortion the receiver sees as
        noise, which is the price this rung pays for being OFDM.
        """
        if self.papr_iterations == 0:
            return bodies
        core = FFT_SIZE * factor
        threshold = np.sqrt(np.mean(bodies ** 2)) * 10 ** (self.papr_target_db / 20)
        for _ in range(self.papr_iterations):
            bodies = np.clip(bodies, -threshold, threshold)
            spectrum = np.fft.rfft(bodies, axis=1)
            keep = np.zeros_like(spectrum)
            keep[:, self.active_bins] = spectrum[:, self.active_bins]
            bodies = np.fft.irfft(keep, n=core, axis=1)
        return bodies

    def _pack(self, payload):
        if len(payload) > self.max_payload_bytes:
            raise ValueError(f"payload too large for {self.name}: {len(payload)}")
        packet = (len(payload).to_bytes(2, "big") + payload
                  + (binascii.crc32(payload) & 0xffffffff).to_bytes(4, "big"))
        return packet.ljust(self.packet_bytes, b"\0")

    def _frame_values(self, payload):
        info = np.unpackbits(np.frombuffer(self._pack(payload), np.uint8))
        k = ldpc.INFORMATION_BITS[self.fec_rate]
        info = np.pad(info, (0, self.n_codewords * k - len(info)))
        coded = np.concatenate([ldpc.encode(row, self.fec_rate)
                                for row in info.reshape(self.n_codewords, k)])
        stream = self.interleaver.spread(coded) ^ self.whitener
        stream = np.pad(stream, (0, self.payload_symbols * self.bits_per_symbol - len(stream)))
        labels = stream.reshape(-1, self.bits_per_carrier)
        data_syms = self.points[labels @ (1 << np.arange(self.bits_per_carrier - 1, -1, -1))]
        values = np.empty((self.payload_symbols, self.n_carriers), dtype=complex)
        values[:, self.data_positions] = data_syms.reshape(
            self.payload_symbols, len(self.data_positions))
        values[:, self.pilot_positions] = self.pilot_values
        return np.vstack((self.header, values)) * self.tx_shape

    def encode(self, payload):
        values = self._frame_values(bytes(payload))
        bodies = self._clip_and_filter(self._bodies(values, TX_FACTOR), TX_FACTOR)
        symbols = self._guard(bodies, TX_FACTOR)
        lead = np.resize(self._build(self.header[:1], factor=TX_FACTOR, guard=False),
                         round(self.lead_in_seconds * TX_RATE)).copy()
        fade = min(FFT_SIZE, len(lead))
        lead[:fade] *= np.linspace(0, 1, fade)
        audio = np.concatenate((lead, symbols, np.zeros(TX_RATE // 10)))
        peak = np.max(np.abs(audio))
        return (audio / peak).astype(np.float32)

    def modulated_papr_db(self, payload):
        """PAPR of the keyed, modulated part of a frame -- what the limiter sees."""
        audio = self.encode(payload)
        head = round(self.lead_in_seconds * TX_RATE)
        return papr_db(audio[head:len(audio) - TX_RATE // 10])

    # -- demodulation -----------------------------------------------------

    def _extract(self, audio, start, count):
        if start < 0 or start + count * self.symbol_samples > len(audio):
            return None
        block = audio[start:start + count * self.symbol_samples].reshape(
            count, self.symbol_samples)
        return np.fft.rfft(block[:, CP_LEN:], axis=1)[:, self.active_bins]

    def decode(self, audio):
        result = {"synced": False, "payload": None, "crc_ok": False, "confidence": 0.0}
        audio = np.asarray(audio, dtype=float)
        if audio.ndim != 1 or not np.all(np.isfinite(audio)):
            return result
        reference = self._build(self.header[:self.sync_symbols])
        if len(audio) < 2 * len(reference):
            return result
        coarse = int(np.argmax(np.abs(
            correlate(hilbert(audio), hilbert(reference), mode="valid"))))
        best = None
        for offset in range(-CP_LEN * 2, CP_LEN // 2 + 1):
            start = coarse + offset
            observed = self._extract(
                audio, start + self.sync_symbols * self.symbol_samples, 4)
            if observed is not None:
                _, evm = _fit_channel(observed, self.header[self.sync_symbols:])
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
        block = self._extract(audio, start + self.sync_symbols * self.symbol_samples,
                              4 + self.payload_symbols)
        if block is None:
            return result
        # Per-bin equalisation measured from the preamble: the pre-emphasis
        # tilt and group-delay ripple are never modelled, only observed.
        channel, _ = _fit_channel(block[:4], self.header[self.sync_symbols:])
        safe = np.where(np.abs(channel) > 1e-12, channel, 1e-12)
        payload = block[4:]
        pilot_idx, data_idx, pv = self.pilot_positions, self.data_positions, self.pilot_values
        carriers = np.arange(self.n_carriers)
        # Drift of each payload symbol against the preamble-fitted channel,
        # read at the pilot bins. On this path that drift is soundcard clock
        # offset, not carrier offset: there is no carrier to recover.
        resid = payload[:, pilot_idx] / (pv * safe[pilot_idx])
        mag = np.abs(resid)
        phase = np.unwrap(np.angle(resid), axis=1)
        R = (np.array([np.interp(carriers, pilot_idx, row) for row in mag])
             * np.exp(1j * np.array([np.interp(carriers, pilot_idx, row) for row in phase])))
        if self.pilot_time_span > 1:
            kernel = np.ones(self.pilot_time_span)
            taps = np.convolve(np.ones(R.shape[0]), kernel, mode="same")[:, None]
            R = np.apply_along_axis(
                lambda col: np.convolve(col, kernel, mode="same"), 0, R) / taps
        equalized = payload[:, data_idx] / (safe[data_idx] * R[:, data_idx])
        if resid.shape[0] >= 2:
            noise_pilot = np.mean(np.abs(np.diff(resid, axis=0)) ** 2, axis=0) / 2
        else:
            noise_pilot = np.mean(np.abs(resid - np.mean(resid, axis=0)) ** 2, axis=0)
        noise = np.maximum(np.interp(carriers, pilot_idx, noise_pilot)[data_idx], 1e-5)
        llr = soft_bit_llrs(equalized.reshape(-1), self.bits_per_carrier,
                            np.tile(noise, self.payload_symbols),
                            mapper=_ofdm49_bits_to_symbols)[:self.coded_bits]
        llr = self.interleaver.gather(llr * (1 - 2 * self.whitener.astype(float)))
        info, _, ok = ldpc.decode_batch(llr.reshape(self.n_codewords, ldpc.N),
                                        rate=self.fec_rate)
        packet = np.packbits(info.reshape(-1)[:self.packet_bytes * 8]).tobytes()
        size = int.from_bytes(packet[:2], "big")
        decoded = packet[2:2 + size]
        crc = int.from_bytes(packet[2 + size:6 + size], "big")
        good = size <= self.max_payload_bytes and crc == (binascii.crc32(decoded) & 0xffffffff)
        result.update(payload=decoded if good else None, crc_ok=good, decoded_length=size,
                      codewords_ok=int(np.count_nonzero(ok)),
                      snr_db=float(10 * np.log10(np.median(1 / noise))),
                      carrier_snr_db=10 * np.log10(1 / noise),
                      end_index=start + self.total_symbols * self.symbol_samples)
        return result

    # -- description ------------------------------------------------------

    def airtime(self, payload_len):
        return (round(self.lead_in_seconds * TX_RATE)
                + self.total_symbols * self.symbol_samples * TX_FACTOR
                + TX_RATE // 10) / TX_RATE

    @property
    def bits_per_second(self):
        return self.chunk_size * 8 / self.airtime(self.max_payload_bytes)

    def geometry(self):
        return {"carrier_spacing_hz": CARRIER_SPACING_HZ, "carriers": self.n_carriers,
                "band_lo_hz": self.band_hz[0], "band_hi_hz": self.band_hz[1],
                "bits_per_carrier": self.bits_per_carrier, "cp_len": CP_LEN,
                "fec_rate": self.fec_rate, "lead_in_seconds": self.lead_in_seconds,
                "n_codewords": self.n_codewords,
                "pilot_comb_stride": self.pilot_comb_stride,
                "pilot_time_span": self.pilot_time_span,
                "sync_symbols": self.sync_symbols,
                "papr_target_db": self.papr_target_db,
                "tx_tilt_db": self.tx_tilt_db,
                "net_bps": self.bits_per_second}

    @property
    def band_hz(self) -> tuple[float, float]:
        return (self.lo_bin * CARRIER_SPACING_HZ, self.hi_bin * CARRIER_SPACING_HZ)

    @property
    def modulation(self) -> str:
        return (f"{self.n_carriers}-carrier 8PSK OFDM "
                f"({len(self.data_positions)} data, "
                f"{len(self.pilot_positions)} comb pilots), "
                f"clip-and-filter to {self.papr_target_db:.0f} dB PAPR")

    @property
    def fec(self) -> str:
        return f"QC-LDPC {self.fec_rate}"


def mode_for(**kwargs):
    return Fmht4Mode(**kwargs)


FMHT4 = Fmht4Mode()
MODES = (FMHT4,)
