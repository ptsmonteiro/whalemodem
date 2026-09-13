"""VF9/VF10 -- 50 Hz-spaced OFDM waveforms for analog FM channel radios.

Every parameter here was chosen from measurements on the real ic705/ht FM
bench rather than from a channel model; scripts/measure_ofdm_fm_burst.py and
scripts/hw_vf9_frames.py are the harnesses that produced them, and are what to
re-run after any change to the radios, levels, or antennas.

What the FM channel is, and what it is not. An FM discriminator hands back
baseband audio, so audio sent at 800 Hz arrives at 800 Hz: there is no carrier
frequency offset, which is the whole reason 50 Hz carrier spacing is usable at
all -- on HF/SSB a dial error of a few tens of Hz would destroy orthogonality
this fine. The two sound-card clocks differ by ~3.7 ppm
(scripts/measure_clock_offset.py), i.e. well under one sample of drift across a
6-second frame, and the path does not fade. So the receiver is deliberately
simple: acquire once, estimate the channel once, hold both for the whole frame.
The tracking an HF mode needs -- CFO search, per-symbol phase tracking,
differential encoding -- would cost margin and buy nothing here. Measured:
tracking a per-symbol common phase/gain term recovers only 0.1-1.4 dB, and
total phase drift across a frame is 5-15 degrees.

Why 500-2900 Hz. A multitone sweep suggested 300-3000 Hz was usable, but it
estimated noise from bins 25 Hz off the carrier grid, and on a 50 Hz grid the
third-order intermodulation products land ON the grid -- so it measured a noise
floor while the carriers sat on an IMD floor, reading 15-25 dB where real OFDM
delivers 6-16 dB. Measured with actual OFDM symbols, 300-450 Hz is dead in both
directions (-9 to +2 dB, raw BER 0.1-0.44): that is the radios' audio
high-pass, where group delay climbs steeply and the impulse response outruns
the guard interval. The top is limited by the ht's receiver, down to 6.8 dB at
2950 Hz and 5.6 dB at 3000 Hz. Between 500 and 2900 Hz raw QPSK bit errors were
zero in both directions.

Why the drive is what it is. This path is intermodulation-limited, not
noise-limited: backing the transmit peak off from 0.9 to 0.4 of full scale
RAISED worst-leg per-carrier SNR from 6.7 to 11.4 dB. Driving an FM
transmitter harder buys deviation and loses linearity, and past a point the
second costs more than the first is worth.

Why the guard interval is 36 samples. Not delay spread -- this path has
essentially none. It is our own receiver: rx_audio's anti-alias FIR is 129 taps
at 48 kHz, spanning ~32 samples at 12 kHz, and a guard shorter than that leaks
one symbol into the next on a noiseless loopback.

Two modes share the waveform and differ only in how many bits each carrier
takes, which is the one thing worth trading: VF9 (QPSK) is the one to trust,
VF10 (8PSK) the one to prefer when the path supports it.
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass
from functools import cached_property

import numpy as np
from scipy.signal import correlate, hilbert

from whale import framing, rx_audio
from whale.dsp import bits as _bits
from whale.dsp import interleave as _interleave
from whale.dsp import ldpc as _ldpc

SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
DECIMATION = rx_audio.DECIMATION

CARRIER_SPACING_HZ = 50.0
CORE_SAMPLES = int(SAMPLE_RATE / CARRIER_SPACING_HZ)        # 960
RX_CORE_SAMPLES = int(RX_SAMPLE_RATE / CARRIER_SPACING_HZ)  # 240
RX_GUARD_SAMPLES = 36
GUARD_SAMPLES = RX_GUARD_SAMPLES * DECIMATION               # 144
SYMBOL_SAMPLES = CORE_SAMPLES + GUARD_SAMPLES
RX_SYMBOL_SAMPLES = RX_CORE_SAMPLES + RX_GUARD_SAMPLES

# Bin index is frequency / 50 Hz at BOTH rates, so the same indices mean the
# same frequencies on the transmit and receive sides. 10..58 -> 500..2900 Hz.
CARRIER_BINS = np.arange(10, 59)
N_CARRIERS = len(CARRIER_BINS)

FEC_RATE = "3/4"
LENGTH_BYTES = 2
CRC_BYTES = 4

SYNC_SYMBOLS = 4        # repeats of one known symbol; what acquisition locks to
ESTIMATE_SYMBOLS = 4    # known symbols the per-carrier channel estimate is fitted to
HEADER_SYMBOLS = SYNC_SYMBOLS + ESTIMATE_SYMBOLS

SYNC_SEED = 0x5104
LEADIN_SEED = 0x1EAD
ESTIMATE_SEED = 0xE57A
WHITENER_SEED = 0x17E35
INTERLEAVER_STEP = 8101

# The sync block is the sync symbol repeated under these signs rather than
# plainly tiled. Plain tiling made the lead-in -- itself a repeated symbol, to
# hold the far end's squelch open -- correlate just as well as the sync block,
# anywhere along its full second. Acquisition then locked somewhere in the
# lead-in, thousands of samples from the truth and far outside the fine-search
# window: measured 0/3 frames one direction, median per-carrier SNR -4.8 dB. A
# sign pattern gives the correlation one unambiguous peak (3.5x the best
# lead-in match), and no uniform repetition of anything can match it.
SYNC_SIGNS = np.array([1.0, 1.0, -1.0, 1.0])

TX_RMS = 0.096
# Rare peaks are CLIPPED, not scaled away. Scaling the frame so its single
# worst sample fits would drop RMS -- and every carrier's SNR with it -- by
# ~2.6 dB across a 5.8 s frame, to buy headroom for a handful of samples the
# radio's own deviation limiter would clip anyway.
MAX_SAMPLE = 0.85

LEAD_IN_SAMPLES = int(np.ceil(framing.HEAD_SECONDS * SAMPLE_RATE))
LEAD_IN_FADE_SAMPLES = 240
TAIL_SAMPLES = 960

# Acquisition correlates on the analytic signal, so its peak sits late by the
# whole path impulse response. That delay belongs to the channel and is
# absorbed as a per-carrier phase ramp: advancing the FFT window to
# "compensate" runs it into the next symbol and manufactures ISI. So search
# backwards from the peak and let the header fit choose.
SEARCH_BACK = 56
SEARCH_FORWARD = 8

CONFIDENCE_THRESHOLD = 0.7


def _constellation(bits_per_carrier: int) -> np.ndarray:
    """Unit-magnitude Gray constellation, indexed by the integer its bits form.

    Both sizes are unit magnitude, so one channel estimate and one set of
    reliability weights serve either without rescaling.

    QPSK's float cast is load-bearing: on a uint8 array `1 - 2*b` wraps 1 to
    255 instead of -1, leaving the constellation at {0.707, 180.3}. That stays
    self-consistent through modulation and equalisation -- a perfect channel
    fit, zero EVM -- and surfaces only as a BER near 0.5, because the
    demapper's sign test can never fire when both points are positive.
    """
    if bits_per_carrier == 2:
        pairs = np.array([[(v >> 1) & 1, v & 1] for v in range(4)],
                         dtype=np.float64)
        return ((1.0 - 2.0 * pairs[:, 0])
                + 1j * (1.0 - 2.0 * pairs[:, 1])) / np.sqrt(2.0)
    if bits_per_carrier == 3:
        # Gray 8PSK: adjacent angles differ in exactly one bit, so the most
        # likely error -- slipping to a neighbouring angle -- costs one bit.
        angles = np.arange(8)
        points = np.zeros(8, dtype=complex)
        points[angles ^ (angles >> 1)] = np.exp(2j * np.pi * angles / 8)
        return points
    raise ValueError(f"unsupported constellation size: {bits_per_carrier}")


def _map_bits(bits: np.ndarray, points: np.ndarray,
              bits_per_carrier: int) -> np.ndarray:
    groups = bits.reshape(-1, bits_per_carrier).astype(np.int64)
    place = 1 << np.arange(bits_per_carrier - 1, -1, -1)
    return points[groups @ place]


def _soft_bits(values: np.ndarray, weights: np.ndarray, points: np.ndarray,
               bits_per_carrier: int) -> np.ndarray:
    """Max-log LLRs for the LDPC decoder: positive means bit zero.

    Matches whale/dsp/ldpc.py's sign convention. Each carrier is scaled by its
    own measured reliability, which is the point of carrying a per-carrier
    channel estimate at all -- a notch at 1450 Hz should not argue as loudly as
    a clean carrier at 1500 Hz.
    """
    distance = np.abs(values[..., None] - points[None, None, :]) ** 2
    masks = ((np.arange(len(points))[:, None]
              >> np.arange(bits_per_carrier - 1, -1, -1)) & 1).astype(bool)
    llr = np.empty(values.shape + (bits_per_carrier,), dtype=np.float64)
    for bit in range(bits_per_carrier):
        llr[..., bit] = (distance[..., masks[:, bit]].min(axis=-1)
                         - distance[..., ~masks[:, bit]].min(axis=-1))
    return (llr * weights[None, :, None]).reshape(-1)


_QPSK_POINTS = _constellation(2)


def _known_values(seed: int, symbols: int) -> np.ndarray:
    """Header symbols are always QPSK, whatever the data carries -- the header
    has to survive on the worst path the mode is ever used on."""
    return _map_bits(_bits.pn_bits(symbols * N_CARRIERS * 2, seed),
                     _QPSK_POINTS, 2).reshape(symbols, N_CARRIERS)


SYNC_VALUES = _known_values(SYNC_SEED, 1)[0]
LEADIN_VALUES = _known_values(LEADIN_SEED, 1)[0]
ESTIMATE_VALUES = _known_values(ESTIMATE_SEED, ESTIMATE_SYMBOLS)
SYNC_BLOCK = SYNC_SIGNS[:, None] * SYNC_VALUES[None, :]
HEADER_VALUES = np.vstack([SYNC_BLOCK, ESTIMATE_VALUES])


def _build_symbols(values: np.ndarray, core: int, guard: int) -> np.ndarray:
    out = np.empty((len(values), core + guard))
    for i, row in enumerate(values):
        spectrum = np.zeros(core // 2 + 1, dtype=complex)
        spectrum[CARRIER_BINS] = row
        body = np.fft.irfft(spectrum, core)
        if guard:
            out[i, :guard] = body[-guard:]   # body[-0:] is the whole array
        out[i, guard:] = body
    return out.reshape(-1)


def _sync_reference() -> np.ndarray:
    return _build_symbols(SYNC_BLOCK, RX_CORE_SAMPLES, RX_GUARD_SAMPLES)


def _extract(audio: np.ndarray, start: int, symbols: int):
    if start < 0 or start + symbols * RX_SYMBOL_SAMPLES > len(audio):
        return None
    out = np.empty((symbols, N_CARRIERS), dtype=complex)
    for i in range(symbols):
        base = start + i * RX_SYMBOL_SAMPLES + RX_GUARD_SAMPLES
        out[i] = np.fft.rfft(audio[base:base + RX_CORE_SAMPLES],
                             RX_CORE_SAMPLES)[CARRIER_BINS]
    return out


def _fit_channel(observed: np.ndarray, reference: np.ndarray):
    channel = (np.sum(observed * np.conj(reference), axis=0)
               / np.sum(np.abs(reference) ** 2, axis=0))
    modelled = channel * reference
    residual = float(np.mean(np.abs(observed - modelled) ** 2))
    signal = float(np.mean(np.abs(modelled) ** 2))
    return channel, (residual / signal if signal > 0 else np.inf)


@dataclass(frozen=True)
class Vf9Mode:
    """A VF9-family waveform, satisfying whale/waveform.py's WaveformMode.

    Only `bits_per_carrier` and `n_codewords` distinguish the family members;
    everything else -- geometry, header, drive, acquisition -- is shared, so
    the two modes cannot drift apart in the ways that matter for acquisition.
    """

    name: str
    mode_id: int
    bits_per_carrier: int
    n_codewords: int
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    tx_sample_rate: int = SAMPLE_RATE
    rx_sample_rate: int = RX_SAMPLE_RATE
    # Squelch-open and AGC-settling budget, measured on this pair rather than
    # inherited. framing.HEAD_SECONDS is 1.0 s, sized against a radio that
    # blacks out ~110 ms after squelch opens; here the lead-in must also let
    # the far end's AGC settle before the channel estimate is taken, since
    # that one estimate is held for the whole frame.
    #
    # Swept on the air: 0.5 s scored 12/12 frames against 1.0 s's 9/12, with
    # identical acquisition confidence (0.95-0.97) and per-carrier SNR
    # (13-16 dB) -- i.e. settling is complete well before 0.5 s, and 1.0 s
    # was simply spending 0.5 s of airtime on nothing. Below that it falls off
    # a cliff: ic705->ht scored 0/3 at both 0.30 s and 0.15 s. So the real
    # requirement is between 0.3 and 0.5 s and this leaves ~1.7x margin.
    #
    # That margin is specific to these two radios. A radio with slower squelch
    # -- the one framing.HEAD_SECONDS was sized for -- would need more, so
    # this is a parameter and not a constant.
    lead_in_seconds: float = 0.5

    @cached_property
    def lead_in_samples(self) -> int:
        return int(np.ceil(self.lead_in_seconds * SAMPLE_RATE))

    @cached_property
    def points(self) -> np.ndarray:
        return _constellation(self.bits_per_carrier)

    @cached_property
    def bits_per_symbol(self) -> int:
        return N_CARRIERS * self.bits_per_carrier

    @cached_property
    def packet_bytes(self) -> int:
        return (self.n_codewords * _ldpc.INFORMATION_BITS[FEC_RATE]) // 8

    @cached_property
    def chunk_size(self) -> int:
        return self.packet_bytes - LENGTH_BYTES - CRC_BYTES

    @cached_property
    def coded_bits(self) -> int:
        return self.n_codewords * _ldpc.N

    @cached_property
    def payload_symbols(self) -> int:
        return int(np.ceil(self.coded_bits / self.bits_per_symbol))

    @cached_property
    def total_symbols(self) -> int:
        return HEADER_SYMBOLS + self.payload_symbols

    @cached_property
    def interleaver(self):
        return _interleave.multiplicative(self.coded_bits, INTERLEAVER_STEP)

    @cached_property
    def whitener(self) -> np.ndarray:
        return _bits.pn_bits(self.coded_bits, WHITENER_SEED)

    # -- framing --------------------------------------------------------

    def _pack(self, payload: bytes) -> bytes:
        if len(payload) > self.chunk_size:
            raise ValueError(f"payload too large for {self.name}: "
                             f"{len(payload)} > {self.chunk_size}")
        packet = bytearray(self.packet_bytes)
        packet[0:LENGTH_BYTES] = len(payload).to_bytes(LENGTH_BYTES, "big")
        packet[LENGTH_BYTES:LENGTH_BYTES + len(payload)] = payload
        crc_at = LENGTH_BYTES + len(payload)
        packet[crc_at:crc_at + CRC_BYTES] = (
            binascii.crc32(payload) & 0xFFFFFFFF).to_bytes(CRC_BYTES, "big")
        return bytes(packet)

    def _unpack(self, packet: bytes) -> tuple[bytes | None, dict]:
        length = int.from_bytes(packet[:LENGTH_BYTES], "big")
        meta = {"decoded_length": length, "crc_ok": False}
        if length > self.chunk_size:
            meta["failure"] = "invalid length"
            return None, meta
        payload = packet[LENGTH_BYTES:LENGTH_BYTES + length]
        crc_at = LENGTH_BYTES + length
        received = int.from_bytes(packet[crc_at:crc_at + CRC_BYTES], "big")
        computed = binascii.crc32(payload) & 0xFFFFFFFF
        meta.update(received_crc32=received, computed_crc32=computed,
                    crc_ok=received == computed)
        if not meta["crc_ok"]:
            meta["failure"] = "CRC mismatch"
            return None, meta
        return payload, meta

    # -- transmit -------------------------------------------------------

    def payload_values(self, payload: bytes) -> np.ndarray:
        """The constellation values this payload puts on the carriers.

        Exposed because a receiver that knows the payload can compare against
        these over every payload symbol, rather than over the handful of
        header symbols -- roughly fifty times the samples, and so a far
        steadier per-carrier SNR estimate. That matters for anything planning
        a bit-loading map: see whale/modes/vf11.py.
        """
        info = np.unpackbits(np.frombuffer(self._pack(payload), dtype=np.uint8))
        k = _ldpc.INFORMATION_BITS[FEC_RATE]
        padded = np.zeros(self.n_codewords * k, dtype=np.uint8)
        padded[:len(info)] = info
        coded = np.concatenate([_ldpc.encode(row, FEC_RATE)
                                for row in padded.reshape(self.n_codewords, k)])

        coded = self.interleaver.spread(coded) ^ self.whitener
        needed = self.payload_symbols * self.bits_per_symbol
        if len(coded) < needed:
            coded = np.concatenate(
                [coded, np.zeros(needed - len(coded), dtype=np.uint8)])
        return _map_bits(coded, self.points, self.bits_per_carrier).reshape(
            self.payload_symbols, N_CARRIERS)

    def encode(self, payload: bytes) -> np.ndarray:
        values = self.payload_values(payload)
        symbols = _build_symbols(np.vstack([HEADER_VALUES, values]),
                                 CORE_SAMPLES, GUARD_SAMPLES)

        # Lead-in is one symbol's bare core repeated -- same spectrum and crest
        # factor as the frame, so the far end's squelch opens and its AGC
        # settles on the signal it then has to measure. It is deliberately NOT
        # the sync symbol; see SYNC_SIGNS. framing.HEAD_SECONDS sizes it
        # against a radio measured to black out ~110 ms after squelch opens.
        core = _build_symbols(LEADIN_VALUES[None, :], CORE_SAMPLES, 0)
        lead = np.resize(core, self.lead_in_samples).copy()
        fade = min(LEAD_IN_FADE_SAMPLES, len(lead))
        lead[:fade] *= np.linspace(0.0, 1.0, fade)

        audio = np.concatenate([lead, symbols, np.zeros(TAIL_SAMPLES)])
        rms = np.sqrt(np.mean(audio ** 2))
        if rms > 0:
            audio *= TX_RMS / rms
        return np.clip(audio, -MAX_SAMPLE, MAX_SAMPLE).astype(np.float32)

    # -- receive --------------------------------------------------------

    def decode(self, audio: np.ndarray) -> dict:
        result = {"synced": False, "payload": None, "confidence": 0.0}
        audio = np.asarray(audio, dtype=np.float64)
        reference = _sync_reference()
        if len(audio) < len(reference) * 2:
            return result

        corr = np.abs(correlate(hilbert(audio), hilbert(reference),
                                mode="valid"))
        coarse = int(np.argmax(corr))

        best = None
        for offset in range(-SEARCH_BACK, SEARCH_FORWARD + 1):
            start = coarse + offset
            observed = _extract(audio, start + SYNC_SYMBOLS * RX_SYMBOL_SAMPLES,
                                ESTIMATE_SYMBOLS)
            if observed is None:
                continue
            _, evm = _fit_channel(observed, ESTIMATE_VALUES)
            if best is None or evm < best[1]:
                best = (start, evm)
        if best is None:
            return result
        start, evm = best
        result.update(confidence=1.0 / (1.0 + evm), start_index=start)

        block = _extract(audio, start + SYNC_SYMBOLS * RX_SYMBOL_SAMPLES,
                         ESTIMATE_SYMBOLS + self.payload_symbols)
        if block is None:
            # A credible header whose payload has not all arrived: report the
            # sync so the caller keeps the buffer, but claim no end_index.
            return result

        channel, _ = _fit_channel(block[:ESTIMATE_SYMBOLS], ESTIMATE_VALUES)
        safe = np.where(np.abs(channel) > 1e-12, channel, 1e-12)
        equalised = block[ESTIMATE_SYMBOLS:] / safe

        noise = np.mean(np.abs(block[:ESTIMATE_SYMBOLS] / safe
                               - ESTIMATE_VALUES) ** 2, axis=0)
        carrier_snr = 1.0 / np.maximum(noise, 1e-12)
        weights = carrier_snr / np.median(carrier_snr)

        # Undo whitening on the soft values -- a whitening bit of 1 inverted
        # the bit, which flips the sign of its LLR -- then undo the interleave.
        llr = _soft_bits(equalised, weights, self.points,
                         self.bits_per_carrier)[:self.coded_bits]
        llr = self.interleaver.gather(
            llr * (1.0 - 2.0 * self.whitener.astype(np.float64)))

        info, _iterations, ok = _ldpc.decode_batch(
            llr.reshape(self.n_codewords, _ldpc.N), rate=FEC_RATE)
        packet = np.packbits(
            info.reshape(-1)[:self.packet_bytes * 8].astype(np.uint8)).tobytes()
        payload, meta = self._unpack(packet)

        result.update(
            synced=True,
            payload=payload,
            snr_db=float(10 * np.log10(np.median(carrier_snr))),
            carrier_snr_db=10 * np.log10(carrier_snr),
            codewords_ok=int(np.count_nonzero(ok)),
            end_index=start + self.total_symbols * RX_SYMBOL_SAMPLES,
            **meta)
        return result

    def airtime(self, payload_len: int) -> float:
        return (self.lead_in_samples + self.total_symbols * SYMBOL_SAMPLES
                + TAIL_SAMPLES) / SAMPLE_RATE

    @property
    def bits_per_second(self) -> float:
        """Payload bits per second of air time, lead-in included -- the number
        worth comparing between modes."""
        return self.chunk_size * 8 / self.airtime(self.chunk_size)


# 30 and 45 codewords put both modes at the same ~5.8 s frame, so the only
# thing that differs when comparing them on the air is bits per carrier.
VF9 = Vf9Mode(name="vf9", mode_id=9, bits_per_carrier=2, n_codewords=30)
VF10 = Vf9Mode(name="vf10", mode_id=11, bits_per_carrier=3, n_codewords=45)
MODES = (VF9, VF10)
