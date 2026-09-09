"""Common incremental receive adapter for waveform modes.

Modes intentionally keep their existing ``decode`` functions: those are also
used by capture replay and qualification.  ``StreamingDecoder`` supplies the
live link with the missing state boundary.  It searches a short rolling
window until a mode locks, then retains only that candidate frame and runs
the mode decoder when enough samples have arrived.  This keeps expensive
OFDM/FEC work out of every receive poll while allowing every mode to consume
the same audio stream incrementally.
"""

from __future__ import annotations

import time
from typing import Callable

import numpy as np

from whale import framing


class StreamingDecoder:
    """Feed one waveform mode arbitrary receive chunks.

    ``feed`` returns the first terminal decoder result produced by that chunk,
    or ``None``.  Result indices are in the decoder's stream coordinates, so
    the link can consume them directly from its receive buffer.
    """

    ACQUISITION_SECONDS = 1.75
    PROBE_SECONDS = 0.25
    FRAME_END_EARLY_SECONDS = 0.15

    def __init__(self, mode, decode: Callable[[np.ndarray], dict], *,
                 head_seconds=None):
        self.mode = mode
        self._decode = decode
        self.head_seconds = head_seconds
        self._probe_samples = max(1, round(mode.rx_sample_rate * self.PROBE_SECONDS))
        self._acquisition_samples = max(
            self._probe_samples,
            round(mode.rx_sample_rate * self.ACQUISITION_SECONDS),
        )
        self.reset()

    def reset(self) -> None:
        self._audio = np.zeros(0, dtype=np.float32)
        self._audio_start = 0
        self._total_samples = 0
        self._next_probe = self._probe_samples
        self._locked_start = None
        self._locked_frame_samples = None
        self._next_frame_decode = None
        self.decode_count = 0
        self.last_decode_cpu_seconds = 0.0
        self.last_decode_wall_seconds = 0.0
        self.last_decode_samples = 0

    @property
    def locked(self) -> bool:
        return self._locked_start is not None

    def _run_decode(self, audio: np.ndarray, offset: int):
        cpu0 = time.thread_time()
        wall0 = time.perf_counter()
        result = self._decode(audio)
        self.last_decode_cpu_seconds = time.thread_time() - cpu0
        self.last_decode_wall_seconds = time.perf_counter() - wall0
        self.last_decode_samples = len(audio)
        self.decode_count += 1
        if result is None:
            return None
        result = dict(result)
        for key in ("start_index", "sync_end_index", "end_index"):
            if result.get(key) is not None:
                result[key] += offset
        return result

    @staticmethod
    def _has_lock(result, threshold):
        return (result.get("start_index") is not None
                and result.get("confidence", 0.0) >= threshold
                and "end_index" not in result)

    def _frame_samples(self, result=None) -> int:
        # airtime() is the only size contract common to every mode.  It may
        # include a leading pad, so this is deliberately conservative: an
        # early attempt is harmless (the mode returns ``frame truncated``),
        # while a late attempt avoids repeatedly decoding a growing frame.
        # CPFSK exposes the checked length in its partial acquisition result;
        # use it when available so a short control/ACK frame is not delayed
        # behind the mode's maximum DATA-frame airtime.
        length = None if result is None else result.get("_declared_length")
        if length is not None:
            baud = getattr(self.mode, "baud", None)
            if baud:
                sync_len = len(framing.sync_bits(baud))
                bits = sync_len + framing.frame_bits_for_length(length)
                return max(self._probe_samples,
                           round(bits / baud * self.mode.rx_sample_rate))
        if result is not None and result.get("_hard_bits") is not None:
            baud = getattr(self.mode, "baud", None)
            if baud:
                sync_len = len(framing.sync_bits(baud))
                length = framing.declared_length(result["_hard_bits"][sync_len:])
                if length is not None:
                    bits = sync_len + framing.frame_bits_for_length(length)
                    return max(self._probe_samples,
                               round(bits / baud * self.mode.rx_sample_rate))
        try:
            seconds = self.mode.airtime(self.mode.chunk_size)
        except (AttributeError, TypeError, ValueError):
            seconds = self.ACQUISITION_SECONDS
        return max(self._probe_samples, round(seconds * self.mode.rx_sample_rate))

    def _retry_samples(self) -> int:
        # Once a frame is locked, retry near its expected end often enough to
        # absorb clock drift and filter tails.  This is still much less work
        # than re-running acquisition on every poll, especially for OFDM.
        frame_samples = (self._locked_frame_samples
                         if self._locked_frame_samples is not None
                         else self._frame_samples())
        return max(8, min(self._probe_samples, frame_samples // 40))

    def _feed_chunk(self, audio, *, force=False) -> dict | None:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not len(samples):
            return None
        self._audio = np.concatenate((self._audio, samples))
        self._total_samples += len(samples)

        if self._locked_start is None:
            if not force and self._total_samples < self._next_probe:
                return None
            self._next_probe = self._total_samples + self._probe_samples
            if len(self._audio) > self._acquisition_samples:
                trim = len(self._audio) - self._acquisition_samples
                self._audio = self._audio[trim:]
                self._audio_start += trim
            result = self._run_decode(self._audio, self._audio_start)
            if result is None:
                return None
            if result.get("payload") is not None or result.get("end_index") is not None:
                return result
            if self._has_lock(result, self.mode.confidence_threshold):
                self._locked_start = result["start_index"]
                self._locked_frame_samples = self._frame_samples(result)
                # A decimator/filter boundary can make the final usable
                # sample land one short of the nominal symbol arithmetic.
                # Probe just before the estimate; a genuinely incomplete
                # frame simply returns ``frame truncated`` and is retried on
                # the next probe interval.
                self._next_frame_decode = max(
                    self._locked_start + self._probe_samples,
                    self._locked_start + self._locked_frame_samples
                    - max(8, round(self.mode.rx_sample_rate
                                   * self.FRAME_END_EARLY_SECONDS)),
                )
            return None

        if not force and self._total_samples < self._next_frame_decode:
            return None
        result = self._run_decode(
            # Keep the pre-lock context. HF modes use their common lead to
            # choose the body boundary; slicing exactly at a provisional OFDM
            # lock can remove that lead and make a complete frame look like a
            # new, false acquisition at offset zero.
            self._audio,
            self._audio_start,
        )
        if result is not None:
            if result.get("payload") is not None or result.get("end_index") is not None:
                return result
        self._next_frame_decode = self._total_samples + self._retry_samples()
        return None

    def feed(self, audio) -> dict | None:
        """Feed audio, preserving streaming semantics for large callbacks.

        Test transports and some audio backends can hand us an entire keying
        in one callback.  Process such a callback in probe-sized pieces so a
        large delivery cannot trim the sync word before acquisition runs.
        """
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not len(samples):
            return None
        for start in range(0, len(samples), self._probe_samples):
            end = min(len(samples), start + self._probe_samples)
            # A bulk callback's short final piece is the only indication this
            # adapter gets that no more samples from that delivery are
            # pending. Probe it so exact-length synthetic transports do not
            # leave a complete frame one poll short.
            result = self._feed_chunk(
                samples[start:end],
                force=len(samples) > self._probe_samples and end == len(samples),
            )
            if result is not None:
                return result
        return None


def decoder_for(mode, *, head_seconds=None) -> StreamingDecoder:
    """Build the live decoder for any object satisfying WaveformMode."""
    def decode(audio):
        if head_seconds is None:
            return mode.decode(audio)
        return mode.decode(audio, head_seconds=head_seconds)
    return StreamingDecoder(mode, decode, head_seconds=head_seconds)
