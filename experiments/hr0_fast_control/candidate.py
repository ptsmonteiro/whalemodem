"""Experimental short/full K9 MFSK controls; deliberately unregistered."""
from dataclasses import dataclass
from functools import cached_property
import math
import numpy as np
from scipy.signal import hilbert
from whale import dsp, rx_audio
from whale.dsp import mfsk
from whale.modes import hf_lead


@dataclass(frozen=True)
class FastControl:
    tones: int = 16
    symbol_samples: int = 512
    sync_symbols: int = 24
    first_bin: int = 8
    tx_sample_rate: int = 48_000
    rx_sample_rate: int = rx_audio.DECODE_SAMPLE_RATE
    confidence_threshold: float = 0.10
    chunk_size: int = 32
    # Offline placeholders only: never advertise as negotiated modes.
    mode_id: int = -1

    @property
    def head_match_allowance_seconds(self):
        return hf_lead.BLOCK_SAMPLES / self.tx_sample_rate

    @property
    def baud(self):
        return self.bank.symbol_rate

    @property
    def name(self):
        return f"fast{self.tones}_k9" + (f"_{self.symbol_samples}" if self.symbol_samples not in (512, 768) else "")

    @cached_property
    def bank(self):
        return mfsk.ToneBank(self.tx_sample_rate, self.symbol_samples,
                             self.first_bin, self.tones)

    @cached_property
    def rx_bank(self):
        return mfsk.ToneBank(self.rx_sample_rate,
                             self.symbol_samples // rx_audio.DECIMATION,
                             self.first_bin, self.tones)

    @cached_property
    def sync(self):
        return np.repeat(self.bank.symbols_from_bits(dsp.bits.pn_bits(
            self.sync_symbols // 2 * self.bank.bits_per_symbol, 0x1D35B)), 2)

    @cached_property
    def codecs(self):
        codecs = []
        for capacity in (12, 42):
            symbols = math.ceil((2 * ((capacity + 6) * 8 + 8)) /
                                self.bank.bits_per_symbol)
            while symbols * self.bank.bits_per_symbol % 2:
                symbols += 1
            count = symbols * self.bank.bits_per_symbol
            multiplier = min((i for i in range(2, count) if math.gcd(i, count) == 1),
                             key=lambda i: abs(i - count * (3 - math.sqrt(5)) / 2))
            codecs.append((symbols, dsp.PacketCodec(count,
                dsp.interleave.multiplicative(count, multiplier),
                0x17A6E + capacity, dsp.K9)))
        return codecs

    def _body(self, length):
        if not 0 <= length <= 42:
            raise ValueError("payload length must be between 0 and 42")
        return self.codecs[int(length > 12)]

    def airtime(self, payload_len):
        return (hf_lead.MIN_SAMPLES + (self.sync_symbols +
                self._body(payload_len)[0]) * self.symbol_samples + 960) / 48000

    def encode(self, payload, *, include_head=True, head_seconds=None):
        _, codec = self._body(len(payload))
        tones = np.concatenate((self.sync,
                                self.bank.symbols_from_bits(codec.encode(payload))))
        # Existing lead for comparison only; promotion needs a new signature.
        return np.concatenate((hf_lead.modulate(hf_lead.HR0_LABEL, head_seconds) if include_head else np.zeros(0),
            mfsk.modulate(self.bank, tones, 0.13 * np.sqrt(2)),
            np.zeros(960))).astype(np.float32)

    def decode(self, audio, **kwargs):
        result = dict(payload=None, synced=False, confidence=0.0)
        try:
            samples = np.asarray(audio, dtype=float)
        except (TypeError, ValueError):
            return result
        if samples.ndim != 1:
            return result
        if not np.all(np.isfinite(samples)) or len(samples) < self.sync_symbols * self.rx_bank.symbol_samples:
            return result
        analytic = hilbert(samples)
        index = np.arange(len(samples))
        best = None
        # Whole-bin search covers the common +/-46 Hz CFO requirement.
        for coarse_hz in (-self.bank.spacing_hz, 0., self.bank.spacing_hz):
            working = np.real(analytic * np.exp(-2j * np.pi * coarse_hz * index / self.rx_sample_rate))
            scores, step = mfsk.correlate(self.rx_bank, working, self.sync)
            if len(scores):
                at = int(np.argmax(scores))
                if best is None or scores[at] > best[0]:
                    best = (float(scores[at]), at * step, step, coarse_hz, working)
        if best is None:
            return result
        confidence, start, step, coarse_hz, working = best
        if confidence < self.confidence_threshold:
            result['confidence'] = confidence
            return result
        start = mfsk.refine(self.rx_bank, working, self.sync, start, radius=step)
        residual = mfsk.offset_hz(self.rx_bank, working, start, self.sync)
        body_start = start + self.sync_symbols * self.rx_bank.symbol_samples
        result.update(synced=True, confidence=confidence, start_index=start,
                      cfo_hz=coarse_hz + residual, sync_end_index=body_start)
        for symbols, codec in self.codecs:
            values = mfsk.analyze(self.rx_bank, working, body_start, symbols, residual)
            if values is None:
                result['failure'] = 'frame truncated'
                return result
            payload, meta = codec.decode_soft(mfsk.soft_bits(self.rx_bank, np.abs(values)))
            if payload is not None:
                result.update(meta)
                observed, score = hf_lead.measure(samples, start, hf_lead.HR0_LABEL, kwargs.get('head_seconds'))
                result.update(head_blocks_observed=observed,
                              head_seconds_received=hf_lead.seconds_received(observed),
                              head_match=score)
                result.update(payload=payload, end_index=min(len(samples), body_start + symbols * self.rx_bank.symbol_samples + 960 // rx_audio.DECIMATION))
                return result
        # A complete failed full-body hypothesis can be discarded. Without
        # end_index the link treats it as perpetually incomplete audio.
        result.update(meta)
        result['end_index'] = min(len(samples), body_start +
            self.codecs[-1][0] * self.rx_bank.symbol_samples +
            960 // rx_audio.DECIMATION)
        result['failure'] = 'CRC failed'
        return result


FAST16 = FastControl()
FAST32 = FastControl(tones=32, symbol_samples=768, sync_symbols=16, first_bin=7)

MARGIN32 = FastControl(tones=32, symbol_samples=1024, sync_symbols=16, first_bin=12)
