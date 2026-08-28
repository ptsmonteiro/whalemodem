"""Receive-audio conversion from the sound-card clock to the DSP clock.

The radio devices remain at 48 kHz, which is the rate used to generate every
transmitted waveform.  Receive buffering and all production decoders use
12 kHz.  Keeping the conversion here makes it happen once per captured
sample rather than once per candidate waveform on every decode poll.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import firwin, lfilter


CAPTURE_SAMPLE_RATE = 48_000
DECODE_SAMPLE_RATE = 12_000
DECIMATION = CAPTURE_SAMPLE_RATE // DECODE_SAMPLE_RATE

if CAPTURE_SAMPLE_RATE % DECODE_SAMPLE_RATE:
    raise RuntimeError("receive sample rates must have an integer ratio")

# All current on-air energy ends below 3.15 kHz.  This leaves a broad
# transition band before the new 6 kHz Nyquist limit while rejecting audio
# that would otherwise alias into the modem band.  An odd tap count gives a
# whole-number group delay at both rates (64 capture / 16 decode samples).
FILTER_TAPS = firwin(129, 4_500.0, fs=CAPTURE_SAMPLE_RATE).astype(np.float64)
FILTER_DELAY_CAPTURE_SAMPLES = (len(FILTER_TAPS) - 1) // 2
FILTER_DELAY_DECODE_SAMPLES = FILTER_DELAY_CAPTURE_SAMPLES // DECIMATION


class ReceiveDecimator:
    """Stateful anti-aliased 48 kHz to 12 kHz converter.

    ``process`` may be called with arbitrary chunk sizes.  Filter history and
    decimation phase carry across calls, which is required for a PortAudio
    callback stream: filtering each callback independently would introduce a
    discontinuity at every block boundary.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._zi = np.zeros(len(FILTER_TAPS) - 1, dtype=np.float64)
        self._input_samples = 0

    def process(self, audio) -> np.ndarray:
        samples = np.asarray(audio, dtype=np.float64).reshape(-1)
        if not len(samples):
            return np.zeros(0, dtype=np.float32)
        filtered, self._zi = lfilter(FILTER_TAPS, [1.0], samples, zi=self._zi)
        first = (-self._input_samples) % DECIMATION
        self._input_samples += len(samples)
        return filtered[first::DECIMATION].astype(np.float32)


def downsample(audio) -> np.ndarray:
    """Convert one complete 48 kHz capture to the production RX rate."""
    return ReceiveDecimator().process(audio)
