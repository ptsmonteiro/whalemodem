"""M-ary continuous-phase FSK: a standalone candidate frame mode.

Deliberately NOT wired into whale/. Nothing here is imported by afsk.py,
link.py or transport.py, and this module imports whale.framing only for
crc16_ccitt_false and the bit<->byte helpers -- pure functions with no state
and no hardware. Integration (a Profile codec_id, mode negotiation, chunk
sizing) is a separate step and is not attempted here.


Why MFSK at all, on a channel this narrow
-----------------------------------------

The instinctive read is that MFSK cannot win here, and for a purely
bandwidth-limited channel that read is correct. Orthogonal MFSK needs a tone
spacing of one symbol rate, so M tones span (M-1)*Rs and the bandwidth
efficiency is log2(M)/(M-1) bits/s/Hz: 1.00 at M=2, 0.67 at M=4, 0.43 at M=8.
More tones is strictly *worse* per Hz. Spend the measured 1700 Hz (600-2300)
that way and you get 1700 bps at M=2, 1133 at M=4, 729 at M=8 -- i.e. the
existing 1200-baud binary profile already beats orthogonal 4-FSK.

What makes it worth measuring anyway is that this bench is not
bandwidth-limited. It is symbol-rate-limited, and the repo has the evidence:
scripts/sweep_baud_600_2300.py cleared 1200 baud at 1200/2200 Hz and failed
1400 baud at *the same two tones*. Bandwidth did not change between those two
points -- only the symbol rate did. Whatever kills 1400 baud (group delay
across the FM audio chain, ISI, the one-symbol integration in the detector)
is a function of symbol duration, not of occupied spectrum.

That is exactly the trade MFSK makes. It buys bits per symbol with bandwidth,
which this channel has spare, and spends nothing on symbol rate, which it does
not. 4-FSK at 700 baud carries 1400 bps -- more than PROFILE_1200's 1200 --
while running at 58% of the symbol rate that is known to work and 50% of the
one known to fail.

So the sweep this module supports is not "is MFSK better", it is "how far up
does the symbol-rate headroom actually go once each symbol carries two bits".


The constraints candidate profiles are generated under
------------------------------------------------------

All four come from measurements already in the repo; see candidates().

  BAND_LOW_HZ / BAND_HIGH_HZ    600-2300 Hz, scripts/measure_band_edges.py,
                                worst direction of the two.
  MAX_SYMBOL_RATE               1200 baud clears, 1400 does not
                                (scripts/sweep_baud_600_2300.py). Candidates
                                are generated up to the last known-good rate,
                                not past it.
  MIN_CYCLES_PER_SYMBOL         the lowest tone must complete at least one
                                cycle inside one symbol. This is the
                                PROFILE_1200 lesson stated as a rule: every
                                attempt to move that profile down to 1500 Hz
                                failed, monotonically worse as the tones went
                                lower, and _tone_energy_diff integrates each
                                tone over exactly one symbol -- 1000 Hz at
                                1200 baud gives the detector 0.83 of a cycle.
                                Below one cycle the mixer's negative-frequency
                                image is not averaged out by the integrator,
                                so the tone's own energy estimate carries a
                                2f ripple.
  spacing_ratio                 spacing / symbol rate. 1.0 is the
                                orthogonality condition for a non-coherent
                                detector, and PROFILE_300/PROFILE_600 both sit
                                on an integer multiple. PROFILE_1200 runs 0.83
                                and works, so sub-orthogonal candidates are
                                generated too -- but they are a bet on this
                                specific hardware, not a general one, and they
                                are more fragile at M>2 than at M=2 because
                                the decision is an argmax over M filters
                                rather than a difference between two.


What is different from whale/afsk.py, beyond M
----------------------------------------------

Three things, all forced by M>2 rather than chosen:

  - The detector is a bank of M matched filters and the symbol decision is an
    argmax over them, not the sign of a difference. That makes per-tone gain
    matter in a way it does not for binary FSK: a difference statistic is
    immune to a common gain and only cares about the *ratio* of the two, while
    an argmax over four tones spanning 600-2300 Hz is biased by whatever tilt
    the audio chain puts across that span.

  - So the preamble is used as training. It is a PN sequence over all M tones,
    so after sync the receiver knows which tone was sent in every preamble
    slot, measures the energy that actually came back for each, and divides it
    out before deciding any data symbol. This is a one-tap-per-tone equaliser
    for free -- there is no extra airtime, the preamble had to be there for
    sync regardless. TRAIN_ON_PREAMBLE turns it off so the sweep can measure
    what it is worth rather than assuming.

  - Sync correlation is multi-channel. whale/afsk.py correlates one scalar
    envelope (e1-e0) against one template; here there are M envelopes and M
    template channels, and the normalised correlation sums the numerator
    across channels and takes the denominator from the total energy across
    all M. Same normalisation as afsk._normalised_correlation and for the same
    reason -- the score has to be a property of the window, not of how much
    dead air surrounds it.

Bit-to-tone mapping is Gray coded, so the adjacent-tone confusions the
detector actually makes cost one bit rather than two. With CRC-only framing
and no FEC this changes nothing about whether a frame passes -- any single bit
error fails it. It is there so that adding FEC later is a drop-in rather than
a format change.
"""

