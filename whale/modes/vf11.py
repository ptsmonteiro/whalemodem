"""VF11 -- per-carrier adaptive bit-loaded OFDM for analog FM channel radios.

Same waveform geometry as VF9, which is hardware-validated on the ic705/ht
bench: 50 Hz carrier spacing, 960-point IFFT at 48 kHz on transmit, 240-point
FFT at 12 kHz on receive, 36-sample guard, 49 carriers on bins 10..58
(500-2900 Hz). Acquisition, channel estimation and framing are imported from
vf9 rather than reimplemented, so the two modes cannot drift apart in the
parts that were expensive to get right.

The one thing that differs is the only thing worth differing: each carrier
gets its OWN constellation, chosen from that carrier's measured SNR.

Why. Measured per-carrier SNR on this path spans roughly 6-18 dB across the
49 carriers -- the band is not flat and its notches are stable. Under uniform
modulation that spread is pure loss at both ends: the strong carriers are
throttled to whatever the weak ones can survive, while the weak ones still
produce nearly all the bit errors. VF9 rides this out by choosing QPSK, which
the weak carriers can just about carry. Uniform 8PSK was the same experiment
run one notch harder and it failed outright -- 0/10 frames, with LDPC
codewords landing anywhere between 0 and 41 of 45 -- not because the median
carrier could not carry 8PSK, but because enough weak carriers fed the decoder
unreliable bits to sink whole codewords. Bit loading is the direct answer: let
the 16 dB carriers carry 16-QAM and let the 7 dB carriers carry BPSK, or
nothing.

On the thresholds. Textbook required-SNR figures for these constellations sit
about 4 dB below what this path actually needs -- they predict uniform 8PSK at
rate 3/4 should work around 11-12 dB, and it demonstrably did not at a median
of 13-16 dB. The defaults in THRESHOLDS_DB are therefore deliberately
pessimistic against theory and calibrated against that failure; `margin_db`
shifts them all at once so the real operating point can be found on the air
rather than argued from a table. Negative margin is more aggressive.

How the receiver knows the map. For now it does not: the map is a parameter
both ends are given, which is enough to measure what bit loading is worth over
a real link (sound the channel with a VF9 frame, compute a map, run VF11 with
it). Making this a self-contained mode needs the map to reach the receiver
BEFORE payload demapping is possible, which rules out the link's air header --
that header is full, and it is parsed after demodulation anyway. The intended
route is a small preset index carried in VF11's own header symbols, indexing a
shared table, the way mode ids already index ModeRegistry.
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass

import numpy as np
from scipy.signal import correlate, hilbert

from whale.dsp import bits as _bits
from whale.dsp import interleave as _interleave
from whale.dsp import ldpc as _ldpc
from whale.modes import vf9
from whale.phy.ofdm49 import _constellation_table, _soft_bit_llrs
from whale.phy.sc import bits_to_symbols

# Geometry, acquisition and header all come from VF9 unchanged.
CARRIER_BINS = vf9.CARRIER_BINS
N_CARRIERS = vf9.N_CARRIERS
RX_SYMBOL_SAMPLES = vf9.RX_SYMBOL_SAMPLES
SYNC_SYMBOLS = vf9.SYNC_SYMBOLS
ESTIMATE_SYMBOLS = vf9.ESTIMATE_SYMBOLS
HEADER_SYMBOLS = vf9.HEADER_SYMBOLS

PAYLOAD_SYMBOLS = 199            # same ~5.8 s frame as VF9, so rates compare
FEC_RATE = "3/4"
LENGTH_BYTES = 2
CRC_BYTES = 4
WHITENER_SEED = 0x2C71
FILLER_SEED = 0x6B3D
INTERLEAVER_STEP = 8101

MAX_BITS_PER_CARRIER = 4         # 16-QAM; 64-QAM needs SNR this path never shows

# Per-carrier SNR (dB) required to carry each constellation at LDPC 3/4.
# Pessimistic against theory on purpose -- see the module docstring.
THRESHOLDS_DB = ((16.0, 4), (14.0, 3), (9.0, 2), (6.0, 1))


def plan_bits(snr_db, margin_db: float = 0.0, max_bits: int = MAX_BITS_PER_CARRIER):
    """Bits per carrier from measured per-carrier SNR.

    A carrier below the lowest threshold gets 0 and is left empty rather than
    being given BPSK it cannot carry -- an unusable carrier that still spends
    power is worse than one that spends none, because it feeds the decoder
    confident-looking nonsense.
    """
    snr_db = np.asarray(snr_db, dtype=float)
    bits = np.zeros(len(snr_db), dtype=int)
    for threshold, value in THRESHOLDS_DB:
        if value > max_bits:
            continue
        bits = np.where((bits == 0) & (snr_db >= threshold + margin_db),
                        value, bits)
    return bits


@dataclass(frozen=True)
class BitLoadedMode:
    """A VF11 instance bound to one bit map.

    The map is fixed at construction because almost every derived size --
    codeword count, packet size, payload capacity -- follows from it.
    """

    bit_map: tuple
    name: str = "vf11"
    mode_id: int = 12
    confidence_threshold: float = vf9.CONFIDENCE_THRESHOLD
    tx_sample_rate: int = vf9.SAMPLE_RATE
    rx_sample_rate: int = vf9.RX_SAMPLE_RATE

    # -- derived geometry ----------------------------------------------

    @property
    def bits(self) -> np.ndarray:
        return np.asarray(self.bit_map, dtype=int)

    @property
    def bits_per_symbol(self) -> int:
        return int(self.bits.sum())

    @property
    def active(self) -> np.ndarray:
        return np.flatnonzero(self.bits > 0)

    @property
    def offsets(self) -> np.ndarray:
        """First bit position within an OFDM symbol for each carrier."""
        return np.concatenate([[0], np.cumsum(self.bits)])[:-1]

    @property
    def n_codewords(self) -> int:
        # floor, not ceil: a codeword that would be only part-filled is not
        # sent at all. Padding the tail with zero-valued LLRs would decode as
        # the all-zero codeword, which satisfies every parity check and
        # reports ok=True having recovered nothing.
        return (PAYLOAD_SYMBOLS * self.bits_per_symbol) // _ldpc.N

    @property
    def coded_bits(self) -> int:
        return self.n_codewords * _ldpc.N

    @property
    def packet_bytes(self) -> int:
        return (self.n_codewords * _ldpc.INFORMATION_BITS[FEC_RATE]) // 8

    @property
    def chunk_size(self) -> int:
        return self.packet_bytes - LENGTH_BYTES - CRC_BYTES

    @property
    def interleaver(self):
        return _interleave.multiplicative(self.coded_bits, INTERLEAVER_STEP)

    @property
    def whitener(self) -> np.ndarray:
        return _bits.pn_bits(self.coded_bits, WHITENER_SEED)

    def _groups(self):
        """(bits_per_carrier, carrier indices, bit positions) per constellation.

        Carriers sharing a constellation are demapped in one call --
        whale/phy/ofdm49.py's mapper and demapper hold no cross-call state, so
        grouping is safe and keeps the per-symbol work vectorised.
        """
        for value in sorted(set(self.bits[self.bits > 0])):
            carriers = np.flatnonzero(self.bits == value)
            positions = (self.offsets[carriers][:, None]
                         + np.arange(value)[None, :])
            yield int(value), carriers, positions

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

    def _unpack(self, packet: bytes):
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

    def encode(self, payload: bytes) -> np.ndarray:
        info = np.unpackbits(np.frombuffer(self._pack(payload), dtype=np.uint8))
        k = _ldpc.INFORMATION_BITS[FEC_RATE]
        padded = np.zeros(self.n_codewords * k, dtype=np.uint8)
        padded[:len(info)] = info
        coded = np.concatenate([_ldpc.encode(row, FEC_RATE)
                                for row in padded.reshape(self.n_codewords, k)])
        stream = self.interleaver.spread(coded) ^ self.whitener

        # Slots past the last whole codeword carry deterministic filler, so
        # every transmitted carrier holds a real symbol and the receiver can
        # simply ignore the tail rather than decode an empty block.
        total = PAYLOAD_SYMBOLS * self.bits_per_symbol
        if total > len(stream):
            stream = np.concatenate(
                [stream, _bits.pn_bits(total - len(stream), FILLER_SEED)])

        grid = stream.reshape(PAYLOAD_SYMBOLS, self.bits_per_symbol)
        values = np.zeros((PAYLOAD_SYMBOLS, N_CARRIERS), dtype=complex)
        for value, carriers, positions in self._groups():
            chunk = grid[:, positions]                       # (sym, carrier, bits)
            symbols = bits_to_symbols(chunk.reshape(-1).astype(np.uint8), value)
            values[:, carriers] = symbols.reshape(PAYLOAD_SYMBOLS, len(carriers))

        symbols = vf9._build_symbols(np.vstack([vf9.HEADER_VALUES, values]),
                                     vf9.CORE_SAMPLES, vf9.GUARD_SAMPLES)
        core = vf9._build_symbols(vf9.LEADIN_VALUES[None, :], vf9.CORE_SAMPLES, 0)
        lead = np.resize(core, vf9.LEAD_IN_SAMPLES).copy()
        lead[:vf9.LEAD_IN_FADE_SAMPLES] *= np.linspace(
            0.0, 1.0, vf9.LEAD_IN_FADE_SAMPLES)

        audio = np.concatenate([lead, symbols, np.zeros(vf9.TAIL_SAMPLES)])
        rms = np.sqrt(np.mean(audio ** 2))
        if rms > 0:
            audio *= vf9.TX_RMS / rms
        return np.clip(audio, -vf9.MAX_SAMPLE, vf9.MAX_SAMPLE).astype(np.float32)

    # -- receive --------------------------------------------------------

    def decode(self, audio: np.ndarray) -> dict:
        result = {"synced": False, "payload": None, "confidence": 0.0}
        audio = np.asarray(audio, dtype=np.float64)
        reference = vf9._sync_reference()
        if len(audio) < len(reference) * 2:
            return result

        corr = np.abs(correlate(hilbert(audio), hilbert(reference),
                                mode="valid"))
        coarse = int(np.argmax(corr))

        best = None
        for offset in range(-vf9.SEARCH_BACK, vf9.SEARCH_FORWARD + 1):
            start = coarse + offset
            observed = vf9._extract(audio,
                                    start + SYNC_SYMBOLS * RX_SYMBOL_SAMPLES,
                                    ESTIMATE_SYMBOLS)
            if observed is None:
                continue
            _, evm = vf9._fit_channel(observed, vf9.ESTIMATE_VALUES)
            if best is None or evm < best[1]:
                best = (start, evm)
        if best is None:
            return result
        start, evm = best
        result.update(confidence=1.0 / (1.0 + evm), start_index=start)

        block = vf9._extract(audio, start + SYNC_SYMBOLS * RX_SYMBOL_SAMPLES,
                             ESTIMATE_SYMBOLS + PAYLOAD_SYMBOLS)
        if block is None:
            return result

        channel, _ = vf9._fit_channel(block[:ESTIMATE_SYMBOLS],
                                      vf9.ESTIMATE_VALUES)
        safe = np.where(np.abs(channel) > 1e-12, channel, 1e-12)
        equalised = block[ESTIMATE_SYMBOLS:] / safe

        # Noise variance per carrier, in the constellations' own units: the
        # tables are unit mean power, so nv is simply 1/SNR.
        noise = np.mean(np.abs(block[:ESTIMATE_SYMBOLS] / safe
                               - vf9.ESTIMATE_VALUES) ** 2, axis=0)
        noise = np.maximum(noise, 1e-12)
        carrier_snr = 1.0 / noise

        llr_grid = np.zeros((PAYLOAD_SYMBOLS, self.bits_per_symbol))
        for value, carriers, positions in self._groups():
            received = equalised[:, carriers].reshape(-1)
            nv = np.repeat(noise[carriers][None, :], PAYLOAD_SYMBOLS,
                           axis=0).reshape(-1)
            llrs = _soft_bit_llrs(received, value, nv)
            llr_grid[:, positions] = llrs.reshape(PAYLOAD_SYMBOLS,
                                                  len(carriers), value)

        stream = llr_grid.reshape(-1)[:self.coded_bits]
        stream = self.interleaver.gather(
            stream * (1.0 - 2.0 * self.whitener.astype(np.float64)))

        info, _iterations, ok = _ldpc.decode_batch(
            stream.reshape(self.n_codewords, _ldpc.N), rate=FEC_RATE)
        packet = np.packbits(
            info.reshape(-1)[:self.packet_bytes * 8].astype(np.uint8)).tobytes()
        payload, meta = self._unpack(packet)

        result.update(
            synced=True,
            payload=payload,
            snr_db=float(10 * np.log10(np.median(carrier_snr))),
            carrier_snr_db=10 * np.log10(carrier_snr),
            codewords_ok=int(np.count_nonzero(ok)),
            end_index=start + (HEADER_SYMBOLS + PAYLOAD_SYMBOLS)
            * RX_SYMBOL_SAMPLES,
            **meta)
        return result

    def airtime(self, payload_len: int) -> float:
        return (vf9.LEAD_IN_SAMPLES
                + (HEADER_SYMBOLS + PAYLOAD_SYMBOLS) * vf9.SYMBOL_SAMPLES
                + vf9.TAIL_SAMPLES) / vf9.SAMPLE_RATE

    @property
    def bits_per_second(self) -> float:
        return self.chunk_size * 8 / self.airtime(self.chunk_size)

    def describe(self) -> str:
        counts = {b: int(np.count_nonzero(self.bits == b)) for b in range(5)}
        spread = " ".join(f"{b}b:{counts[b]}" for b in range(5) if counts[b])
        return (f"{self.name} [{spread}] {self.bits_per_symbol} bits/sym, "
                f"{self.n_codewords} codewords, {self.chunk_size} B, "
                f"{self.bits_per_second:.0f} bps")


def sound(audio, payload, mode=None):
    """Per-carrier SNR from a received frame whose payload is known.

    Measures against every payload symbol rather than the few header symbols
    the ordinary decode path uses. That is not a refinement, it is the
    difference between a map that helps and one that hurts: a per-carrier SNR
    fitted to 4 symbols carries several dB of standard error, so carriers that
    happen to measure high get handed constellations they cannot actually
    support, and the resulting errors concentrate badly enough to sink whole
    codewords. Planning from ~200 symbols instead of 4 removes that.

    Returns per-carrier SNR in dB, or None if the frame could not be found.
    """
    mode = vf9.VF9 if mode is None else mode
    audio = np.asarray(audio, dtype=np.float64)
    reference = vf9._sync_reference()
    if len(audio) < len(reference) * 2:
        return None

    corr = np.abs(correlate(hilbert(audio), hilbert(reference), mode="valid"))
    coarse = int(np.argmax(corr))

    best = None
    for offset in range(-vf9.SEARCH_BACK, vf9.SEARCH_FORWARD + 1):
        start = coarse + offset
        observed = vf9._extract(audio, start + SYNC_SYMBOLS * RX_SYMBOL_SAMPLES,
                                ESTIMATE_SYMBOLS)
        if observed is None:
            continue
        _, evm = vf9._fit_channel(observed, vf9.ESTIMATE_VALUES)
        if best is None or evm < best[1]:
            best = (start, evm)
    if best is None:
        return None

    block = vf9._extract(audio, best[0] + SYNC_SYMBOLS * RX_SYMBOL_SAMPLES,
                         ESTIMATE_SYMBOLS + mode.payload_symbols)
    if block is None:
        return None

    channel, _ = vf9._fit_channel(block[:ESTIMATE_SYMBOLS], vf9.ESTIMATE_VALUES)
    safe = np.where(np.abs(channel) > 1e-12, channel, 1e-12)
    equalised = block[ESTIMATE_SYMBOLS:] / safe
    error = np.mean(np.abs(equalised - mode.payload_values(payload)) ** 2,
                    axis=0)
    return 10 * np.log10(1.0 / np.maximum(error, 1e-12))


def from_snr(snr_db, margin_db: float = 0.0, **kw) -> BitLoadedMode:
    return BitLoadedMode(bit_map=tuple(plan_bits(snr_db, margin_db)), **kw)


def uniform(bits_per_carrier: int, **kw) -> BitLoadedMode:
    """A flat map -- the control case VF9 represents, for A/B comparison."""
    return BitLoadedMode(bit_map=tuple([bits_per_carrier] * N_CARRIERS), **kw)
