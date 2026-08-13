"""Continuous-phase binary FSK modulate/demodulate for the AFSK link.

300 baud, 700/1300 Hz tones: FSK carries information in frequency only, so it
survives the amplitude gating/compression this HT+IC-705 hardware chain shows
on receive (AGC, limiting) far better than an amplitude-sensitive scheme
would. Values chosen for this reason, not for throughput -- this is a
correctness-first v1.

Measured channel: the hardware test bench is one IC-705 (STA1) and one HT via
a Digirig-style interface (STA2), both squelched, on the bench per
whale/hw/radios.py. scripts/measure_snr.py measures in-band SNR (500-1600 Hz,
signal RMS in that band vs. a PSD-scaled noise estimate from the adjacent
300-550/1750-2050 Hz side bands, taken from a live squelch-open reception --
squelch-closed audio is just the receiver muted, not a usable noise
reference) and found:

    STA1 -> STA2: ~15 dB
    STA2 -> STA1: ~12 dB

Both comfortably clear PROFILE_300's confidence_threshold below. Re-run
measure_snr.py after any antenna, power, or placement change on the bench;
these numbers drift with the physical setup, not with anything in this
module.
"""

from dataclasses import dataclass

import numpy as np
from scipy.signal import correlate

from whale import framing

SAMPLE_RATE = 48000
BAUD = 300
FREQ_0 = 700.0
FREQ_1 = 1300.0
CONFIDENCE_THRESHOLD = 4.0
CHUNK_SIZE = 40  # link-layer payload bytes per DATA frame, see whale/link.py

MAX_FRAME_BITS = len(framing.SYNC_BITS) + 8 + 8 * framing.MAX_PAYLOAD_BYTES + 16


@dataclass(frozen=True)
class Profile:
    """One speed/tone setting for the modem. Everything that depends on
    baud or tone frequencies -- modulate(), demodulate(), frame timing, and
    the link layer's chunk size and ACK timeouts -- should come from a
    Profile passed around explicitly, not from these module constants.
    The constants above exist only to define PROFILE_300, today's only
    profile and the default everywhere a caller doesn't pass one."""

    name: str
    baud: int
    freq0: float
    freq1: float
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    chunk_size: int = CHUNK_SIZE


PROFILE_300 = Profile(name="300baud", baud=BAUD, freq0=FREQ_0, freq1=FREQ_1)


def _cpfsk_tone(bits, sps, sample_rate, freq0, freq1):
    freqs_per_bit = np.where(np.asarray(bits) == 0, freq0, freq1)
    inst_freq = np.repeat(freqs_per_bit, sps)
    phase = 2 * np.pi * np.cumsum(inst_freq) / sample_rate
    return np.cos(phase)


def _apply_ramp(signal, sample_rate, ramp_ms=5):
    ramp_len = min(int(sample_rate * ramp_ms / 1000), len(signal) // 2)
    if ramp_len <= 0:
        return signal
    window = np.hanning(2 * ramp_len)
    signal = signal.copy()
    signal[:ramp_len] *= window[:ramp_len]
    signal[-ramp_len:] *= window[ramp_len:]
    return signal


def modulate(payload: bytes, profile: Profile = PROFILE_300, sample_rate=SAMPLE_RATE, amplitude=0.6):
    sps = round(sample_rate / profile.baud)
    bits = framing.build_frame_bits(payload)
    tone = _cpfsk_tone(bits, sps, sample_rate, profile.freq0, profile.freq1)
    audio = amplitude * tone
    return _apply_ramp(audio, sample_rate).astype(np.float32)


def _tone_energy_diff(audio, sample_rate, sps, freq0, freq1):
    n = np.arange(len(audio))
    b0 = audio * np.exp(-1j * 2 * np.pi * freq0 * n / sample_rate)
    b1 = audio * np.exp(-1j * 2 * np.pi * freq1 * n / sample_rate)
    box = np.ones(sps) / sps
    e0 = np.abs(np.convolve(b0, box))
    e1 = np.abs(np.convolve(b1, box))
    norm0 = np.sqrt(np.mean(e0 ** 2)) or 1.0
    norm1 = np.sqrt(np.mean(e1 ** 2)) or 1.0
    return e1 / norm1 - e0 / norm0


def _sync_template(sps, sample_rate, freq0, freq1):
    tone = _cpfsk_tone(framing.SYNC_BITS, sps, sample_rate, freq0, freq1)
    return _tone_energy_diff(tone, sample_rate, sps, freq0, freq1)


def frame_seconds(payload_len=framing.MAX_PAYLOAD_BYTES, profile: Profile = PROFILE_300):
    n_bits = len(framing.SYNC_BITS) + 8 + 8 * payload_len + 16 + len(framing.TAIL_PAD_BITS)
    return n_bits / profile.baud


def demodulate(audio, profile: Profile = PROFILE_300, sample_rate=SAMPLE_RATE):
    """Tries to find and decode one frame in `audio`. Returns a dict with at
    least 'synced' and 'payload' (None if nothing usable was found)."""
    sps = round(sample_rate / profile.baud)
    audio = np.asarray(audio, dtype=np.float64)

    diff = _tone_energy_diff(audio, sample_rate, sps, profile.freq0, profile.freq1)
    template = _sync_template(sps, sample_rate, profile.freq0, profile.freq1)
    if len(diff) < len(template):
        return {"synced": False, "payload": None}

    corr = correlate(diff, template, mode="valid", method="fft")
    i_star = int(np.argmax(corr))
    peak = float(corr[i_star])
    noise_floor = float(np.median(np.abs(corr)))
    confidence = peak / noise_floor if noise_floor > 0 else 0.0

    if confidence < profile.confidence_threshold:
        return {"synced": False, "confidence": confidence, "payload": None}

    n_sync = len(framing.SYNC_BITS)
    first_index = i_star + (sps - 1)
    max_symbols = (len(diff) - 1 - first_index) // sps + 1
    if max_symbols < n_sync + 8:
        # Not even the length byte has fully arrived yet -- this may well
        # be a real frame still streaming in; say nothing definitive rather
        # than reporting it as a dead end.
        return {"synced": False, "confidence": confidence, "payload": None}

    num_symbols = min(max_symbols, MAX_FRAME_BITS)
    sample_indices = first_index + sps * np.arange(num_symbols)
    decoded_bits = (diff[sample_indices] > 0).astype(int).tolist()

    payload = framing.parse_frame_bits(decoded_bits[n_sync:])
    length = framing.bits_to_bytes(decoded_bits[n_sync:n_sync + 8])[0]
    total_bits_needed = n_sync + 8 + 8 * length + 16
    result = {
        "synced": payload is not None,
        "confidence": confidence,
        "start_index": i_star,
        "payload": payload,
    }
    if payload is not None or max_symbols >= total_bits_needed:
        # Either it decoded, or we had every bit the claimed length says it
        # needed and it *still* didn't check out -- this one's done, safe
        # to skip past. If neither holds, the frame may just still be
        # arriving; leave end_index out so the caller keeps waiting on the
        # same buffer instead of discarding a frame that hasn't finished.
        result["end_index"] = first_index + sps * min(total_bits_needed, num_symbols)
    return result