from dataclasses import dataclass
from functools import cached_property

import numpy as np
from scipy.signal import correlate

from whale import framing

SAMPLE_RATE = 48000

# -- what the bench measured, and what candidates are generated inside -----

# scripts/measure_band_edges.py, worst of the two directions.
BAND_LOW_HZ = 600.0
BAND_HIGH_HZ = 2300.0

# scripts/sweep_baud_600_2300.py: 1200 baud 100% both directions at
# 1200/2200 Hz, 1400 baud 0/5 ht->ic705 with confidence flatlined. Candidates
# stop at the last rate known to work rather than probing past it -- MFSK's
# whole argument is that it does not need to go there.
MAX_SYMBOL_RATE = 1200.0

# The lowest tone must complete this many cycles per symbol. See the module
# docstring; 1.0 is where PROFILE_1200's working placement sits and every
# failed one is below.
MIN_CYCLES_PER_SYMBOL = 1.0

# -- the keying budget, copied rather than imported ------------------------
#
# Same numbers as afsk.MAX_KEYING_SECONDS / afsk.KEYING_OVERHEAD_SECONDS,
# restated here so this module stands alone. The overhead is
# transport.PTT_LEAD + transport.STREAM_FILL + transport.PTT_TAIL and is
# measured (0.42-0.43s over 88 keyings); the cap is chosen. If either moves in
# whale/, this copy is stale -- experiments/mfsk/test_mfsk.py asserts the two
# still agree, which is the only place the coupling shows.
MAX_KEYING_SECONDS = 3.0
KEYING_OVERHEAD_SECONDS = 0.43

# Known symbols in front of every frame, doubling as the sync correlation
# target and as the per-tone training sequence. 63 matches the sync word
# whale/framing.py has been proven with; at these symbol rates it costs
# 50-90ms.
PREAMBLE_SYMBOLS = 63

# On-air padding either side of the frame, as durations rather than symbol
# counts for the same reason whale/framing.py states: what the radio corrupts
# is a span of time, so a fixed symbol count tuned at one rate silently
# shrinks at the next. Values carried over from framing.HEAD_PAD_SECONDS /
# TAIL_PAD_SECONDS, which were swept on this hardware.
HEAD_PAD_SECONDS = 0.08
TAIL_PAD_SECONDS = 0.03

# As afsk.CONFIDENCE_THRESHOLD. Kept at the same value; the normalised
# multi-channel correlation below scores on the same [0, 1] scale, and the
# software tests check the genuine/absent gap is still wide here.
CONFIDENCE_THRESHOLD = 0.7

# As afsk._ENERGY_FLOOR_FRACTION.
_ENERGY_FLOOR_FRACTION = 0.05

# As afsk.MAX_CREDIBLE_FRAME_SECONDS: a declared length is unverifiable until
# the CRC after it arrives, so an implausible one has to be rejected on its
# face rather than waited out.
MAX_CREDIBLE_FRAME_SECONDS = 8.0

