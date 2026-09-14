"""VF14: robust noncoherent one-tone M-FSK control/fallback mode for FM.

HC0's waveform re-fitted to the FM audio band: one tone per symbol, so the
transmitted audio is constant-envelope and every symbol uses the full
deviation; tone spacing equals the symbol rate, so the tones are orthogonal
under noncoherent energy detection and the receiver needs no phase
reference. Everything downstream of the tone magnitudes is `whale.dsp`:
`mfsk.ToneBank` geometry, Gray mapping, `PacketCodec` (length, CRC32,
whitening, terminated K=7 rate-1/2 soft Viterbi) and a multiplicative
interleaver spanning the whole codeword.

Three profiles, three mode IDs (an ID is one immutable waveform):

  vf14-16  16 tones, 62.5 Hz spacing, 16 ms symbols, 562.5-1,500 Hz
  vf14-4    4 tones, 600 Hz spacing, 1.667 ms symbols, 600-2,400 Hz
  vf14-8    8 tones, 125 Hz spacing,   8 ms symbols, 625-1,500 Hz

FM audio carries no carrier offset (the sound-card clocks are ~5 ppm apart,
0.01 Hz at 1.5 kHz), so there is no offset search.

Each profile has short, medium and full payload grids sharing one preamble.
The short grid carries ACK/DISC/FLOOR traffic; the medium grid carries typical
CONNECT exchanges; the full grid carries DATA and long control packets.
CRC32 selects the decoded grid.

Simulated flat_nbfm C/N floor: vf14-16 -7 dB (20/20 full-capacity frames,
-8 dB did not pass at 0/20); vf14-4 0 dB (20/20 full-capacity frames, -1 dB
did not pass at 14/20); vf14-8 not measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from math import gcd

import numpy as np

from .. import dsp, framing, rx_audio
from ..dsp import mfsk as _mfsk

TX_SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
DECIMATION = TX_SAMPLE_RATE // RX_SAMPLE_RATE

VF14_16_MODE_ID = 20
VF14_4_MODE_ID = 23
VF14_8_MODE_ID = 21

#: Peak (= sine) amplitude of every tone.
DEFAULT_AMPLITUDE = 0.6
MAX_SAMPLE = 0.95

#: Largest packet the full grid carries: a CONNECT_ACK with two 15-character
#: callsigns is 51 bytes plus one per advertised mode ID, so 64 bytes leaves
#: room for 13 mode IDs.
FULL_PACKET_PAYLOAD_BYTES = 64
#: A DATA_ACK is the 10-byte air header plus one body byte.
SHORT_PACKET_PAYLOAD_BYTES = 11


def _interleaver_stride(size: int) -> int:
    """The stride nearest size/phi that is coprime with `size`."""
    start = max(3, int(size / 1.618033988749895))
    for delta in range(size):
        for candidate in (start + delta, start - delta):
            if 2 < candidate < size and gcd(candidate, size) == 1:
                return candidate
    raise ValueError(f"no usable interleaver stride for size {size}")


@dataclass(frozen=True)
class Vf14Mode:
    name: str
    mode_id: int
    tone_count: int
    symbol_samples: int                  # at 48 kHz
    first_bin: int
    sync_symbols: int
    payload_symbols: int                 # full grid
    short_payload_symbols: int
    medium_payload_symbols: int
    confidence_threshold: float
    head_seconds: float = 0.6
    tail_seconds: float = 0.02
    amplitude: float = DEFAULT_AMPLITUDE
    #: K=7 puncture rate (`whale.dsp.fec.PUNCTURE_PATTERNS`); "1/2" is the
    #: unpunctured mother code.
    fec_rate: str = "1/2"
    #: Linear-in-dB tilt removed from the tones, 0 dB at the lowest and
    #: -`preemph_db` at the highest relative to the RMS-normalized set, the
    #: same knob as `Vf13Mode.preemph_db`.
    preemph_db: float = 0.0
    sync_seed: int = 0x0B91D
    head_seed: int = 0x13A57
    whitener_seed: int = 0x0E14A
    short_whitener_seed: int = 0x0E14B
    medium_whitener_seed: int = 0x0E14C
    tx_sample_rate: int = field(default=TX_SAMPLE_RATE, init=False)
    rx_sample_rate: int = field(default=RX_SAMPLE_RATE, init=False)

    def __post_init__(self):
        if self.symbol_samples % DECIMATION:
            raise ValueError(f"symbol_samples must be a multiple of {DECIMATION}")
        if self.fec_rate not in dsp.fec.PUNCTURE_PATTERNS:
            raise ValueError(f"unknown fec_rate {self.fec_rate!r}; have "
                             f"{sorted(dsp.fec.PUNCTURE_PATTERNS)}")
        for symbols in (self.payload_symbols, self.short_payload_symbols,
                        self.medium_payload_symbols):
            coded_bits = symbols * self.bits_per_symbol
            if coded_bits % 2:
                raise ValueError("a rate-1/2 grid needs an even coded-bit count")
            if self.fec_rate != "1/2":
                kept = int(dsp.fec.PUNCTURE_PATTERNS[self.fec_rate].sum())
                if coded_bits % kept:
                    raise ValueError(
                        f"{symbols} symbols x {self.bits_per_symbol} bits = "
                        f"{coded_bits} coded bits is not a multiple of "
                        f"{kept}, fec_rate={self.fec_rate!r}'s puncture period")
        # Constructing the banks and codecs validates the geometry.
        self.rx_bank, self.codec, self.short_codec, self.medium_codec  # noqa: B018

    # -- geometry -----------------------------------------------------------

    @cached_property
    def tx_bank(self) -> _mfsk.ToneBank:
        return _mfsk.ToneBank(TX_SAMPLE_RATE, self.symbol_samples,
                              self.first_bin, self.tone_count)

    @cached_property
    def rx_bank(self) -> _mfsk.ToneBank:
        return _mfsk.ToneBank(RX_SAMPLE_RATE, self.rx_symbol_samples,
                              self.first_bin, self.tone_count)

    @property
    def rx_symbol_samples(self) -> int:
        return self.symbol_samples // DECIMATION

    @property
    def bits_per_symbol(self) -> int:
        return self.tx_bank.bits_per_symbol

    @property
    def spacing_hz(self) -> float:
        return self.tx_bank.spacing_hz

    @property
    def baud(self) -> float:
        return self.tx_bank.symbol_rate

    @property
    def symbol_seconds(self) -> float:
        return self.symbol_samples / TX_SAMPLE_RATE

    @property
    def head_symbols(self) -> int:
        return max(1, int(np.ceil(self.head_seconds / self.symbol_seconds)))

    @property
    def tail_samples(self) -> int:
        return int(round(self.tail_seconds * TX_SAMPLE_RATE))

    def _mother_bits(self, coded_bits: int) -> int:
        """Interleaver-domain (rate-1/2) codeword length that punctures down
        to exactly `coded_bits` transmitted bits at `self.fec_rate`."""
        if self.fec_rate == "1/2":
            return coded_bits
        pattern = dsp.fec.PUNCTURE_PATTERNS[self.fec_rate]
        period, kept = len(pattern), int(pattern.sum())
        return (coded_bits // kept) * period

    def _make_codec(self, symbols: int, seed: int) -> dsp.PacketCodec:
        mother = self._mother_bits(symbols * self.bits_per_symbol)
        return dsp.PacketCodec(
            payload_bits=mother,
            interleaver=dsp.interleave.multiplicative(
                mother, _interleaver_stride(mother)),
            whitener_seed=seed, code=dsp.K7, puncture_rate=self.fec_rate)

    @cached_property
    def codec(self) -> dsp.PacketCodec:
        return self._make_codec(self.payload_symbols, self.whitener_seed)

    @cached_property
    def short_codec(self) -> dsp.PacketCodec:
        return self._make_codec(self.short_payload_symbols,
                                self.short_whitener_seed)

    @cached_property
    def medium_codec(self) -> dsp.PacketCodec:
        return self._make_codec(self.medium_payload_symbols,
                                self.medium_whitener_seed)

    @property
    def max_payload_bytes(self) -> int:
        return self.codec.max_payload_bytes

    @property
    def short_max_payload_bytes(self) -> int:
        return self.short_codec.max_payload_bytes

    @property
    def medium_max_payload_bytes(self) -> int:
        return self.medium_codec.max_payload_bytes

    @property
    def chunk_size(self) -> int:
        return self.max_payload_bytes - framing.AIR_HEADER_BYTES

    @cached_property
    def sync_pattern(self) -> np.ndarray:
        """PN tones, not repeated pairs: with no offset to estimate, every
        symbol is free to be a different tone, which sharpens the timing
        peak."""
        return self.tx_bank.symbols_from_bits(dsp.bits.pn_bits(
            self.sync_symbols * self.bits_per_symbol, self.sync_seed))

    @cached_property
    def head_pattern(self) -> np.ndarray:
        return self.tx_bank.symbols_from_bits(dsp.bits.pn_bits(
            self.head_symbols * self.bits_per_symbol, self.head_seed))

    @cached_property
    def tone_weights(self) -> np.ndarray:
        hz = self.tx_bank.tone_hz
        raw = 10 ** (-self.preemph_db * (hz - hz[0]) / (hz[-1] - hz[0]) / 20)
        return raw / np.sqrt(np.mean(raw ** 2))

    def grid_symbols(self, payload_len: int) -> int:
        if not 0 <= payload_len <= self.max_payload_bytes:
            raise ValueError(
                f"packet is {payload_len} bytes; {self.name} carries at most "
                f"{self.max_payload_bytes}")
        if payload_len <= self.short_max_payload_bytes:
            return self.short_payload_symbols
        if payload_len <= self.medium_max_payload_bytes:
            return self.medium_payload_symbols
        return self.payload_symbols

    def frame_seconds(self, payload_len: int | None = None) -> float:
        symbols = self.grid_symbols(
            self.max_payload_bytes if payload_len is None else payload_len)
        return ((self.head_symbols + self.sync_symbols + symbols)
                * self.symbol_samples + self.tail_samples) / TX_SAMPLE_RATE

    def airtime(self, payload_len: int) -> float:
        return self.frame_seconds(payload_len)

    def net_bit_rate(self) -> float:
        return self.chunk_size * 8 / self.frame_seconds()

    def describe(self) -> str:
        hz = self.tx_bank.tone_hz
        return (f"{self.name}: {self.tone_count}-FSK {hz[0]:.1f}-{hz[-1]:.1f} Hz, "
                f"{self.baud:g} Bd, sync {self.sync_symbols}, grids "
                f"{self.short_payload_symbols}/{self.medium_payload_symbols}/"
                f"{self.payload_symbols} symbols "
                f"({self.short_max_payload_bytes}/{self.medium_max_payload_bytes}/"
                f"{self.max_payload_bytes} B), "
                f"frames {self.frame_seconds(0):.3f}/{self.frame_seconds():.3f} s, "
                f"DATA net {self.net_bit_rate():.1f} bit/s")

    # -- transmit -----------------------------------------------------------

    def modulate(self, payload: bytes) -> np.ndarray:
        payload = bytes(payload)
        symbols = self.grid_symbols(len(payload))
        codec = {self.short_payload_symbols: self.short_codec,
                 self.medium_payload_symbols: self.medium_codec,
                 self.payload_symbols: self.codec}[symbols]
        tones = np.concatenate((self.head_pattern, self.sync_pattern,
                                self.tx_bank.symbols_from_bits(codec.encode(payload))))
        audio = _mfsk.modulate(self.tx_bank, tones, 1.0)
        gains = self.amplitude * self.tone_weights[tones]
        audio = (audio.reshape(len(tones), self.symbol_samples)
                 * gains[:, None]).reshape(-1)
        fade = min(240, len(audio))
        audio[:fade] *= np.linspace(0.0, 1.0, fade)
        audio = np.concatenate((audio, np.zeros(self.tail_samples)))
        peak = float(np.max(np.abs(audio)))
        if peak > MAX_SAMPLE:
            audio *= MAX_SAMPLE / peak
        return audio.astype(np.float32)

    def encode(self, payload: bytes) -> np.ndarray:
        return self.modulate(payload)

    # -- receive ------------------------------------------------------------

    def soft_bits(self, magnitudes: np.ndarray) -> np.ndarray:
        """HC0's per-symbol-normalized max-log metric.

        Normalizing each symbol by its own mean tone energy is what keeps a
        discriminator click -- energy across every tone in one symbol --
        from dominating: that symbol's tones all score near 1 and its LLRs
        collapse towards zero instead of growing with the click. It also
        clips every |LLR| at `tone_count`, the score of a symbol whose
        energy is all in one tone. Measured on flat_nbfm at the payload
        cliff, a frame-wide normalizer, magnitude instead of energy, and a
        tighter clip at 4 all decoded within 2-3 frames of 30 of this one.
        """
        return _mfsk.soft_bits(self.rx_bank, magnitudes)

    def acquire(self, samples: np.ndarray) -> tuple[int | None, float]:
        bank = self.rx_bank
        scores, step = _mfsk.correlate(bank, samples, self.sync_pattern)
        if not len(scores):
            return None, 0.0
        coarse = int(np.argmax(scores))
        score = float(scores[coarse])
        if score < self.confidence_threshold:
            return coarse * step, score
        start = _mfsk.refine(bank, samples, self.sync_pattern, coarse * step,
                             radius=step, step=max(1, self.rx_symbol_samples // 48))
        return int(start), score

    def demodulate(self, audio: np.ndarray) -> dict:
        result = {"synced": False, "payload": None, "confidence": 0.0,
                  "start_index": None, "cfo_hz": 0.0, "raw_payload_bits": None}
        try:
            samples = np.asarray(audio, dtype=np.float64)
        except (TypeError, ValueError):
            result["failure"] = "invalid audio"
            return result
        sync_len = self.sync_symbols * self.rx_symbol_samples
        if (samples.ndim != 1 or len(samples) < sync_len
                or not np.all(np.isfinite(samples))):
            result["failure"] = "invalid or short capture"
            return result

        start, confidence = self.acquire(samples)
        result.update(confidence=confidence, start_index=start)
        if start is None or confidence < self.confidence_threshold:
            result["failure"] = "preamble not found"
            return result
        payload_start = start + sync_len
        result["sync_end_index"] = payload_start
        tail = self.tail_samples // DECIMATION

        grids = ((self.short_payload_symbols, self.short_codec),
                 (self.medium_payload_symbols, self.medium_codec),
                 (self.payload_symbols, self.codec))
        for symbols, codec in grids:
            values = _mfsk.analyze(self.rx_bank, samples, payload_start, symbols)
            if values is None:
                # A failed short grid may be the prefix of a full frame: no
                # end_index, so the link waits rather than consuming it.
                result["failure"] = "frame truncated"
                return result
            magnitudes = np.abs(values)
            payload, meta = codec.decode_soft(self.soft_bits(magnitudes))
            if payload is None and symbols != self.payload_symbols:
                continue
            result.pop("failure", None)
            result["end_index"] = min(
                len(samples), payload_start + symbols * self.rx_symbol_samples + tail)
            result.update(meta)
            result.update(
                payload=payload, synced=True, payload_symbols=symbols,
                tone_magnitudes=magnitudes,
                tone_snr_db=_tone_snr_db(magnitudes),
                raw_payload_bits=self.rx_bank.bits_from_symbols(
                    np.argmax(magnitudes, axis=1)))
            return result
        return result  # unreachable: the full grid always returns

    def decode(self, audio, **kwargs) -> dict:
        del kwargs
        return self.demodulate(audio)


def _tone_snr_db(magnitudes: np.ndarray) -> float:
    """Winning tone against the mean of the others, as `hc0._tone_snr_db`."""
    power = magnitudes ** 2
    best = np.max(power, axis=1)
    rest = (np.sum(power, axis=1) - best) / (magnitudes.shape[1] - 1)
    return float(10.0 * np.log10(np.mean(best) / max(np.mean(rest), 1e-30)))


#: Was the FM control mode; superseded by VF14_4 (below). Kept, and its
#: on-air ID (mode 20) kept unreassigned, as an experimental waveform --
#: see whale/mode_qualification.py's MANIFEST. Old peers that only know
#: mode 20 as control no longer interoperate.
VF14_16 = Vf14Mode(
    name="vf14-16", mode_id=VF14_16_MODE_ID, tone_count=16,
    symbol_samples=768, first_bin=9,
    sync_symbols=24, payload_symbols=283, short_payload_symbols=71,
    medium_payload_symbols=171,
    confidence_threshold=0.12)

#: Faster DATA waveform: 274 packet bytes (264 application bytes) in 3.36 s.
#: At 600 Bd a VF14_8-length (48-symbol, 80 ms) preamble is too short to hold
#: down the noise-correlation floor; 96 symbols cleared the noise-floor test
#: and reached 40/40 full-capacity frames at 0 dB in isolation, but missed
#: 20/20 against this file's own fixed-seed discriminator-threshold draw
#: (19/20). 144 symbols (240 ms) is the shortest tried that holds 20/20 on
#: both that draw and 40/40 across two fresh 20-trial seed sets.
VF14_4 = Vf14Mode(
    name="vf14-4", mode_id=VF14_4_MODE_ID, tone_count=4,
    symbol_samples=80, first_bin=1,
    sync_symbols=144, payload_symbols=1500, short_payload_symbols=144,
    medium_payload_symbols=400, fec_rate="3/4",
    confidence_threshold=0.145)

VF14_8 = Vf14Mode(
    name="vf14-8", mode_id=VF14_8_MODE_ID, tone_count=8,
    symbol_samples=384, first_bin=5,
    sync_symbols=48, payload_symbols=378, short_payload_symbols=96,
    medium_payload_symbols=228,
    confidence_threshold=0.14)

PROFILES = {"16": VF14_16, "4": VF14_4, "8": VF14_8}
