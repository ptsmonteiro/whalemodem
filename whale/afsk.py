"""Continuous-phase binary FSK modulate/demodulate for the AFSK link.

300 baud, 1200/1800 Hz tones: FSK carries information in frequency only, so it
survives the amplitude gating/compression this HT+IC-705 hardware chain shows
on receive (AGC, limiting) far better than an amplitude-sensitive scheme
would. Values chosen for this reason, not for throughput -- this is a
correctness-first v1.

PROFILE_300 and PROFILE_600 are centred on 1500 Hz, the middle of the
~600-2300 Hz usable band scripts/measure_band_edges.py measured on this bench.
Each keeps the tone *separation* its own sweeps established -- 600 Hz at 300
baud, 800 at 600 -- and places that separation symmetrically about 1500 Hz
rather than wherever its own history happened to leave it. The profiles were
developed one at a time and had drifted to different centres (1000 and 1100
Hz), so the passband margin a profile had was an accident of when it was
tuned; a common centre gives each the most headroom its separation allows, and
equal headroom on both sides. Both measured better after the move, not just
no worse -- see the SNR figures below.

PROFILE_1200 is the exception, and it is a measured one rather than an
oversight: it stays at 1200/2200 Hz (centre 1700). Centring it was tried on
the radios and does not work on this chain. See PROFILE_1200's note for the
runs.

Measured channel: the hardware test bench is one IC-705 (STA1) and one HT via
a Digirig-style interface (STA2), both squelched, on the bench per
whale/hw/radios.py. scripts/measure_snr.py measures in-band SNR (signal RMS
across the profile's tone pair vs. a PSD-scaled noise estimate from the side
bands just outside it, taken from a live squelch-open reception --
squelch-closed audio is just the receiver muted, not a usable noise
reference) and found, at the tone placements below:

                          STA1 -> STA2    STA2 -> STA1
    PROFILE_300               21.2 dB         15.2 dB
    PROFILE_600               13.0 dB          8.8 dB
    PROFILE_1200              16.3 dB          7.5 dB

The 300/600 figures are with those profiles centred on 1500 Hz. PROFILE_300
improved by re-centring, from ~15/~12 dB at its old 700/1300 Hz.

Do not read PROFILE_600's row as worse than the others, or as worse than the
16.3/11.3 dB its previous 1100/1900 Hz tones scored. These bands are derived
per profile from the profile's own tones, so a narrower tone pair is scored
through a narrower band and the rows are not comparable with each other.
Measured properly -- both pairs through one fixed band, interleaved -- the
1200/1800 Hz pair this profile now uses beats 1100/1900 by 2.2 dB in one
direction and ties in the other. See PROFILE_600's note.

The ht->ic705 leg is consistently the weaker of the two, and PROFILE_1200
leans on it hardest -- that is the leg that fails first whenever this profile
is moved (see its note).

Note that SNR no longer shows up in
the sync-detection margin: the normalised measure demodulate() uses scores
a genuine sync word around 0.98 whether the link is at 20 dB or 0 dB (see
CONFIDENCE_THRESHOLD). What SNR buys at these levels is bit errors in the
frame body, which the CRC catches -- so it governs how often a frame has
to be retransmitted, not whether the receiver notices it at all. Re-run
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

# The centre the profiles share: a profile's tone pair is CENTER_FREQ +/-
# half its own separation. PROFILE_1200 is the one exception and spells its
# tones out literally -- see its comment for the bench runs behind that.
CENTER_FREQ = 1500.0


def tones(separation, center=CENTER_FREQ):
    """The (freq0, freq1) pair `separation` Hz apart, centred on `center`."""
    return center - separation / 2, center + separation / 2


FREQ_0, FREQ_1 = tones(600.0)  # 1200/1800 Hz

# Sync-detection threshold, as a normalised correlation in [0, 1]: 1 means
# the window has exactly the sync word's shape. See
# _normalised_correlation for why the measure is normalised rather than the
# ratio-to-noise-floor this used to be, and demodulate() for how it's used.
#
# Calibrated against the 20 real off-air captures in scratch_captures_600ack
# and against synthetic frames from 20 dB SNR down to 0 dB:
#
#   genuine sync word present   0.93 - 0.99   (all profiles, all SNRs, and
#                                              still 0.94 on the captures
#                                              whose frames fail CRC)
#   no sync word present        0.19 - 0.49   (off-air noise, and modulated
#                                              data carrying no sync)
#
# 0.7 sits in the middle of that gap. The gap is what matters here: the
# previous peak/median-noise-floor measure scored those same no-sync
# captures at 7.8 to 214 against a threshold of 4.0, i.e. it false-synced
# on every one of them.
CONFIDENCE_THRESHOLD = 0.7

# Windows quieter than this fraction of the buffer's own RMS window energy
# are treated as having this much energy instead, so normalising by a
# near-silent window cannot manufacture a large ratio out of a tiny
# correlation. See _normalised_correlation.
_ENERGY_FLOOR_FRACTION = 0.05

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

# Second speed. 800 Hz of tone separation, which was arrived at the hard
# way: 700/1900 Hz (a 1200 Hz spread, the same 2:1 separation-to-baud ratio
# as PROFILE_300) measured 9-10 dB SNR on the bench and failed outright
# (0/5 DATA frames ACKed), and narrowing to 700/1500 fixed it. That was
# read at the time as "the audio chain rolls off well before 1900 Hz",
# which the later band-edge measurement contradicts -- PROFILE_1200 runs a
# tone at 2200 Hz. The separation, not the upper tone, is what mattered.
# Centred on 1500 Hz that separation is 1100/1900: the same spread that
# worked, now with 500 Hz of band either side instead of 100 Hz below and
# 800 Hz above.
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
# The separation was then narrowed again, from 800 Hz to 600, after the
# re-centring: 1200/1800 A/B'd against 1100/1900 on the bench, interleaved
# trial by trial so drift could not favour either. Both decoded 100% (32/32
# at the 102-byte production frame, 12/12 at 160 bytes), but the narrower
# pair scored better everywhere it differed -- 18.8 dB vs 16.6 dB
# ic705->ht through one fixed measurement band, level on the reverse leg,
# and steadier sync confidence on large frames (0.985 min vs a 0.847
# wobble). Careful with the SNR figure: scoring each pair through bands
# derived from its own tones, as measure_snr.py does by default, reverses
# the result and flatters the wider pair -- the comparison has to hold the
# band fixed.
#
# Why narrower wins here, most likely: non-coherent FSK detection is
# orthogonal when the tone separation is an integer multiple of the baud
# rate. 600 Hz at 600 baud is exactly 1.0 -- the matched filter for one
# tone sees a null from the other. 800 Hz gave 1.33, which sits between
# orthogonality points and leaks each tone into the other's filter.
# PROFILE_300 lands on 2.0 by the same arithmetic. (PROFILE_1200's 0.833 is
# the odd one out, and it is also the profile that will not move -- see its
# note, though the mechanism there looks like absolute tone placement
# rather than this.)
#
# These are now the same two tones PROFILE_300 uses, which is safe and was
# checked rather than assumed: the sync template discriminates on symbol
# timing, not tone frequency, so a 300 baud frame scores 0.61 against the
# 600 baud correlator and vice versa 0.38 -- both far below the 0.7 lock
# threshold, and unchanged from when the tones differed. A full acceptance
# run with both profiles live on air produced no near-miss decodes.
PROFILE_600 = Profile(name="600baud", mode_id=1, baud=600, freq0=tones(600.0)[0],
                       freq1=tones(600.0)[1], chunk_size=100)

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
# separation -- don't have that problem. Re-running the baud sweep at
# 1200/2200 Hz cleared 1200 baud cleanly (100% both directions) and broke
# down at 1400 (0/5 ht->ic705, confidence flatlined -- a real passband
# wall, not a marginal case). scripts/sweep_payload_1200_2200.py then found
# the frame-size ceiling at that baud: 120-byte AFSK payloads passed 100%
# both directions; 160 bytes broke down on the ic705->ht leg specifically
# via false/duplicate sync lock (end_index ~2x the expected frame length),
# not gradual SNR falloff. chunk_size=100 (-> 102-byte AFSK payload for
# DATA, see whale/link.py's frame_airtime calc) keeps meaningful margin
# below both the 120-byte clean point and the 160-byte failure mode.
#
# These tones are NOT centred on 1500 Hz the way the two slower profiles
# are, and the exception is deliberate. Moving this profile down to the
# common centre was tried on the bench (connect + 96 bytes each direction
# per placement, the hw_smoke_link flow) and this profile alone will not
# take it:
#
#   1200/2200  sep 1000  centre 1700   PASS, both directions, first try
#   1100/2100  sep 1000  centre 1600   ic705->ht passed only after retries
#                                      (9.2s vs 2.6s), ht->ic705 0/6
#   1000/2000  sep 1000  centre 1500   both directions 0/6, twice
#   1100/1900  sep  800  centre 1500   both directions 0/6
#   1200/1800  sep  600  centre 1500   both directions 0/6
#
# Narrowing the separation does not rescue it, so this is the placement,
# not the spread: performance falls off monotonically as the pair moves
# down from 1700, and every centred variant is unusable. The ht->ic705 leg
# goes first, which is the same weaker leg measure_snr.py reads at 6.2 dB
# for 1000/2000 against 16.5 dB the other way.
#
# Note what does *not* explain it: none of this reproduces in software,
# where 1000/2000 modulates and demodulates at 0.99 confidence like any
# other pair. It is a property of the radio chain. One suggestive detail
# for whoever picks this up -- _tone_energy_diff integrates each tone over
# exactly one symbol, so at 1200 baud a 1000 Hz tone gives the detector
# only 0.83 cycles to work with, 1100 Hz gives 0.92, and 1200 Hz gives a
# full one. The three placements above line up with that ordering. If that
# is the mechanism, the fix is in the detector (a longer integration, or a
# coherent one), not in the tone table, and this profile could join the
# others at 1500 Hz afterwards.
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
    """One keying's worth of audio: `payload` framed (sync word, length,
    CRC, head/tail pads) and modulated as one continuous signal.

    A multi-frame variant of this existed briefly, for putting a burst of
    link-layer frames in a single keying. That was rolled back with the
    rest of the burst work; if it returns, note what the first hardware run
    established: the frames have to share one _cpfsk_tone call. Modulating
    each separately and concatenating the audio puts a 10ms fade to silence
    at every join, because _apply_ramp ramps each frame down and the next
    back up -- inaudible on a clean channel, but with the receiver's
    AGC/limiter pumping through the gap the frame after each join decoded
    only about a third of the time.
    """
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


# How many correlation peaks demodulate() will attempt to decode in one
# call, and how close together two peaks may be before they're treated as
# one. A frame's sync word is 63 symbols, so peaks closer than a few
# symbols are the same lock seen twice; the cap keeps a noisy buffer full
# of marginal peaks from turning one poll into hundreds of CRC attempts.
_MIN_PEAK_SEPARATION_SYMBOLS = 8
_MAX_SYNC_CANDIDATES = 16


def _normalised_correlation(diff, template):
    """Sliding normalised cross-correlation of `template` over `diff`: one
    value per offset, in [-1, 1], where 1 means that window has exactly the
    template's shape.

    This replaces a plain correlation scored against
    `peak / median(abs(corr))`. That ratio measured the wrong thing. Its
    denominator is the *typical* correlation across the buffer, which is
    small when the buffer is mostly idle noise and large when the buffer is
    mostly modulated audio -- so the same frame scored 255 sitting in six
    seconds of silence and 12 when the buffer held little else, and a
    buffer of pure noise could score 6 against a threshold of 4. Measured
    over the real off-air captures in scratch_captures_600ack, that measure
    reported a sync lock on all 30 recordings that contain no sync word at
    all.

    Whether a frame is present cannot depend on how much dead air happens
    to surround it, and it does not here: dividing each window's
    correlation by that window's own energy makes the score a property of
    the window alone.
    """
    corr = correlate(diff, template, mode="valid", method="fft")
    m = len(template)
    cumulative = np.concatenate([[0.0], np.cumsum(diff.astype(np.float64) ** 2)])
    window_energy = np.maximum(cumulative[m:] - cumulative[:-m], 0.0)
    local = np.sqrt(window_energy)[:len(corr)]
    # A window of digital silence has no energy at all, and dividing a
    # ~zero correlation by ~zero yields NaN or a meaningless large ratio.
    # Floor it relative to the buffer's own typical window energy, with an
    # absolute epsilon for a buffer that is silent throughout.
    rms_window = float(np.sqrt(np.mean(diff.astype(np.float64) ** 2))) * np.sqrt(m)
    floor = max(_ENERGY_FLOOR_FRACTION * rms_window, 1e-12)
    ncc = corr / (np.maximum(local, floor) * float(np.sqrt(np.sum(template ** 2))))
    return np.nan_to_num(ncc, nan=0.0, posinf=0.0, neginf=0.0)


def _sync_peaks(score, threshold_value, min_separation):
    """Indices of distinct peaks in `score` (a normalised correlation)
    above `threshold_value`, in time order.

    demodulate() used to take argmax and stop, which is right only if the
    buffer holds at most one sync-like thing. It does not: the RX buffer
    routinely holds a garbled self-echo of our own last transmission
    alongside the peer's genuine reply, and the echo can easily be the
    louder of the two. Consuming up to the strongest peak's end then throws
    away everything before it, the real frame included. The earliest peak
    that decodes -- not the loudest -- is the one to return.
    """
    above = np.flatnonzero(score > threshold_value)
    if above.size == 0:
        return []
    splits = np.flatnonzero(np.diff(above) > min_separation)
    peaks = [int(group[np.argmax(score[group])]) for group in np.split(above, splits + 1)]
    if len(peaks) > _MAX_SYNC_CANDIDATES:
        # Keep the strongest, but hand them back in time order regardless:
        # the point of the search is "earliest that decodes", and strength
        # is only used to decide which marginal peaks are worth the CRC.
        peaks = sorted(sorted(peaks, key=lambda i: score[i], reverse=True)[:_MAX_SYNC_CANDIDATES])
    return peaks


def _try_sync(diff, i_star, sps, confidence):
    """Attempts to read a frame at one correlation peak. Same return shape
    as demodulate()."""
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
        # Where the sync word itself ends. A caller giving up on this
        # position only has to step past the sync to guarantee the same
        # peak cannot win again, and stepping past just that discards far
        # less unexamined audio than skipping to the end of a frame whose
        # declared length it has no reason to trust. See whale/link.py's
        # _decode_one.
        "sync_end_index": first_index + sps * n_sync,
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


def demodulate(audio, profile: Profile = PROFILE_300, sample_rate=SAMPLE_RATE):
    """Finds and decodes the *earliest* frame in `audio`. Returns a dict
    with at least 'synced' and 'payload' (None if nothing usable was found).

    `audio` can hold more than one sync-like thing -- a garbled self-echo of
    our own last transmission and the peer's genuine reply routinely sit in
    the buffer together. Every correlation peak clearing the profile's
    confidence threshold is tried in time order and the first one whose CRC
    checks out wins, so the caller can consume up to its 'end_index' and
    come straight back for whatever follows.

    When nothing decodes, the reported near-miss ('end_index' without a
    'payload') is likewise the earliest such peak, so a caller skipping past
    it discards as little unexamined audio as possible.
    """
    sps = round(sample_rate / profile.baud)
    audio = np.asarray(audio, dtype=np.float64)

    diff = _tone_energy_diff(audio, sample_rate, sps, profile.freq0, profile.freq1)
    template = _sync_template(sps, sample_rate, profile.freq0, profile.freq1)
    if len(diff) < len(template):
        return {"synced": False, "payload": None}

    ncc = _normalised_correlation(diff, template)
    peaks = _sync_peaks(ncc, profile.confidence_threshold,
                        sps * _MIN_PEAK_SEPARATION_SYMBOLS)
    if not peaks:
        return {"synced": False, "payload": None, "confidence": float(np.max(ncc))}

    # First peak that yields a valid frame wins. Otherwise prefer reporting
    # "still arriving" (no end_index, so the caller waits for more audio)
    # over the earliest dead end, since discarding a frame mid-flight costs
    # a retransmit while waiting one more poll costs nothing.
    near_miss = None
    still_arriving = None
    for i_star in peaks:
        result = _try_sync(diff, i_star, sps, float(ncc[i_star]))
        if result.get("payload") is not None:
            return result
        if "end_index" in result:
            if near_miss is None:
                near_miss = result
        elif still_arriving is None:
            still_arriving = result
    return still_arriving or near_miss