# Divide each tone's received energy by what the preamble says that tone
# actually came back at, before deciding data symbols. See the module
# docstring. Off makes the detector fall back to whole-buffer per-tone RMS,
# which is what whale/afsk.py does.
TRAIN_ON_PREAMBLE = True


# -- Gray mapping ----------------------------------------------------------
#
# Tone t carries the value t ^ (t >> 1), so tones adjacent in frequency carry
# values one bit apart. Stated in this direction (value as a function of tone)
# rather than the usual one because it is the frequency axis whose neighbours
# get confused, not the value axis.


def _gray(t):
    return t ^ (t >> 1)


def _tone_for_value(m):
    """value -> tone index, the inverse of _gray over range(m)."""
    inv = np.empty(m, dtype=np.int64)
    for t in range(m):
        inv[_gray(t)] = t
    return inv


def _value_for_tone(m):
    return np.array([_gray(t) for t in range(m)], dtype=np.int64)


# -- profile ---------------------------------------------------------------


@dataclass(frozen=True)
class MfskProfile:
    """One MFSK setting: M tones, evenly spaced, at one symbol rate.

    Everything derived -- tone frequencies, payload capacity, bit rate --
    comes off this rather than off module constants, so a sweep can hold one
    of them fixed and walk the others.
    """

    name: str
    m: int
    symbol_rate: float
    freq_base: float
    spacing: float
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    train_on_preamble: bool = TRAIN_ON_PREAMBLE

    def __post_init__(self):
        if self.m < 2 or (self.m & (self.m - 1)):
            raise ValueError(f"m must be a power of two, got {self.m}")

    @cached_property
    def bits_per_symbol(self) -> int:
        return int(self.m).bit_length() - 1

    @cached_property
    def tones(self) -> np.ndarray:
        return self.freq_base + self.spacing * np.arange(self.m)

    @property
    def top_freq(self) -> float:
        return float(self.tones[-1])

    @property
    def spacing_ratio(self) -> float:
        """Tone spacing in symbol rates. 1.0 (or any integer) is orthogonal
        for a non-coherent detector; below that the filters leak into each
        other and the argmax gets a biased input."""
        return self.spacing / self.symbol_rate

    @property
    def cycles_per_symbol(self) -> float:
        """Cycles of the *lowest* tone inside one symbol -- the quantity
        MIN_CYCLES_PER_SYMBOL bounds. The lowest tone is the binding one; the
        others only have more."""
        return self.freq_base / self.symbol_rate

    @property
    def raw_bitrate(self) -> float:
        """Channel bits per second while transmitting, framing included."""
        return self.symbol_rate * self.bits_per_symbol

    @property
    def sps(self) -> int:
        """Samples per symbol at the experiment's default 48 kHz rate."""
        return round(SAMPLE_RATE / self.symbol_rate)

    def samples_per_symbol(self, sample_rate=SAMPLE_RATE) -> int:
        """Samples per symbol at ``sample_rate``.

        Keeping this calculation profile-local lets fixed profiles be
        exercised at both common radio rates (notably 9.6 and 48 kHz) without
        silently using the 48 kHz integration window in the receiver.  As in
        the original candidate modem, non-integral ratios use the nearest
        integer because the generated sweep includes rates such as 550 baud.
        """
        exact = float(sample_rate) / self.symbol_rate
        rounded = round(exact)
        if rounded < 1:
            raise ValueError("sample rate must be at least the symbol rate")
        return rounded

    @cached_property
    def max_payload(self) -> int:
        """Largest payload whose whole keying fits MAX_KEYING_SECONDS."""
        return max_payload_for_keying(self)

    @property
    def payload_bitrate(self) -> float:
        """The number this whole exercise is trying to maximise: payload bits
        delivered per second of channel occupancy, dead air included. Compare
        against the 209/455/947 the three shipped profiles reach."""
        return self.max_payload * 8 / MAX_KEYING_SECONDS


