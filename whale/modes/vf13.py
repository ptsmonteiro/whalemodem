"""VF13: combinatorial noncoherent MFSK for analog FM.

Reuses HF16's geometry (`experiments/hf16_mfsk_lowsnr`, retained there, not
here) transplanted to the FM passband: one M-ary decision per symbol, scored
by noncoherent energy detection so the receive path needs no phase
reference anywhere -- unlike VF12's OFDM, which needs a fast comb-pilot
channel tracker to survive the phase drift this FM path has. `mapping`
generalizes the one-hot decision to a k-of-N combinatorial code:
`active_tones` of `tone_count` tones are on at once per symbol, chosen from
a codebook built with the combinatorial number system and (if C(N,k) is not
a power of two) truncated to the largest power-of-two codeword count. The
demodulator scores every codeword in that codebook directly via one matrix
multiply -- exact max-log LLRs over the codebook actually used, not an
approximation.

Shipped configuration: 16 tones, 6 active per symbol (12 bits/symbol,
C(16,6)=8008 truncated to a 4,096-entry codebook), 150 Bd (320 samples at
48 kHz, 150 Hz spacing), tones from 600-3,000 Hz, punctured K=7
convolutional rate 7/8 (`whale.dsp.fec.PUNCTURE_PATTERNS`), interleaved, an
8 s fixed frame: 1,438.8 net application bit/s.

Measured on radios (IC-705 <-> Wouxun KG-UV9D Plus FM, drive 0.077,
2026-09-14): 150/150 exact-payload frames (90/90 ht->ic705, 60/60
ic705->ht). Installed as DEFAULT below the faster OFDM data rungs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from math import comb, gcd

import numpy as np

from .. import framing, rx_audio
from .. import dsp
from ..dsp import mfsk as _mfsk

TX_SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE          # 12 000
DECIMATION = TX_SAMPLE_RATE // RX_SAMPLE_RATE

BAND_LO_HZ = 600.0
BAND_HI_HZ = 3000.0

#: Per-tone cosine coefficient with one subband on the air; scaled by
#: 1/sqrt(active tones) so summing k active tones keeps roughly the same
#: total transmitted power as one.
DEFAULT_AMPLITUDE = 0.5

MAX_SAMPLE = 0.98

OFFSET_SEARCH_HZ = 30.0
OFFSET_STEP_DIVISOR = 3.0

#: Same tone_count (16) sync bank as HC0's; HC0's own energy-detection
#: acquisition threshold (`whale.modes.hc0.ACQUISITION_THRESHOLD`) is the
#: reference point for this value, not measured independently.
CONFIDENCE_THRESHOLD = 0.12

VF13_MODE_ID = 19


def _analytic(x: np.ndarray) -> np.ndarray:
    n = len(x)
    spec = np.fft.fft(x)
    h = np.zeros(n)
    h[0] = 1.0
    if n % 2 == 0:
        h[n // 2] = 1.0
        h[1:n // 2] = 2.0
    else:
        h[1:(n + 1) // 2] = 2.0
    return np.fft.ifft(spec * h)


def shift_hz(audio: np.ndarray, hz: float, rate: float) -> np.ndarray:
    """Move `audio` down by `hz`, staying real. See hf16_mfsk_lowsnr."""
    if not hz:
        return np.asarray(audio, dtype=np.float64)
    z = _analytic(np.asarray(audio, dtype=np.float64))
    n = np.arange(len(z))
    return np.real(z * np.exp(-2j * np.pi * hz * n / rate))


def _combinadic(c: int, k: int, n: int) -> tuple[int, ...]:
    """The `c`-th (colex-order) `k`-combination of {0,...,n-1}, as a sorted
    tuple. Standard combinatorial number system: the inverse map (subset ->
    index) is not needed here -- only bits-to-combination on transmit, and
    the demodulator scores *every* combination in the codebook directly
    rather than inverting a hard decision."""
    result = []
    remaining = n - 1
    for pos in range(k, 0, -1):
        a = remaining
        while comb(a, pos) > c:
            a -= 1
        result.append(a)
        c -= comb(a, pos)
        remaining = a - 1
    return tuple(sorted(result))


def _coprime_stride(size: int) -> int:
    start = max(3, int(size / 1.618033988749895))
    for delta in range(size):
        for candidate in (start + delta, start - delta):
            if 2 < candidate < size and gcd(candidate, size) == 1:
                return candidate
    raise ValueError(f"no usable interleaver stride for size {size}")


@dataclass(frozen=True)
class Vf13Mode:
    """One FM MFSK waveform: geometry, K subbands, framing, repetition.

    Defaults are the shipped VF13 configuration; `mode_for` (below) builds
    other geometries by target frame duration for `scripts/sweep_mfsk_fm.py`.
    """

    name: str = "vf13"
    mode_id: int = VF13_MODE_ID
    symbol_samples: int = 320            # 150 Hz spacing at 48 kHz
    tone_count: int = 16                 # M tones per subband / N-tone grid
    subbands: int = 1                    # K parallel one-hot groups
    mapping: str = "combinatorial"       # "subband" | "combinatorial"
    active_tones: int = 6                # k tones on, combinatorial only
    band_lo_hz: float = BAND_LO_HZ
    band_hi_hz: float = BAND_HI_HZ
    payload_symbols: int = 1100
    sync_seconds: float = 0.5
    repeat: int = 1
    constraint: int = 7                  # 7 or 9
    amplitude: float | None = None       # None => DEFAULT_AMPLITUDE/sqrt(k)
    #drive_scale: float = 0.077           # final peak-audio multiplier
    drive_scale: float = 1           # final peak-audio multiplier
    head_seconds: float = 0.10
    tail_seconds: float = 0.05
    soft_metric: str = "normalized"      # "normalized" | "raw" | "snr"
    fec_rate: str = "7/8"                # "1/2","2/3","3/4","5/6","7/8"
    preemph_db: float = 0.0              # dB boost at top of band vs. bottom
    guard_seconds: float = 0.0           # silent gap after each payload symbol
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    sync_seed: int = 0x0A73D
    head_seed: int = 0x136E9
    whitener_seed: int = 0x0C4B1
    tx_sample_rate: int = field(default=TX_SAMPLE_RATE, init=False)
    rx_sample_rate: int = field(default=RX_SAMPLE_RATE, init=False)

    def __post_init__(self):
        m = self.tone_count
        if m < 2 or m & (m - 1):
            raise ValueError("tone_count must be a power of two")
        if self.subbands < 1:
            raise ValueError("subbands must be at least 1")
        if self.mapping not in ("subband", "combinatorial"):
            raise ValueError(f"unknown mapping {self.mapping!r}")
        if self.mapping == "combinatorial":
            if self.subbands != 1:
                raise ValueError(
                    "combinatorial mapping requires subbands=1; tone_count "
                    "is the whole N-tone grid, active_tones is k")
            if not 1 <= self.active_tones < self.tone_count:
                raise ValueError("active_tones must be between 1 and tone_count-1")
        if self.symbol_samples % DECIMATION:
            raise ValueError(f"symbol_samples must be a multiple of {DECIMATION}")
        if self.constraint not in (7, 9):
            raise ValueError("constraint must be 7 or 9")
        if self.repeat < 1:
            raise ValueError("repeat must be at least 1")
        if not 0 < self.drive_scale <= 1:
            raise ValueError("drive_scale must be a peak amplitude in (0, 1]")
        if self.soft_metric not in ("normalized", "raw", "snr"):
            raise ValueError(f"unknown soft_metric {self.soft_metric!r}")
        if self.band_lo_hz <= 0 or self.band_lo_hz >= self.band_hi_hz:
            raise ValueError("band edges must be positive and in order")
        top_hz = (self.first_bin + self.total_tones) * self.spacing_hz
        if top_hz > self.band_hi_hz + 1e-9:
            raise ValueError(
                f"{self.subbands} subbands of {m} tones at {self.spacing_hz:.2f} Hz "
                f"spacing reach {top_hz:.1f} Hz, above band_hi_hz={self.band_hi_hz}")
        if self.first_bin < 1:
            raise ValueError("the lowest tone must be above DC")
        if self.coded_bits % self.repeat:
            raise ValueError(
                f"{self.payload_symbols} symbols x {self.bits_per_symbol} "
                f"bits = {self.coded_bits} coded bits is not divisible by "
                f"repeat={self.repeat}")
        if self.codec_bits % 2:
            raise ValueError("a rate-1/2 frame needs an even coded-bit count")
        if self.fec_rate not in dsp.fec.PUNCTURE_PATTERNS:
            raise ValueError(f"unknown fec_rate {self.fec_rate!r}; have "
                             f"{sorted(dsp.fec.PUNCTURE_PATTERNS)}")
        if self.fec_rate != "1/2":
            kept = int(dsp.fec.PUNCTURE_PATTERNS[self.fec_rate].sum())
            if self.codec_bits % kept:
                raise ValueError(
                    f"codec_bits={self.codec_bits} is not a multiple of "
                    f"{kept}, the kept-bit count of fec_rate={self.fec_rate}'s "
                    f"puncture period; adjust payload_symbols/repeat")
        if self.guard_seconds < 0:
            raise ValueError("guard_seconds must be non-negative")
        if self.guard_tx_samples % DECIMATION:
            raise ValueError(
                f"guard_seconds={self.guard_seconds} must be a multiple of "
                f"{DECIMATION / TX_SAMPLE_RATE:.6f}s at the rx decimation")

    # -- geometry -----------------------------------------------------------

    @property
    def spacing_hz(self) -> float:
        return TX_SAMPLE_RATE / self.symbol_samples

    @property
    def symbol_rate(self) -> float:
        return self.spacing_hz

    @property
    def symbol_seconds(self) -> float:
        return self.symbol_samples / TX_SAMPLE_RATE

    @property
    def rx_symbol_samples(self) -> int:
        return self.symbol_samples // DECIMATION

    @property
    def guard_tx_samples(self) -> int:
        return int(round(self.guard_seconds * TX_SAMPLE_RATE))

    @property
    def guard_rx_samples(self) -> int:
        return self.guard_tx_samples // DECIMATION

    @property
    def first_bin(self) -> int:
        return int(round(self.band_lo_hz / self.spacing_hz))

    @property
    def total_tones(self) -> int:
        return self.tone_count * self.subbands

    @property
    def bits_per_subband_symbol(self) -> int:
        return int(self.tone_count).bit_length() - 1

    @property
    def bits_per_symbol(self) -> int:
        if self.mapping == "combinatorial":
            return int(np.floor(np.log2(comb(self.total_tones, self.active_tones))))
        return self.subbands * self.bits_per_subband_symbol

    @property
    def active_tone_count(self) -> int:
        """Simultaneous tones on the air per symbol -- what the channel's
        K-dependent self-noise model should be driven by, which for
        combinatorial mapping is `active_tones`, not the (always-1) layout
        parameter `subbands`."""
        return self.active_tones if self.mapping == "combinatorial" else self.subbands

    @cached_property
    def combination_masks(self) -> np.ndarray:
        """(2**bits_per_symbol, total_tones) bool: which tones are on for
        each transmitted combination index, in combinatorial-number-system
        (colex) order truncated to a power of two. Built once per mode and
        reused for both TX tone selection and RX max-log scoring."""
        n, k = self.total_tones, self.active_tones
        count = 1 << self.bits_per_symbol
        masks = np.zeros((count, n), dtype=bool)
        for c in range(count):
            masks[c, list(_combinadic(c, k, n))] = True
        return masks

    @property
    def occupied_bandwidth_hz(self) -> float:
        return self.total_tones * self.spacing_hz

    @cached_property
    def tx_banks(self) -> tuple[_mfsk.ToneBank, ...]:
        return tuple(
            _mfsk.ToneBank(sample_rate=TX_SAMPLE_RATE,
                           symbol_samples=self.symbol_samples,
                           first_bin=self.first_bin + i * self.tone_count,
                           tone_count=self.tone_count)
            for i in range(self.subbands))

    @cached_property
    def rx_banks(self) -> tuple[_mfsk.ToneBank, ...]:
        return tuple(
            _mfsk.ToneBank(sample_rate=RX_SAMPLE_RATE,
                           symbol_samples=self.rx_symbol_samples,
                           first_bin=self.first_bin + i * self.tone_count,
                           tone_count=self.tone_count)
            for i in range(self.subbands))

    @property
    def per_tone_amplitude(self) -> float:
        base = DEFAULT_AMPLITUDE if self.amplitude is None else self.amplitude
        return base / np.sqrt(self.active_tone_count)

    @cached_property
    def preemph_weights(self) -> tuple[np.ndarray, ...]:
        """Per-subband, per-tone linear amplitude weight that flattens a
        measured `preemph_db` linear-in-dB tilt across the occupied band.

        0 dB (weight 1) at the lowest active tone, `+preemph_db` dB at the
        highest; the whole set is then rescaled so its RMS across every
        tone in every subband is 1, which keeps total transmitted power
        equal to the unweighted case (`drive_scale`/peak-clip behavior is
        unaffected -- it is applied afterwards, in `modulate`).
        """
        banks = self.tx_banks
        lo = float(banks[0].tone_hz[0])
        hi = float(banks[-1].tone_hz[-1])
        span = max(hi - lo, 1e-9)
        raw = tuple(10 ** (self.preemph_db * (np.asarray(bank.tone_hz) - lo)
                           / span / 20.0)
                    for bank in banks)
        if self.preemph_db == 0.0:
            return raw
        ms = float(np.mean([w ** 2 for weights in raw for w in weights]))
        scale = 1.0 / np.sqrt(max(ms, 1e-30))
        return tuple(w * scale for w in raw)

    @property
    def sync_symbols(self) -> int:
        n = int(round(self.sync_seconds / self.symbol_seconds))
        return max(8, n + (n & 1))

    @property
    def head_symbols(self) -> int:
        return max(2, int(round(self.head_seconds / self.symbol_seconds)))

    @property
    def tail_samples(self) -> int:
        return int(round(self.tail_seconds * TX_SAMPLE_RATE))

    # -- framing --------------------------------------------------------

    @property
    def coded_bits(self) -> int:
        return self.payload_symbols * self.bits_per_symbol

    @property
    def codec_bits(self) -> int:
        return self.coded_bits // self.repeat

    @cached_property
    def code(self) -> dsp.ConvolutionalCode:
        return dsp.K7 if self.constraint == 7 else dsp.K9

    @property
    def _mother_bits(self) -> int:
        """Interleaver-domain (rate-1/2) codeword length that punctures down
        to exactly `codec_bits` transmitted bits at `fec_rate`."""
        if self.fec_rate == "1/2":
            return self.codec_bits
        pattern = dsp.fec.PUNCTURE_PATTERNS[self.fec_rate]
        period, kept = len(pattern), int(pattern.sum())
        return (self.codec_bits // kept) * period

    @cached_property
    def codec(self) -> dsp.PacketCodec:
        mother = self._mother_bits
        return dsp.PacketCodec(
            payload_bits=mother,
            interleaver=dsp.interleave.multiplicative(
                mother, _coprime_stride(mother)),
            whitener_seed=self.whitener_seed,
            code=self.code, puncture_rate=self.fec_rate)

    @cached_property
    def outer_interleaver(self) -> dsp.Interleaver:
        return dsp.interleave.multiplicative(
            self.coded_bits, _coprime_stride(self.coded_bits))

    @property
    def max_payload_bytes(self) -> int:
        return self.codec.max_payload_bytes

    @property
    def chunk_size(self) -> int:
        return self.max_payload_bytes - framing.AIR_HEADER_BYTES

    @cached_property
    def sync_pattern(self) -> np.ndarray:
        bank = self.tx_banks[0]
        half = self.sync_symbols // 2
        bits = dsp.bits.pn_bits(half * bank.bits_per_symbol, self.sync_seed)
        return np.repeat(bank.symbols_from_bits(bits), 2)

    @cached_property
    def head_pattern(self) -> np.ndarray:
        bank = self.tx_banks[0]
        bits = dsp.bits.pn_bits(self.head_symbols * bank.bits_per_symbol,
                                self.head_seed)
        return bank.symbols_from_bits(bits)

    @property
    def total_symbols(self) -> int:
        return self.head_symbols + self.sync_symbols + self.payload_symbols

    def frame_seconds(self, payload_len: int | None = None) -> float:
        del payload_len   # fixed frame geometry, like hr0/hc0/vf12
        return (self.total_symbols * self.symbol_seconds
                + self.payload_symbols * self.guard_seconds
                + self.tail_samples / TX_SAMPLE_RATE)

    def raw_bit_rate(self) -> float:
        return self.symbol_rate * self.bits_per_symbol

    def net_bit_rate(self) -> float:
        return self.max_payload_bytes * 8 / self.frame_seconds()

    def geometry(self) -> dict:
        return {
            "tone_count": self.tone_count, "subbands": self.subbands,
            "mapping": self.mapping, "active_tones": self.active_tone_count,
            "spacing_hz": self.spacing_hz, "first_bin": self.first_bin,
            "band_lo_hz": self.band_lo_hz,
            "band_hi_hz": self.first_bin * self.spacing_hz
                          + self.occupied_bandwidth_hz,
            "occupied_bandwidth_hz": self.occupied_bandwidth_hz,
            "symbol_seconds": self.symbol_seconds,
            "bits_per_symbol": self.bits_per_symbol,
            "constraint": self.constraint, "repeat": self.repeat,
            "payload_symbols": self.payload_symbols,
            "max_payload_bytes": self.max_payload_bytes,
        }

    def describe(self) -> str:
        tag = (f"K={self.subbands}" if self.mapping == "subband"
               else f"comb_k={self.active_tones}")
        return (f"{self.name} M={self.tone_count} {tag} "
                f"spacing={self.spacing_hz:.2f}Hz "
                f"symbol={self.symbol_seconds * 1000:.2f}ms "
                f"bw={self.occupied_bandwidth_hz:.0f}Hz "
                f"code=K{self.constraint} repeat={self.repeat} "
                f"bytes={self.max_payload_bytes} "
                f"frame={self.frame_seconds():.2f}s "
                f"net={self.net_bit_rate():.1f}bps")

    # -- transmit -------------------------------------------------------

    def _tone_grid(self, payload: bytes) -> np.ndarray:
        """Tone index per (symbol, subband), shape (payload_symbols, subbands).

        For combinatorial mapping `subbands` is always 1 and the single
        column holds the transmitted *combination* index (0..2**bits-1)
        instead of a one-hot tone index.
        """
        coded = self.codec.encode(payload)                 # codec_bits
        repeated = np.tile(coded, self.repeat)              # coded_bits
        spread = self.outer_interleaver.spread(repeated)    # coded_bits
        bps = self.bits_per_symbol // self.subbands
        grouped = spread.reshape(self.payload_symbols, self.subbands, bps)
        if self.mapping == "combinatorial":
            bits = grouped[:, 0, :]
            idx = np.zeros(len(bits), dtype=np.int64)
            for column in range(bps):
                idx = (idx << 1) | bits[:, column]
            return idx.reshape(self.payload_symbols, 1)
        tones = np.empty((self.payload_symbols, self.subbands), dtype=np.int64)
        for j in range(self.subbands):
            tones[:, j] = self.tx_banks[j].symbols_from_bits(
                grouped[:, j, :].reshape(-1))
        return tones

    def tone_grid(self, payload: bytes) -> np.ndarray:
        """Public alias for ``_tone_grid``: the transmitted tone index per
        (symbol, subband), for SER measurement against a decoder's
        ``hard_tones``."""
        return self._tone_grid(payload)

    def _modulate_combinatorial(self, tones: np.ndarray, amp: float,
                                weights: np.ndarray) -> np.ndarray:
        """`tones` is the (payload_symbols, 1) combination-index grid;
        each symbol sums the cosines of its k active bins directly (there
        is no per-subband ToneBank one-hot decision to reuse)."""
        bank = self.tx_banks[0]
        masks = self.combination_masks
        phase = 2.0 * np.pi * np.arange(self.symbol_samples) / self.symbol_samples
        table = np.cos(bank.bins[:, None] * phase[None, :])  # (N, symbol_samples)
        body = np.zeros(self.payload_symbols * self.symbol_samples)
        for s in range(self.payload_symbols):
            on = masks[int(tones[s, 0])]
            if np.any(on):
                sig = (amp * weights[on])[:, None] * table[on]
                body[s * self.symbol_samples:(s + 1) * self.symbol_samples] = (
                    sig.sum(axis=0))
        return body

    def modulate(self, payload: bytes) -> np.ndarray:
        tones = self._tone_grid(payload)
        amp = self.per_tone_amplitude
        weights = self.preemph_weights
        if self.mapping == "combinatorial":
            body = self._modulate_combinatorial(tones, amp, weights[0])
        else:
            body = np.zeros(self.payload_symbols * self.symbol_samples)
            for j in range(self.subbands):
                raw = _mfsk.modulate(self.tx_banks[j], tones[:, j], 1.0)
                per_symbol = amp * weights[j][tones[:, j]]
                body += (raw.reshape(self.payload_symbols, self.symbol_samples)
                         * per_symbol[:, None]).reshape(-1)
        if self.guard_tx_samples:
            grid = body.reshape(self.payload_symbols, self.symbol_samples)
            gap = np.zeros((self.payload_symbols, self.guard_tx_samples))
            body = np.concatenate((grid, gap), axis=1).reshape(-1)
        preamble = np.concatenate((self.head_pattern, self.sync_pattern))
        head = _mfsk.modulate(self.tx_banks[0], preamble, amp)
        audio = np.concatenate((head, body))
        fade = min(len(audio), self.symbol_samples)
        audio = audio.copy()
        audio[:fade] *= np.linspace(0.0, 1.0, fade)
        audio = np.concatenate((audio, np.zeros(self.tail_samples)))
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak > MAX_SAMPLE:
            audio = audio * (MAX_SAMPLE / peak)
        audio = audio * self.drive_scale
        return audio.astype(np.float32)

    def encode(self, payload: bytes) -> np.ndarray:
        return self.modulate(bytes(payload))

    # -- receive ----------------------------------------------------------

    def _offset_hypotheses(self) -> np.ndarray:
        step = self.spacing_hz / OFFSET_STEP_DIVISOR
        if step >= OFFSET_SEARCH_HZ:
            return np.array([0.0])
        n = int(np.ceil(OFFSET_SEARCH_HZ / step))
        return np.arange(-n, n + 1) * step

    def acquire(self, audio_12k: np.ndarray) -> dict:
        audio = np.asarray(audio_12k, dtype=np.float64)
        bank0 = self.rx_banks[0]
        best = {"score": -1.0, "start": None, "coarse_hz": 0.0}
        for hz in self._offset_hypotheses():
            shifted = shift_hz(audio, hz, RX_SAMPLE_RATE)
            scores, step = _mfsk.correlate(bank0, shifted, self.sync_pattern)
            if not len(scores):
                continue
            peak = int(np.argmax(scores))
            if scores[peak] > best["score"]:
                best = {"score": float(scores[peak]), "start": peak * step,
                        "coarse_hz": float(hz), "step": step}
        if best["start"] is None:
            return {"score": 0.0, "start": None, "offset_hz": 0.0}
        shifted = shift_hz(audio, best["coarse_hz"], RX_SAMPLE_RATE)
        start = _mfsk.refine(bank0, shifted, self.sync_pattern,
                             best["start"], radius=best["step"], step=2)
        fine = _mfsk.offset_hz(bank0, shifted, start, self.sync_pattern)
        return {"score": best["score"], "start": int(start),
                "coarse_hz": best["coarse_hz"], "fine_hz": float(fine),
                "offset_hz": float(best["coarse_hz"] + fine)}

    def _soft_bits(self, bank: _mfsk.ToneBank, magnitudes: np.ndarray) -> np.ndarray:
        metric = np.asarray(magnitudes, dtype=np.float64) ** 2
        if self.soft_metric == "normalized":
            scale = np.maximum(np.mean(metric, axis=1, keepdims=True), 1e-30)
            metric = metric / scale
        elif self.soft_metric == "snr":
            top = np.max(metric, axis=1, keepdims=True)
            rest = (np.sum(metric, axis=1, keepdims=True) - top) / max(
                1, bank.tone_count - 1)
            rest = np.maximum(rest, 1e-30)
            excess = np.maximum(top / rest - 1.0, 0.0)
            metric = (metric / rest) * excess
        if self.mapping == "combinatorial":
            return self._soft_bits_combinatorial(metric)
        labels = bank._gray
        bps = bank.bits_per_symbol
        out = np.empty((len(metric), bps))
        for bit in range(bps):
            set_bits = (labels >> (bps - 1 - bit)) & 1
            out[:, bit] = (np.max(metric[:, set_bits == 0], axis=1)
                           - np.max(metric[:, set_bits == 1], axis=1))
        return out.reshape(-1)

    def _soft_bits_combinatorial(self, metric: np.ndarray) -> np.ndarray:
        """Exact max-log LLR per bit of the transmitted combination index.

        `metric` is (symbols, total_tones) normalized tone energy.
        `scores = metric @ masks.T` is (symbols, 2**bits): the summed
        energy of every codebook combination's k active tones, computed
        for the *whole* truncated codebook at once via one matrix
        multiply -- this is exact max-log over the codebook actually
        used (not an approximation), and cheap because the codebook is
        capped at 2**bits_per_symbol (4,096 for the shipped k=6/N=16
        configuration), not the full C(N,k) (8,008).
        """
        masks = self.combination_masks
        scores = metric @ masks.T.astype(np.float64)
        bits = self.bits_per_symbol
        idx = np.arange(masks.shape[0])
        out = np.empty((len(metric), bits))
        for bit in range(bits):
            bitval = (idx >> (bits - 1 - bit)) & 1
            out[:, bit] = (np.max(scores[:, bitval == 0], axis=1)
                           - np.max(scores[:, bitval == 1], axis=1))
        return out.reshape(-1)

    def _analyze_payload(self, bank, audio, payload_start):
        """Per-symbol tone analysis, skipping `guard_rx_samples` after each
        symbol -- a no-op reshape when there is no guard, and otherwise the
        guard-aware equivalent of one contiguous `_mfsk.analyze` call."""
        if not self.guard_rx_samples:
            return _mfsk.analyze(bank, audio, payload_start, self.payload_symbols)
        stride = self.rx_symbol_samples + self.guard_rx_samples
        rows = []
        for s in range(self.payload_symbols):
            one = _mfsk.analyze(bank, audio, payload_start + s * stride, 1)
            if one is None:
                return None
            rows.append(one[0])
        return np.stack(rows, axis=0)

    def soft_payload_bits(self, audio_12k, start, offset_hz):
        """Combined per-codec-bit soft metrics, and each subband's magnitudes."""
        audio = shift_hz(np.asarray(audio_12k, dtype=np.float64),
                         offset_hz, RX_SAMPLE_RATE)
        payload_start = start + self.sync_symbols * self.rx_symbol_samples
        bps = self.bits_per_symbol // self.subbands
        per_subband_soft = []
        magnitudes = []
        for j in range(self.subbands):
            values = self._analyze_payload(self.rx_banks[j], audio, payload_start)
            if values is None:
                return None, None
            mag = np.abs(values)
            magnitudes.append(mag)
            per_subband_soft.append(self._soft_bits(self.rx_banks[j], mag))
        grouped = np.stack(
            [s.reshape(self.payload_symbols, bps) for s in per_subband_soft],
            axis=1)                                    # (symbols, subbands, bps)
        spread_soft = grouped.reshape(-1)                # coded_bits order
        gathered = self.outer_interleaver.gather(spread_soft)
        combined = gathered.reshape(self.repeat, self.codec_bits).sum(axis=0)
        return combined, magnitudes

    def demodulate(self, audio_12k: np.ndarray) -> dict:
        result = {"synced": False, "payload": None, "crc_ok": False,
                  "sync_score": 0.0, "confidence": 0.0, "offset_hz": None,
                  "start_index": None, "tone_snr_db": None, "meta": None}
        acq = self.acquire(audio_12k)
        result["sync_score"] = acq["score"]
        result["confidence"] = acq["score"]
        if acq["start"] is None:
            return result
        result["start_index"] = acq["start"]
        result["offset_hz"] = acq["offset_hz"]

        combined, magnitudes = self.soft_payload_bits(
            audio_12k, acq["start"], acq["offset_hz"])
        if combined is None:
            return result
        result["synced"] = True
        # Each entry of `magnitudes` is one *bank's* own tone array; for
        # subband (one-hot) mapping exactly one tone per bank is active
        # regardless of how many subbands there are, so k=1 there. Only
        # combinatorial mapping puts more than one active tone inside a
        # single magnitude array (`active_tones` of the one N-tone bank).
        k = self.active_tones if self.mapping == "combinatorial" else 1
        snrs = [self.tone_snr_db(m, k=k) for m in magnitudes]
        result["tone_snr_db"] = float(np.mean(snrs))
        result["tone_snr_db_per_subband"] = snrs
        per_symbol = self.per_symbol_tone_snr_db(magnitudes, k=k)
        result["tone_snr_db_median"] = float(np.median(per_symbol))
        result["tone_snr_db_worst"] = float(np.min(per_symbol))
        result["hard_tones"] = self.hard_tones(magnitudes)
        result["magnitudes"] = magnitudes
        payload, meta = self.codec.decode_soft(combined)
        result["payload"] = payload
        result["crc_ok"] = bool(meta.get("crc_ok"))
        result["meta"] = meta
        return result

    @staticmethod
    def per_symbol_tone_snr_db(magnitudes: list[np.ndarray], k: int = 1
                               ) -> np.ndarray:
        """Per-(symbol, subband) SNR in dB, flattened across all subbands.

        Same top-vs-rest ratio as ``tone_snr_db``, but kept per symbol
        instead of averaged, so callers can take a median/worst over the
        whole frame -- ``tone_snr_db`` alone hides whether a frame's margin
        is uniform or a few symbols are carrying the whole frame. `k` is
        the number of simultaneously-active tones per symbol (1 for
        one-hot subband mapping, `active_tones` for combinatorial): the
        top-`k` tones' mean power vs. the remaining tones' mean power.
        """
        rows = []
        for mag in magnitudes:
            power = np.abs(np.asarray(mag)) ** 2
            n = power.shape[1]
            kk = max(1, min(k, n - 1))
            sorted_p = np.sort(power, axis=1)
            top = np.mean(sorted_p[:, -kk:], axis=1)
            rest = np.mean(sorted_p[:, :-kk], axis=1)
            ratio = top / np.maximum(rest, 1e-30)
            rows.append(10 * np.log10(np.maximum(ratio - 1.0, 1e-12)))
        return np.concatenate(rows) if rows else np.zeros(0)

    def hard_tones(self, magnitudes: list[np.ndarray]) -> np.ndarray:
        """Hard decisions, shape (payload_symbols, subbands).

        For combinatorial mapping this is the argmax *combination* index
        (shape (payload_symbols, 1)), scored the same max-log way as
        `_soft_bits_combinatorial` -- not simply the top tone, since the
        transmitted symbol is a whole k-subset.
        """
        if self.mapping == "combinatorial":
            metric = np.abs(magnitudes[0]) ** 2
            scale = np.maximum(np.mean(metric, axis=1, keepdims=True), 1e-30)
            metric = metric / scale
            scores = metric @ self.combination_masks.T.astype(np.float64)
            return np.argmax(scores, axis=1).reshape(-1, 1)
        return np.stack(
            [np.argmax(np.abs(mag), axis=1) for mag in magnitudes], axis=1)

    def decode(self, audio: np.ndarray, **kwargs) -> dict:
        del kwargs
        return self.demodulate(audio)

    def airtime(self, payload_len: int | None = None) -> float:
        return self.frame_seconds(payload_len)

    @staticmethod
    def tone_snr_db(values: np.ndarray, k: int = 1) -> float:
        power = np.abs(np.asarray(values)) ** 2
        n = power.shape[1]
        kk = max(1, min(k, n - 1))
        sorted_p = np.sort(power, axis=1)
        top = np.mean(sorted_p[:, -kk:], axis=1)
        rest = np.mean(sorted_p[:, :-kk], axis=1)
        ratio = np.mean(top) / max(np.mean(rest), 1e-30)
        return float(10 * np.log10(max(ratio - 1.0, 1e-12)))


