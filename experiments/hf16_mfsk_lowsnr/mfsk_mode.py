"""HF16: parametric non-coherent MFSK for a very low SNR HF path.

Built for the measured `ic705 -> ic7300` leg, whose properties came out of
`experiments/hf15_lowsnr_ofdm/sounder.py` and `coherence.py`:

  * band SNR in 2400 Hz between about -6 and -14 dB, varying frame to frame;
  * coherent integration keeps paying to roughly 0.3-1.0 s and then stops,
    so a symbol may be long but not arbitrarily long;
  * a carrier offset near +8 Hz that drifts a couple of Hz between keyings.

Those three facts pick the waveform. Nothing coherent survives at -14 dB, so
information is carried by *which* tone is present and detected as energy --
no phase reference anywhere in the receive path. The coherence limit caps the
symbol duration, and therefore caps how many tones fit: orthogonal MFSK ties
tone spacing to the symbol rate, so M tones across a fixed band means both
1/M of the spacing and M times the symbol. The offset is larger than half a
tone spacing once M passes 128, which is why acquisition searches offset
hypotheses instead of assuming one.

Geometry. M tones fill BAND_LO..BAND_HI with spacing = symbol rate =
(BAND_HI - BAND_LO)/M, which makes every derived length an exact integer at
both 48 kHz and 12 kHz (symbol = 20M and 5M samples) and puts the lowest tone
exactly on BAND_LO.

    M     spacing   symbol    raw bits/s   spacing vs the ~2 Hz offset drift
    16    150.0 Hz   6.7 ms       600      trivially safe
    32     75.0 Hz  13.3 ms       375      trivially safe
    64     37.5 Hz  26.7 ms       225      safe
    128    18.75 Hz 53.3 ms       131      needs the offset search
    256     9.38 Hz 106.7 ms       75      needs the offset search
    512     4.69 Hz 213.3 ms       42      at the coherence limit; marginal

Coding is `whale.dsp.PacketCodec` unchanged -- length/CRC32 packet, whitener,
multiplicative interleaver, rate-1/2 K=7 or K=9 convolutional code, soft
Viterbi -- with one addition this mode needs and the OFDM modes do not:
`repeat`, which sends every coded bit `repeat` times at widely separated
places in the frame and sums the copies' soft metrics at the receiver. That
buys rate 1/(2*repeat) and, because the outer interleaver scatters the copies
across the whole frame, it buys time diversity across the path's ~0.5 s fades
rather than just noise averaging.

Everything downstream of the tone magnitudes is shared repo code. This module
is geometry, framing, acquisition and the repetition layer.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import cached_property
from math import gcd
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

from whale import dsp, rx_audio
from whale.dsp import mfsk as _mfsk

TX_SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE          # 12 000
DECIMATION = TX_SAMPLE_RATE // RX_SAMPLE_RATE

BAND_LO_HZ = 300.0
BAND_HI_HZ = 2700.0
BAND_WIDTH_HZ = BAND_HI_HZ - BAND_LO_HZ

#: Constant-envelope, so this is the peak too. MFSK's whole physical
#: advantage over the OFDM attempts is that peak and RMS are the same number:
#: through a peak-limited SSB transmitter it delivers the full envelope where
#: a 10 dB crest-factor OFDM frame delivers a tenth of it.
DEFAULT_AMPLITUDE = 0.5

#: Acquisition searches this far for the carrier offset, in Hz. The measured
#: leg sits near +8 Hz and drifts a couple of Hz between keyings; +/-30 Hz
#: covers that with room for a different pair of radios.
OFFSET_SEARCH_HZ = 30.0

#: Hypotheses are spaced at a third of a tone spacing: a non-coherent tone
#: detector's response is flat well inside that, so a finer grid buys nothing
#: and a coarser one starts losing the peak.
OFFSET_STEP_DIVISOR = 3.0


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
    """Move `audio` down by `hz`, staying real.

    Used to fold a carrier-offset hypothesis into acquisition without
    touching the tone detector: `_mfsk.correlate` works on a real buffer and
    an exact FFT bin grid, and shifting the buffer keeps both.
    """
    if not hz:
        return np.asarray(audio, dtype=np.float64)
    z = _analytic(np.asarray(audio, dtype=np.float64))
    n = np.arange(len(z))
    return np.real(z * np.exp(-2j * np.pi * hz * n / rate))


def _coprime_stride(size: int) -> int:
    """A multiplicative-interleaver stride near size/phi that is coprime to it."""
    start = max(3, int(size / 1.618033988749895))
    for delta in range(size):
        for candidate in (start + delta, start - delta):
            if 2 < candidate < size and gcd(candidate, size) == 1:
                return candidate
    raise ValueError(f"no usable interleaver stride for size {size}")


@dataclass(frozen=True)
class MfskMode:
    """One MFSK waveform: geometry, framing and the repetition layer."""

    tone_count: int
    payload_symbols: int
    sync_seconds: float = 0.5
    repeat: int = 1
    constraint: int = 7                 # 7 or 9; K9 is ~0.5 dB better, 4x cost
    amplitude: float = DEFAULT_AMPLITUDE
    head_seconds: float = 0.10
    tail_seconds: float = 0.05
    soft_metric: str = "normalized"     # "normalized" | "raw" | "snr"
    sync_seed: int = 0x0A73D
    head_seed: int = 0x136E9
    whitener_seed: int = 0x0C4B1

    def __post_init__(self):
        m = self.tone_count
        if m < 8 or m & (m - 1):
            raise ValueError("tone_count must be a power of two, at least 8")
        if m % 8:
            raise ValueError("tone_count must be a multiple of 8 so the "
                             "lowest tone lands exactly on BAND_LO_HZ")
        if self.constraint not in (7, 9):
            raise ValueError("constraint must be 7 or 9")
        if self.coded_bits % self.repeat:
            raise ValueError(
                f"{self.payload_symbols} symbols x {self.bits_per_symbol} "
                f"bits = {self.coded_bits} coded bits is not divisible by "
                f"repeat={self.repeat}")
        if self.codec_bits % 2:
            raise ValueError("a rate-1/2 frame needs an even coded-bit count")
        if self.soft_metric not in ("normalized", "raw", "snr"):
            raise ValueError(f"unknown soft_metric {self.soft_metric!r}")

    # -- geometry ---------------------------------------------------------

    @property
    def spacing_hz(self) -> float:
        return BAND_WIDTH_HZ / self.tone_count

    @property
    def symbol_rate(self) -> float:
        return self.spacing_hz

    @property
    def symbol_seconds(self) -> float:
        return 1.0 / self.symbol_rate

    @property
    def tx_symbol_samples(self) -> int:
        return 20 * self.tone_count       # 48000 / (2400/M)

    @property
    def rx_symbol_samples(self) -> int:
        return 5 * self.tone_count        # 12000 / (2400/M)

    @property
    def first_bin(self) -> int:
        return self.tone_count // 8       # BAND_LO_HZ / spacing

    @property
    def bits_per_symbol(self) -> int:
        return int(self.tone_count).bit_length() - 1

    @cached_property
    def tx_bank(self) -> _mfsk.ToneBank:
        return _mfsk.ToneBank(sample_rate=TX_SAMPLE_RATE,
                              symbol_samples=self.tx_symbol_samples,
                              first_bin=self.first_bin,
                              tone_count=self.tone_count)

    @cached_property
    def rx_bank(self) -> _mfsk.ToneBank:
        return _mfsk.ToneBank(sample_rate=RX_SAMPLE_RATE,
                              symbol_samples=self.rx_symbol_samples,
                              first_bin=self.first_bin,
                              tone_count=self.tone_count)

    @property
    def tone_hz(self) -> np.ndarray:
        return self.tx_bank.tone_hz

    @property
    def sync_symbols(self) -> int:
        """Even, so the pattern is whole repeated pairs."""
        n = int(round(self.sync_seconds / self.symbol_seconds))
        return max(8, n + (n & 1))

    @property
    def head_symbols(self) -> int:
        return max(2, int(round(self.head_seconds / self.symbol_seconds)))

    @property
    def tail_samples(self) -> int:
        return int(round(self.tail_seconds * TX_SAMPLE_RATE))

    # -- framing ----------------------------------------------------------

    @property
    def coded_bits(self) -> int:
        """Bits carried by the payload symbols, before repetition is undone."""
        return self.payload_symbols * self.bits_per_symbol

    @property
    def codec_bits(self) -> int:
        """Bits the rate-1/2 codec produces; each is sent `repeat` times."""
        return self.coded_bits // self.repeat

    @cached_property
    def code(self) -> dsp.ConvolutionalCode:
        return dsp.K7 if self.constraint == 7 else dsp.K9

    @cached_property
    def codec(self) -> dsp.PacketCodec:
        return dsp.PacketCodec(
            payload_bits=self.codec_bits,
            interleaver=dsp.interleave.multiplicative(
                self.codec_bits, _coprime_stride(self.codec_bits)),
            whitener_seed=self.whitener_seed,
            code=self.code)

    @cached_property
    def outer_interleaver(self) -> dsp.Interleaver:
        """Scatters the `repeat` copies of each coded bit across the frame.

        Without it the copies would sit next to each other and average noise
        only. Spread across the whole payload they straddle the path's ~0.5 s
        fades, which is where the real gain is.
        """
        return dsp.interleave.multiplicative(
            self.coded_bits, _coprime_stride(self.coded_bits))

    @property
    def max_payload_bytes(self) -> int:
        return self.codec.max_payload_bytes

    @cached_property
    def sync_pattern(self) -> np.ndarray:
        """Known tones, each sent twice.

        The repeat is what makes `_mfsk.offset_hz` exact: a symbol's measured
        phase carries a timing term that depends on its tone, and across a
        pair sharing a tone that term cancels.
        """
        half = self.sync_symbols // 2
        bits = dsp.bits.pn_bits(half * self.bits_per_symbol, self.sync_seed)
        return np.repeat(self.tx_bank.symbols_from_bits(bits), 2)

    @cached_property
    def head_pattern(self) -> np.ndarray:
        bits = dsp.bits.pn_bits(self.head_symbols * self.bits_per_symbol,
                                self.head_seed)
        return self.tx_bank.symbols_from_bits(bits)

    @property
    def total_symbols(self) -> int:
        return self.head_symbols + self.sync_symbols + self.payload_symbols

    def frame_seconds(self) -> float:
        return (self.total_symbols * self.symbol_seconds
                + self.tail_samples / TX_SAMPLE_RATE)

    def raw_bit_rate(self) -> float:
        return self.symbol_rate * self.bits_per_symbol

    def net_bit_rate(self) -> float:
        """Payload bits per second of frame, everything counted."""
        return self.max_payload_bytes * 8 / self.frame_seconds()

    def describe(self) -> str:
        return (f"M={self.tone_count} spacing={self.spacing_hz:.2f}Hz "
                f"symbol={self.symbol_seconds * 1000:.1f}ms "
                f"K={self.constraint} repeat={self.repeat} "
                f"sync={self.sync_symbols}sym "
                f"payload={self.payload_symbols}sym "
                f"bytes={self.max_payload_bytes} "
                f"frame={self.frame_seconds():.2f}s "
                f"raw={self.raw_bit_rate():.0f}bps "
                f"net={self.net_bit_rate():.1f}bps")

    # -- transmit ---------------------------------------------------------

    def payload_tones(self, payload: bytes) -> np.ndarray:
        coded = self.codec.encode(payload)                 # codec_bits
        repeated = np.tile(coded, self.repeat)             # coded_bits
        spread = self.outer_interleaver.spread(repeated)
        return self.tx_bank.symbols_from_bits(spread)

    def modulate(self, payload: bytes) -> np.ndarray:
        tones = np.concatenate((self.head_pattern, self.sync_pattern,
                                self.payload_tones(payload)))
        audio = _mfsk.modulate(self.tx_bank, tones, self.amplitude)
        fade = min(len(audio), self.tx_symbol_samples)
        audio = audio.copy()
        audio[:fade] *= np.linspace(0.0, 1.0, fade)
        audio = np.concatenate((audio, np.zeros(self.tail_samples)))
        return audio.astype(np.float32)

    # -- receive ----------------------------------------------------------

    def _offset_hypotheses(self) -> np.ndarray:
        step = self.spacing_hz / OFFSET_STEP_DIVISOR
        if step >= OFFSET_SEARCH_HZ:
            return np.array([0.0])
        n = int(np.ceil(OFFSET_SEARCH_HZ / step))
        return np.arange(-n, n + 1) * step

    def acquire(self, audio_12k: np.ndarray) -> dict:
        """Best (start, offset) for the sync pattern.

        Scores every offset hypothesis with `_mfsk.correlate`, keeps the best,
        refines its timing on matched tone energy, then measures the offset
        properly from the sync pattern's repeated pairs -- the grid only has
        to get close enough for the tone detector, the pair estimator does
        the rest.
        """
        audio = np.asarray(audio_12k, dtype=np.float64)
        best = {"score": -1.0, "start": None, "coarse_hz": 0.0}
        for hz in self._offset_hypotheses():
            shifted = shift_hz(audio, hz, RX_SAMPLE_RATE)
            scores, step = _mfsk.correlate(self.rx_bank, shifted,
                                           self.sync_pattern)
            if not len(scores):
                continue
            peak = int(np.argmax(scores))
            if scores[peak] > best["score"]:
                best = {"score": float(scores[peak]), "start": peak * step,
                        "coarse_hz": float(hz), "step": step}
        if best["start"] is None:
            return {"score": 0.0, "start": None, "offset_hz": 0.0}

        shifted = shift_hz(audio, best["coarse_hz"], RX_SAMPLE_RATE)
        start = _mfsk.refine(self.rx_bank, shifted, self.sync_pattern,
                             best["start"], radius=best["step"], step=2)
        fine = _mfsk.offset_hz(self.rx_bank, shifted, start, self.sync_pattern)
        return {"score": best["score"], "start": int(start),
                "coarse_hz": best["coarse_hz"], "fine_hz": float(fine),
                "offset_hz": float(best["coarse_hz"] + fine)}

    def soft_bits(self, magnitudes: np.ndarray) -> np.ndarray:
        """Max-log bit reliabilities from tone magnitudes; positive means zero.

        Three weightings, because on this path the choice is not cosmetic.

        `normalized` is `whale.dsp.mfsk.soft_bits` unchanged: each symbol is
        divided by its own mean tone power, which makes the metric immune to
        the receiver's AGC and absolute level. That is the right call on a
        path whose SNR is steady, and the wrong one here -- it gives a symbol
        sitting in a four-second fade exactly the same weight as a clean one,
        so the Viterbi decoder is handed confident-looking noise. This is the
        default only because it is what the recorded campaigns used.

        `raw` skips the normalisation, so a symbol contributes in proportion
        to the energy that actually arrived. This is square-law combining, and
        it is the classic answer for non-coherent MFSK in fading: faded
        symbols quietly weigh less without anything having to detect the fade.

        `snr` normalises each symbol by its own noise estimate -- the mean of
        the tones other than the strongest, which is a noise-only estimate
        whenever the symbol decision is right -- and then scales by that
        symbol's estimated SNR. It is `raw` with the AGC divided back out, so
        it should keep `raw`'s fade weighting while staying level-independent.

        All three are max-log over squared magnitudes and differ only in the
        per-symbol scale factor.
        """
        metric = np.asarray(magnitudes, dtype=np.float64) ** 2
        if self.soft_metric == "normalized":
            scale = np.maximum(np.mean(metric, axis=1, keepdims=True), 1e-30)
            metric = metric / scale
        elif self.soft_metric == "snr":
            top = np.max(metric, axis=1, keepdims=True)
            rest = (np.sum(metric, axis=1, keepdims=True) - top) / max(
                1, self.tone_count - 1)
            rest = np.maximum(rest, 1e-30)
            # metric/rest is the per-symbol SNR profile; multiplying by the
            # symbol's own excess SNR restores the fade weighting that the
            # division just removed
            excess = np.maximum(top / rest - 1.0, 0.0)
            metric = (metric / rest) * excess
        # "raw" leaves metric alone

        labels = self.rx_bank._gray
        bps = self.bits_per_symbol
        out = np.empty((len(metric), bps))
        for bit in range(bps):
            set_bits = (labels >> (bps - 1 - bit)) & 1
            out[:, bit] = (np.max(metric[:, set_bits == 0], axis=1)
                           - np.max(metric[:, set_bits == 1], axis=1))
        return out.reshape(-1)

    def soft_payload_bits(self, audio_12k, start, offset_hz):
        """Per-transmitted-bit soft metrics, de-interleaved and combined."""
        audio = shift_hz(np.asarray(audio_12k, dtype=np.float64),
                         offset_hz, RX_SAMPLE_RATE)
        payload_start = start + self.sync_symbols * self.rx_symbol_samples
        values = _mfsk.analyze(self.rx_bank, audio, payload_start,
                               self.payload_symbols)
        if values is None:
            return None, None
        soft = self.soft_bits(np.abs(values))
        gathered = self.outer_interleaver.gather(soft)
        combined = gathered.reshape(self.repeat, self.codec_bits).sum(axis=0)
        return combined, values

    def demodulate(self, audio_12k: np.ndarray) -> dict:
        result = {"synced": False, "payload": None, "crc_ok": False,
                  "sync_score": 0.0, "offset_hz": None, "start_index": None,
                  "tone_snr_db": None, "meta": None}
        acq = self.acquire(audio_12k)
        result["sync_score"] = acq["score"]
        if acq["start"] is None:
            return result
        result["start_index"] = acq["start"]
        result["offset_hz"] = acq["offset_hz"]
        result["synced"] = True

        combined, values = self.soft_payload_bits(
            audio_12k, acq["start"], acq["offset_hz"])
        if combined is None:
            result["synced"] = False
            return result
        result["tone_snr_db"] = self.tone_snr_db(values)
        payload, meta = self.codec.decode_soft(combined)
        result["payload"] = payload
        result["crc_ok"] = bool(meta.get("crc_ok"))
        result["meta"] = meta
        return result

    @staticmethod
    def tone_snr_db(values: np.ndarray) -> float:
        """Crude per-symbol SNR from the winning tone against the rest.

        Diagnostic only: it assumes the strongest tone is the transmitted one,
        which stops being true exactly where the mode stops working, so it
        reads optimistically at the bottom of the range. It is here to tell a
        dead path from a marginal one, not to calibrate anything.
        """
        power = np.abs(np.asarray(values)) ** 2
        top = np.max(power, axis=1)
        rest = (np.sum(power, axis=1) - top) / max(1, power.shape[1] - 1)
        ratio = np.mean(top) / max(np.mean(rest), 1e-30)
        return float(10 * np.log10(max(ratio - 1.0, 1e-12)))


def mode_for(tone_count, *, frame_seconds=None, payload_symbols=None,
             repeat=1, constraint=7, sync_seconds=0.5, **kwargs) -> MfskMode:
    """Build a mode by target frame duration instead of symbol count.

    `payload_symbols` is snapped so that payload_symbols * bits_per_symbol is
    divisible by `repeat` and the resulting codec bit count is even, which the
    rate-1/2 codec requires.
    """
    probe = MfskMode(tone_count=tone_count, payload_symbols=8,
                     repeat=1, constraint=constraint,
                     sync_seconds=sync_seconds, **kwargs)
    if payload_symbols is None:
        if frame_seconds is None:
            raise ValueError("give frame_seconds or payload_symbols")
        overhead = (probe.head_symbols + probe.sync_symbols) * probe.symbol_seconds
        overhead += probe.tail_samples / TX_SAMPLE_RATE
        payload_symbols = int((frame_seconds - overhead) / probe.symbol_seconds)
    bps = probe.bits_per_symbol
    # coded_bits must be divisible by repeat, and codec_bits must be even
    quantum = repeat * 2
    while payload_symbols > 0 and (payload_symbols * bps) % quantum:
        payload_symbols -= 1
    if payload_symbols <= 0:
        raise ValueError("frame_seconds is too short for this geometry")
    return MfskMode(tone_count=tone_count, payload_symbols=payload_symbols,
                    repeat=repeat, constraint=constraint,
                    sync_seconds=sync_seconds, **kwargs)