def _preamble_bits(n):
    """PN bits from an order-9 LFSR (x^9 + x^5 + 1, period 511), long enough
    that no M reuses the sequence inside one preamble. Chunked into symbols by
    preamble_symbols(); the resulting M-ary sequence is near-random rather
    than a designed M-ary PN word, which is all a matched filter needs."""
    state, bits = 1, []
    for _ in range(n):
        bits.append(state & 1)
        fb = ((state >> 0) & 1) ^ ((state >> 4) & 1)
        state = ((state >> 1) | (fb << 8)) & 0x1FF
    return bits


def preamble_symbols(profile: MfskProfile) -> np.ndarray:
    """The known symbol sequence in front of every frame. Tone indices, not
    values -- this never goes through the Gray map, it is transmitted as
    literal tones."""
    bpsym = profile.bits_per_symbol
    bits = _preamble_bits(PREAMBLE_SYMBOLS * bpsym)
    syms = [int("".join(str(b) for b in bits[i:i + bpsym]), 2)
            for i in range(0, len(bits), bpsym)]
    return np.array(syms[:PREAMBLE_SYMBOLS], dtype=np.int64)


def _pad_symbols(profile: MfskProfile, seconds):
    """Padding tones, cycling through the whole bank so the receiver's AGC
    settles on the same spectrum the frame will use rather than on one tone.
    Content is never parsed."""
    n = round(seconds * profile.symbol_rate)
    return np.arange(n, dtype=np.int64) % profile.m


# -- frame layout ----------------------------------------------------------
#
#   head pad | preamble | [ length(16) | payload | crc16 ] | tail pad
#
# The bracketed part is byte-identical to whale/framing.py's frame body, CRC
# included, so an integrated version could share a parser. It is packed
# MSB-first into bits and then into log2(M)-bit symbols, zero-padded up to a
# symbol boundary; parse_frame_bits reads the exact prefix it needs and
# ignores the rest, so the pad costs at most one symbol and needs no marker.