def mode_for(tone_count=16, *, subbands=1, frame_seconds=None,
             payload_symbols=None, repeat=1, constraint=7, sync_seconds=0.5,
             symbol_samples=320, **kwargs) -> Vf13Mode:
    """Build a mode by target frame duration instead of symbol count."""
    probe_kwargs = {k: v for k, v in kwargs.items() if k != "fec_rate"}
    probe = Vf13Mode(tone_count=tone_count, subbands=subbands,
                     symbol_samples=symbol_samples, payload_symbols=8,
                     repeat=1, constraint=constraint,
                     sync_seconds=sync_seconds, **probe_kwargs)
    if payload_symbols is None:
        if frame_seconds is None:
            raise ValueError("give frame_seconds or payload_symbols")
        overhead = (probe.head_symbols + probe.sync_symbols) * probe.symbol_seconds
        overhead += probe.tail_samples / TX_SAMPLE_RATE
        payload_symbols = int((frame_seconds - overhead)
                              / (probe.symbol_seconds + probe.guard_seconds))
    bps = probe.bits_per_symbol
    fec_rate = kwargs.get("fec_rate", "1/2")
    kept = (None if fec_rate == "1/2"
            else int(dsp.fec.PUNCTURE_PATTERNS[fec_rate].sum()))
    quantum = repeat * 2

    def _bad(n):
        coded = n * bps
        if coded % quantum:
            return True
        return kept is not None and (coded // repeat) % kept
    while payload_symbols > 0 and _bad(payload_symbols):
        payload_symbols -= 1
    if payload_symbols <= 0:
        raise ValueError("frame_seconds is too short for this geometry")
    return Vf13Mode(tone_count=tone_count, subbands=subbands,
                    symbol_samples=symbol_samples,
                    payload_symbols=payload_symbols, repeat=repeat,
                    constraint=constraint, sync_seconds=sync_seconds,
                    **kwargs)


#: The shipped instance: config A (see module docstring), 1,438.8 net bit/s.
VF13 = Vf13Mode()
