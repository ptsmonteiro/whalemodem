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
    The constants above exist only to define PROFILE_300, the baseline
    control profile and the default everywhere a caller doesn't pass one.

    mode_id is the on-air identifier used by whale/link.py's speed
    negotiation -- it's what gets sent over the radio, not `name`, so it
    must stay stable once anything has shipped with it.

    If a future speed mode needs a genuinely different modulation (not just
    different baud/tones on this same CPFSK scheme), Profile would need a
    codec_id and a dispatch table for modulate/demodulate. Not needed yet:
    every profile below still uses the CPFSK code in this module.
    """

    name: str
    mode_id: int
    baud: int
    freq0: float
    freq1: float
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    chunk_size: int = CHUNK_SIZE


PROFILE_300 = Profile(name="300baud", mode_id=0, baud=BAUD, freq0=FREQ_0, freq1=FREQ_1)

# Second speed. Originally tried at 700/1900 Hz (same 2:1 tone-separation-
# to-baud ratio as PROFILE_300); that measured 9-10 dB SNR on the bench
# (vs. 17-20 dB for PROFILE_300) and DATA frames failed outright (0/5
# ACKed) -- the IC-705/HT/Digirig audio chain's FM discriminator +
# de-emphasis rolls off well before 1900 Hz. Dropped freq1 to 1500 Hz to
# stay inside the flatter part of that passband; re-validated on the bench
# with scripts/measure_snr.py --profile 600baud and scripts/hw_smoke_link.py
# --profile 600baud, see whale/afsk.py module docstring for the numbers.
#
# scripts/sweep_baud_payload.py --skip-baud --baud 600 found the frame-size
# ceiling at this baud: 2-120 byte AFSK payloads passed 100% both
# directions; 160 bytes broke down 0/5 on the ic705->ht leg specifically
# (ht->ic705 stayed 100%) via the same false/duplicate-sync-lock signature
# PROFILE_1200 hit at its own ceiling (end_index ~1.2x the expected frame
# length), not gradual SNR falloff -- and notably the leg that failed here
# is the *stronger*-SNR one, so this ceiling is a framing effect, not
# simply "whichever leg has less SNR." chunk_size=100 (-> 102-byte AFSK
# payload for DATA, see whale/link.py's frame_airtime calc) mirrors
# PROFILE_1200's margin choice, keeping clearance below both the 120-byte
# clean point and the 160-byte failure mode.
PROFILE_600 = Profile(name="600baud", mode_id=1, baud=600, freq0=700.0, freq1=1500.0,
                       chunk_size=100)

# Third speed. PROFILE_600's tone-widening approach (700/1500 -> pushing
# further apart) hit a hard wall: scripts/measure_band_edges.py found the
# usable band runs from ~600 Hz to ~2300 Hz, but scripts/sweep_baud_600_2300.py
# showed that even at those edges, baud tops out at 600 -- 700 baud fails
# 0/5 both directions, and padding 1s of settle noise before/after the frame
# ruled out a PTT/timing transient as the cause. The failure is the tone
# placement itself: wide separation pushed to the passband edges spends
# more of each symbol transition in the region of worst group delay/rolloff
# for this FM audio chain (mic/speaker filtering, de-emphasis).
#
# Bell 202 (AX.25 1200bps) tones -- 1200/2200 Hz, narrower 1000 Hz
# separation, centered in the flat part of the passband -- don't have that
# problem. Re-running the baud sweep at 1200/2200 Hz cleared 1200 baud
# cleanly (100% both directions) and broke down at 1400 (0/5 ht->ic705,
# confidence flatlined -- a real passband wall, not a marginal case).
# scripts/sweep_payload_1200_2200.py then found the frame-size ceiling at
# that baud: 120-byte AFSK payloads passed 100% both directions; 160 bytes
# broke down on the ic705->ht leg specifically via false/duplicate sync
# lock (end_index ~2x the expected frame length), not gradual SNR falloff.
# chunk_size=100 (-> 102-byte AFSK payload for DATA, see whale/link.py's
# frame_airtime calc) keeps meaningful margin below both the 120-byte
# clean point and the 160-byte failure mode.
PROFILE_1200 = Profile(name="1200baud", mode_id=2, baud=1200, freq0=1200.0, freq1=2200.0,
                        chunk_size=100)

# Slowest -> fastest. Index order is also step order for mid-session
# adaptation (whale/link.py steps to PROFILES[i-1] / PROFILES[i+1]).
PROFILES = [PROFILE_300, PROFILE_600, PROFILE_1200]
PROFILES_BY_ID = {p.mode_id: p for p in PROFILES}

# Always used for CONNECT/CONNECT_ACK (before speed is agreed) and, per
# whale/link.py's design, for every other control-plane packet (DISC,
# MODE_REQ/MODE_ACK) regardless of the currently negotiated data speed --
# the control plane always runs at the most robust profile so it keeps
# working even when the data channel is struggling. Only PT_DATA/PT_DATA_ACK
# ever use the negotiated profile.
CONTROL_PROFILE = PROFILE_300


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
    bits = framing.build_frame_bits(payload, baud=profile.baud)
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
    n_bits = (len(framing.head_pad_bits(profile.baud)) + len(framing.SYNC_BITS) + 8 + 8 * payload_len + 16
              + len(framing.tail_pad_bits(profile.baud)))
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