def _body_bits(payload: bytes):
    body = len(payload).to_bytes(framing.LENGTH_FIELD_BITS // 8, "big") + payload
    crc = framing.crc16_ccitt_false(body)
    return framing.bytes_to_bits(body + bytes([(crc >> 8) & 0xFF, crc & 0xFF]))


def body_symbols_for_length(profile: MfskProfile, payload_len):
    """Data symbols (preamble and pads excluded) for a frame of this length."""
    bits = framing.frame_bits_for_length(payload_len)
    return -(-bits // profile.bits_per_symbol)


def frame_symbols(profile: MfskProfile, payload_len):
    """Total symbols on air for one frame, pads included."""
    return (len(_pad_symbols(profile, HEAD_PAD_SECONDS))
            + PREAMBLE_SYMBOLS
            + body_symbols_for_length(profile, payload_len)
            + len(_pad_symbols(profile, TAIL_PAD_SECONDS)))


def frame_seconds(profile: MfskProfile, payload_len):
    """How long the frame's own audio lasts."""
    return frame_symbols(profile, payload_len) / profile.symbol_rate


def keying_seconds(profile: MfskProfile, payload_len):
    """How long the channel is occupied, PTT key-down to PTT release. This,
    not frame_seconds, is what MAX_KEYING_SECONDS caps."""
    return KEYING_OVERHEAD_SECONDS + frame_seconds(profile, payload_len)


def max_payload_for_keying(profile: MfskProfile, budget=MAX_KEYING_SECONDS) -> int:
    """Largest payload, in bytes, whose whole keying fits `budget`."""
    spare = (budget - KEYING_OVERHEAD_SECONDS) * profile.symbol_rate
    spare -= frame_symbols(profile, 0)
    if spare < 0:
        return 0
    spare_bits = int(spare) * profile.bits_per_symbol
    return max(0, min(spare_bits // 8, framing.MAX_PAYLOAD_BYTES))


def max_credible_symbols(profile: MfskProfile):
    return int(MAX_CREDIBLE_FRAME_SECONDS * profile.symbol_rate)


# -- modulate --------------------------------------------------------------


def _cpfsk(symbols, profile: MfskProfile, sample_rate):
    """Continuous-phase M-FSK. Phase is integrated across symbol boundaries,
    so there is no discontinuity to splatter energy outside the band -- the
    same construction as afsk._cpfsk_tone, just indexing a tone table instead
    of choosing between two."""
    freqs = profile.tones[np.asarray(symbols, dtype=np.int64)]
    inst = np.repeat(freqs, profile.samples_per_symbol(sample_rate))
    return np.cos(2 * np.pi * np.cumsum(inst) / sample_rate)


def _apply_ramp(signal, sample_rate, ramp_ms=5):
    ramp_len = min(int(sample_rate * ramp_ms / 1000), len(signal) // 2)
    if ramp_len <= 0:
        return signal
    window = np.hanning(2 * ramp_len)
    signal = signal.copy()
    signal[:ramp_len] *= window[:ramp_len]
    signal[-ramp_len:] *= window[ramp_len:]
    return signal


def frame_symbol_sequence(payload: bytes, profile: MfskProfile) -> np.ndarray:
    bits = _body_bits(payload)
    bpsym = profile.bits_per_symbol
    if len(bits) % bpsym:
        bits = bits + [0] * (bpsym - len(bits) % bpsym)
    values = np.array(
        [int("".join(str(b) for b in bits[i:i + bpsym]), 2) for i in range(0, len(bits), bpsym)],
        dtype=np.int64)
    data = _tone_for_value(profile.m)[values]
    return np.concatenate([
        _pad_symbols(profile, HEAD_PAD_SECONDS),
        preamble_symbols(profile),
        data,
        _pad_symbols(profile, TAIL_PAD_SECONDS),
    ])


def modulate(payload: bytes, profile: MfskProfile, sample_rate=SAMPLE_RATE, amplitude=0.6):
    """One keying's worth of audio: `payload` framed and modulated as one
    continuous waveform. As with afsk.modulate, the whole keying is a single
    _cpfsk call -- concatenating separately-ramped pieces puts a fade to
    silence at every join, which cost the burst work about two thirds of its
    post-join frames."""
    audio = amplitude * _cpfsk(frame_symbol_sequence(payload, profile), profile, sample_rate)
    return _apply_ramp(audio, sample_rate).astype(np.float32)


# -- demodulate ------------------------------------------------------------


def _boxcar(x, n):
    """Running sum of the last `n` samples, index i covering [i-n+1, i].
    A cumsum difference rather than a convolution: exact, and O(len(x))
    instead of O(len(x) * n), which matters because there are M of these per
    call rather than 2."""
    c = np.concatenate([[0], np.cumsum(x)])
    out = c[1:] - c[np.maximum(np.arange(len(x)) + 1 - n, 0)]
    return out


def tone_envelopes(audio, profile: MfskProfile, sample_rate=SAMPLE_RATE):
    """(M, len(audio)) of per-tone energy, each row normalised by its own RMS
    over the whole buffer.

    The normalisation is what afsk._tone_energy_diff does to its two rows and
    is carried over unchanged -- it is a coarse per-tone gain equaliser, and
    with M tones spread over 1700 Hz it is doing more work here than it does
    there. Where it is coarse is that a buffer which is mostly idle equalises
    the *noise* per tone rather than the signal; the preamble training in
    _decode_at is the sharper version of the same idea, applied after sync
    when the receiver knows which tones were actually sent.
    """
    audio = np.asarray(audio, dtype=np.float64)
    n = np.arange(len(audio))
    sps = profile.samples_per_symbol(sample_rate)
    rows = []
    for f in profile.tones:
        mixed = audio * np.exp(-1j * 2 * np.pi * f * n / sample_rate)
        env = np.abs(_boxcar(mixed, sps)) / sps
        rms = np.sqrt(np.mean(env ** 2)) or 1.0
        rows.append(env / rms)
    return np.vstack(rows)


def _sync_template(profile: MfskProfile, sample_rate=SAMPLE_RATE):
    """The preamble's own envelope bank, with the leading partial-window ramp
    trimmed off so every column is a full symbol-length window. Returns
    (template, lead) where a correlation peak at index t puts the preamble's
    first sample at t - lead."""
    audio = _cpfsk(preamble_symbols(profile), profile, sample_rate)
    env = tone_envelopes(audio, profile, sample_rate)
    lead = profile.samples_per_symbol(sample_rate) - 1
    return env[:, lead:], lead


def _centre(env):
    """Subtract the across-tone mean at each instant, so the M channels sum to
    zero at every sample.

    This is not cosmetic, and leaving it out is a bug that was measured rather
    than reasoned about. Tone envelopes are magnitudes, so every channel is
    non-negative and carries a large DC component; correlating raw envelopes
    against a raw template therefore scores the DC against the DC, and the
    score never drops to where it should. Pure Gaussian noise scored 0.73 at
    M=2 against a 0.70 lock threshold -- i.e. the detector would have false
    synced on silence, the exact failure afsk._normalised_correlation was
    rewritten to eliminate.

    Centring is also what makes this the honest generalisation of the binary
    case. whale/afsk.py correlates the signed difference e1 - e0, which has no
    DC by construction; at M=2 the centred channels here are (e1-e0)/2 and
    -(e1-e0)/2, so the normalised score reduces to exactly afsk's, and
    CONFIDENCE_THRESHOLD keeps the meaning it was calibrated with.
    """
    return env - env.mean(axis=0, keepdims=True)


def _normalised_correlation(env, template):
    """Multi-channel normalised cross-correlation in [-1, 1]: the numerator
    sums each tone's correlation, the denominator is the total energy across
    all M tones in that window.

    Normalising by the window's own energy, rather than by a buffer-wide
    noise floor, is the fix afsk._normalised_correlation documents -- the
    ratio measure it replaced reported a sync lock on all 30 off-air captures
    that contain no sync word at all. The generalisation to M channels is the
    obvious one: treat the M envelopes as one vector-valued signal, once the
    common mode is out of the way (see _centre).
    """
    env = _centre(env)
    template = _centre(template)
    m, width = template.shape
    corr = sum(correlate(env[i], template[i], mode="valid", method="fft") for i in range(m))
    total = np.sum(env.astype(np.float64) ** 2, axis=0)
    cumulative = np.concatenate([[0.0], np.cumsum(total)])
    window_energy = np.maximum(cumulative[width:] - cumulative[:-width], 0.0)
    local = np.sqrt(window_energy)[:len(corr)]
    rms_window = float(np.sqrt(np.mean(total))) * np.sqrt(width)
    floor = max(_ENERGY_FLOOR_FRACTION * rms_window, 1e-12)
    ncc = corr / (np.maximum(local, floor) * float(np.sqrt(np.sum(template ** 2))))
    return np.nan_to_num(ncc, nan=0.0, posinf=0.0, neginf=0.0)


_MIN_PEAK_SEPARATION_SYMBOLS = 8
_MAX_SYNC_CANDIDATES = 16


def _sync_peaks(score, threshold, min_separation):
    """Distinct peaks above `threshold`, in time order. Time order rather than
    strongest-first for the reason afsk._sync_peaks gives: the RX buffer
    routinely holds a garbled self-echo of our own last transmission
    alongside the peer's real reply, and the echo is often the louder."""
    above = np.flatnonzero(score > threshold)
    if above.size == 0:
        return []
    splits = np.flatnonzero(np.diff(above) > min_separation)
    peaks = [int(g[np.argmax(score[g])]) for g in np.split(above, splits + 1)]
    if len(peaks) > _MAX_SYNC_CANDIDATES:
        peaks = sorted(sorted(peaks, key=lambda i: score[i], reverse=True)[:_MAX_SYNC_CANDIDATES])
    return peaks


def _training_gains(env, preamble_start, profile: MfskProfile,
                    sample_rate=SAMPLE_RATE):
    """Per-tone gain measured off the preamble: for each tone, the mean
    envelope in the slots where that tone was actually transmitted.

    This is the one-tap equaliser the module docstring describes. Returns None
    if any tone is missing from the preamble or came back at essentially zero,
    in which case the caller keeps the whole-buffer RMS normalisation rather
    than dividing by a number it cannot trust.
    """
    syms = preamble_symbols(profile)
    sps = profile.samples_per_symbol(sample_rate)
    idx = preamble_start + sps * np.arange(len(syms)) + sps - 1
    if idx[-1] >= env.shape[1] or idx[0] < 0:
        return None
    gains = np.ones(profile.m)
    for tone in range(profile.m):
        slots = idx[syms == tone]
        if slots.size == 0:
            return None
        gains[tone] = float(np.mean(env[tone, slots]))
    if not np.all(np.isfinite(gains)) or gains.min() <= 1e-9:
        return None
    return gains / gains.mean()


def _decode_at(env, peak, lead, profile: MfskProfile, sample_rate=SAMPLE_RATE):
    """Attempts to read a frame at one correlation peak. Returns the same
    shape as demodulate()."""
    sps = profile.samples_per_symbol(sample_rate)
    preamble_start = peak - lead
    data_start = preamble_start + sps * PREAMBLE_SYMBOLS
    available = (env.shape[1] - data_start) // sps
    min_symbols = -(-framing.LENGTH_FIELD_BITS // profile.bits_per_symbol)
    result = {"synced": False, "payload": None,
              "sync_end_index": data_start, "start_index": preamble_start}
    if preamble_start < 0 or available < min_symbols:
        # Not even the length field has landed. Report nothing definitive:
        # this may be a real frame still streaming in, and a caller that
        # discards it pays a retransmit for what one more poll would have
        # settled.
        return result

    if profile.train_on_preamble:
        gains = _training_gains(env, preamble_start, profile, sample_rate)
        if gains is not None:
            env = env / gains[:, None]

    n = min(available, max_credible_symbols(profile))
    points = data_start + sps * np.arange(n) + (sps - 1)
    tones_rx = np.argmax(env[:, points], axis=0)
    values = _value_for_tone(profile.m)[tones_rx]

    bits = []
    for v in values:
        bits.extend((v >> b) & 1 for b in range(profile.bits_per_symbol - 1, -1, -1))

    payload = framing.parse_frame_bits(bits)
    length = framing.declared_length(bits)
    needed = body_symbols_for_length(profile, length) if length is not None else None
    result["payload"] = payload
    result["synced"] = payload is not None
    if needed is not None and needed > max_credible_symbols(profile):
        # A length no keying at this rate could be sending. Nothing will ever
        # arrive to settle it, so call the sync dead and end it at the
        # preamble rather than measuring a skip with a length just rejected.
        result["end_index"] = result["sync_end_index"]
    elif payload is not None or (needed is not None and available >= needed):
        result["end_index"] = data_start + sps * min(needed, n)
    return result


def demodulate(audio, profile: MfskProfile, sample_rate=SAMPLE_RATE):
    """Finds and decodes the earliest frame in `audio`. Returns a dict with at
    least 'synced' and 'payload' (None if nothing usable was found).

    Every correlation peak clearing the profile's threshold is tried in time
    order and the first whose CRC checks out wins; when nothing decodes, a
    frame that may still be arriving is preferred over the earliest dead end,
    so a caller waits one more poll rather than discarding a frame mid-flight.
    """
    env = tone_envelopes(audio, profile, sample_rate)
    template, lead = _sync_template(profile, sample_rate)
    if env.shape[1] < template.shape[1]:
        return {"synced": False, "payload": None, "confidence": 0.0}

    ncc = _normalised_correlation(env, template)
    peaks = _sync_peaks(ncc, profile.confidence_threshold,
                        profile.samples_per_symbol(sample_rate) * _MIN_PEAK_SEPARATION_SYMBOLS)
    if not peaks:
        return {"synced": False, "payload": None, "confidence": float(np.max(ncc))}

    near_miss = still_arriving = None
    for peak in peaks:
        result = _decode_at(env, peak, lead, profile, sample_rate)
        result["confidence"] = float(ncc[peak])
        if result["payload"] is not None:
            return result
        if "end_index" in result:
            near_miss = near_miss or result
        else:
            still_arriving = still_arriving or result
    return still_arriving or near_miss


# -- candidate generation --------------------------------------------------


def place(m, symbol_rate, spacing_ratio, name=None, **kwargs):
    """A profile with M tones at `spacing_ratio` symbol rates apart, centred
    in whatever room the band and the one-cycle rule leave.

    Centred rather than pushed low, on PROFILE_300/PROFILE_600's evidence:
    both were re-centred in the band from wherever their development history
    had left them, and both measured better afterwards, not merely no worse.
    Returns None if the tone set does not fit.
    """
    spacing = spacing_ratio * symbol_rate
    low_limit = max(BAND_LOW_HZ, MIN_CYCLES_PER_SYMBOL * symbol_rate)
    room = BAND_HIGH_HZ - low_limit
    span = (m - 1) * spacing
    if span > room:
        return None
    base = low_limit + (room - span) / 2
    if name is None:
        name = f"{m}fsk_{symbol_rate:.0f}bd_x{spacing_ratio:g}"
    return MfskProfile(name=name, m=m, symbol_rate=float(symbol_rate),
                       freq_base=float(base), spacing=float(spacing), **kwargs)


# Spacing ratios worth generating, and what each is a bet on.
#
# 1.0 is the only one with a guarantee behind it: the box integrator's
# frequency response is a sinc with its first null at exactly one symbol rate,
# so an orthogonal neighbour contributes exactly zero to your filter. Every
# ratio below 1.0 leaks |sinc(ratio)| of the neighbour's amplitude into each
# decision -- 0.19 at 0.833, 0.30 at 0.75, 0.37 at 0.70, 0.50 at 0.60 -- and
# with continuous phase that leakage arrives at an arbitrary phase, so it
# behaves like a second noise term rather than a fixed bias.
#
# Sub-orthogonal ratios are generated anyway, and they are the only reason
# this exercise can beat the shipped 1200-baud profile at all. At orthogonal
# spacing the band and the one-cycle rule give M*Rs <= 2300, so the raw rate
# is (2300/M)*log2(M): 1150 bps at M=2 and *exactly the same* 1150 at M=4,
# because log2(M)/M is equal at 2 and 4. Orthogonal 4-FSK therefore ties the
# existing binary profile rather than beating it -- it reaches the same bit
# rate at half the symbol rate, which is a robustness win and not a throughput
# one. Everything above parity is bought by packing tones closer than the
# detector can cleanly separate, and whether this hardware pays for that is a
# measurement, which is what sweep_mfsk.py is for.
#
# PROFILE_1200 is the standing evidence that 0.833 is survivable on this
# chain at M=2. Nothing in the repo says anything about M=4 at any ratio.
SPACING_RATIOS = (1.0, 0.9, 0.833, 0.75, 0.7, 0.6)


def candidates(ms=(2, 4, 8), spacing_ratios=SPACING_RATIOS,
               rates=range(100, int(MAX_SYMBOL_RATE) + 1, 25), **kwargs):
    """Every profile the constraints allow, best payload throughput first.

    The sweep walks this in order and stops at the first that decodes 100%
    both directions, which makes "max throughput subject to 100%" a property
    of the ordering rather than of anyone's judgement about which candidate
    ought to win.
    """
    out = []
    for m in ms:
        for ratio in spacing_ratios:
            for rate in rates:
                if rate > MAX_SYMBOL_RATE:
                    continue
                profile = place(m, rate, ratio, **kwargs)
                if profile is not None and profile.max_payload > 0:
                    out.append(profile)
    out.sort(key=lambda p: (-p.payload_bitrate, p.symbol_rate, -p.spacing_ratio))
    return out


def describe(profile: MfskProfile) -> str:
    tones = "/".join(f"{f:.0f}" for f in profile.tones)
    return (f"{profile.name:<22} M={profile.m} {profile.symbol_rate:>6.0f}bd "
            f"sep={profile.spacing:>6.1f}Hz (x{profile.spacing_ratio:.2f}) "
            f"cyc={profile.cycles_per_symbol:.2f} tones={tones:<28} "
            f"raw={profile.raw_bitrate:>5.0f}bps payload={profile.max_payload:>4d}B "
            f"={profile.payload_bitrate:>6.1f}bps "
            f"keying={keying_seconds(profile, profile.max_payload):.2f}s")
