"""OFDM: a standalone candidate frame mode for the IC-705 / HT bench.

Deliberately NOT wired into whale/, on the same terms as experiments/mfsk:
nothing in whale/ imports this, and this imports whale.framing only for
crc16_ccitt_false and the bit/byte helpers -- pure functions, no state, no
hardware. Integration (a codec_id on Profile, mode negotiation, chunk sizing
in link.py) is a separate step and is not attempted here.


Why OFDM, on a channel this narrow
----------------------------------

Every mode in this repo so far -- three binary FSK profiles and the 4-FSK
experiment -- has run into the same wall, and the repo has already localised
it precisely. scripts/sweep_baud_600_2300.py cleared 1200 baud at 1200/2200 Hz
and failed 1400 baud *at the same two tones*. Bandwidth did not change between
those points; only symbol duration did. experiments/mfsk then found its own
cliff, and its post-mortem put it on adjacent-tone leakage rather than noise.

Both are the signature of a *dispersive* channel: one whose impulse response
is long enough that a symbol smears into its neighbour (ISI) and a tone smears
into the filter next door (ICI). An FM audio chain has every ingredient --
mic-path and speaker-path filtering, de-emphasis, an IF filter -- and the
group delay of a filter is worst at its band edges, which is exactly where
every failed tone placement in this repo has been.

A cyclic prefix is the direct answer to that, and it is the reason to try
OFDM here rather than yet another tone arrangement. If the channel's impulse
response fits inside the prefix, then every subcarrier sees one complex gain
-- a number, not a smear -- and one division per subcarrier removes it
completely. Dispersion stops being an impairment and becomes a parameter to
estimate. Nothing in an FSK detector can do that: whale/afsk.py integrates
each tone over exactly one symbol and has nowhere to put the energy that
arrived late.

The second reason is the detector itself. Every mode here is non-coherent --
it measures tone *energy* and throws the phase away, because with FSK there is
no phase reference to keep. That costs about 4 dB against coherent detection
at the error rates a no-FEC link needs. OFDM has a phase reference by
construction (the training symbol), so coherent Gray-coded QPSK is available,
and that 4 dB is what pays for the extra bits per second:

    non-coherent binary FSK, BER 1e-5     ~13.4 dB Eb/N0
    coherent Gray QPSK,      BER 1e-5      ~9.6 dB Eb/N0

2.3x the bit rate costs 3.6 dB. The detector gives back 3.8. So QPSK OFDM at
roughly 2.3x PROFILE_1200's throughput should need *about the same* channel
SNR that profile already gets 100% on -- which is the whole bet, and the bench
is the only thing that can settle it.

Third, and the one that costs nothing: FM demodulation recovers baseband audio
directly, so a tuning error does not shift the audio spectrum the way it would
on SSB. OFDM's usual worst enemy -- carrier frequency offset, which destroys
subcarrier orthogonality -- does not exist on this link.

What remains is sample-clock offset, and it is the one place this mode is
*less* tolerant than the FSK profiles rather than more. The instinctive reading
-- 3.4 ppm between these two cards is 8.5 microseconds of drift across a 2.5s
frame, against a prefix measured in milliseconds, so it cannot matter -- is
wrong about the mechanism. The prefix absorbs the drift as *timing* easily.
What it does not absorb is the phase: a drift of tau samples rotates
subcarrier k by 2*pi*k*tau/n_fft, and the top subcarrier at 2300 Hz sweeps
through a whole cycle for every 1/2300th of a second of accumulated drift. The
frame dies when that rotation reaches the constellation's decision boundary.

That gives a closed form, and the measurement matches it to the ppm:

    max ppm ~ 1 / (2^(bits_per_carrier + 1) * top_frequency * frame_seconds)

               predicted   measured
    BPSK          43          40
    QPSK          21          20
    8PSK          11           8

-- and it is independent of subcarrier spacing, which the measurement confirms
(50, 30 and 75 Hz spacings all break at the same 20 ppm at QPSK). Both terms
in the denominator are fixed by things this experiment cannot change: the band
edge, and the 3s keying cap it is trying to fill.

So the margin is 6x at QPSK on this bench and only 2.4x at 8PSK, against the
235-745 ppm the FSK profiles enjoy. It costs nothing here and it is the first
thing to check on any other pair of sound cards. Profiles can now insert a
known full-band tracking symbol after every ``pilot_interval`` data symbols.
Those symbols re-anchor timing and EQ, and the receiver interpolates their
complex per-carrier estimates. On this bench they measured real channel
movement but did not rescue QPSK; the remaining errors were scattered noise,
not untracked drift. See test_tracking_pilots_refresh_a_moving_channel and
RESULTS.md.


What OFDM costs, and what is done about each
--------------------------------------------

Three things, and the first has no precedent anywhere in this repo.

  - PAPR. Every mode here so far is constant-envelope: FSK's amplitude never
    changes, so the audio can be driven at whatever level the radio takes and
    the deviation is the same from one instant to the next. A sum of 35
    independent subcarriers is Gaussian, with peaks ~11 dB above its own RMS.
    Drive it so the peaks fit and the *average* deviation -- which is what
    sets received SNR -- drops by that much. Drive it so the average is right
    and the HT's mic amp and deviation limiter clip the peaks, which is
    non-linear, and non-linearity in OFDM is in-band distortion on every
    subcarrier at once.

    So the peaks are clipped deliberately, in software, where it can be done
    to a chosen ratio and the out-of-band splatter filtered off afterwards
    (_clip_and_filter). papr_db is a profile parameter for exactly this
    reason: it is the one knob with no measurement behind it anywhere in the
    repo, and sweep_ofdm.py --drive A/Bs it on air.

    Sync and training symbols are built with Newman phases rather than random
    ones, which measures 5.4 dB of PAPR against the data symbols' 12.2 -- so
    the parts of the frame the receiver needs *intact* to decode anything are
    the parts the limiter touches least.

    The default of 9 dB is measured rather than picked, and the measurement
    is in _clip_and_filter.

  - One bad subcarrier fails the whole frame. There is no FEC here by
    instruction, and CRC framing is all-or-nothing, so a single subcarrier
    sitting in a notch produces errors in every symbol of the frame and no
    amount of margin elsewhere compensates. FSK does not have this failure
    mode in the same way -- it has two tones and they either work or they do
    not.

    This is why probe_channel.py exists and why it runs before the ladder:
    it measures |H| and per-subcarrier SNR straight off the training symbols,
    both directions, for about a minute of airtime. Choosing the band from
    that measurement is worth more than any number of blind ladder rungs, and
    band_low/band_high are per-profile so the answer can be applied.

  - Timing. The FFT window has to land inside the symbol it is reading. That
    sounds like a tighter constraint than FSK's and is in fact a much looser
    one: an error that lands the window early, anywhere inside the cyclic
    prefix, appears as a linear phase ramp across the subcarriers -- and since
    the training symbol picks up the same ramp, the equaliser divides it out
    exactly. The window is therefore placed deliberately early, by half a
    prefix (_WINDOW_GUARD_FRACTION), so the tolerance is two-sided. See
    test_a_timing_error_inside_the_prefix_costs_nothing.


Sync, and the trap in the obvious way of doing it
-------------------------------------------------

Two stages, because neither alone is safe here.

The repetition metric (Schmidl & Cox) is the standard OFDM answer: build the
preamble from even-numbered subcarriers only and its two halves come out
identical, so the receiver finds it by correlating the signal against a
delayed copy of *itself*. That is immune to whatever phase and gain the
channel applies, which is exactly what makes it standard.

It is also, on its own, unusable on this bench. A steady tone whose period
divides the half-symbol correlates perfectly with a delayed copy of itself --
metric 1.000, indistinguishable from a real preamble. This RX buffer is
routinely full of exactly that: the head pad of an AFSK frame, a garbled
self-echo of our own last transmission, another profile's carrier. This is
the third time in this repo a sync measure has been found to false-lock on
things that are not frames (see afsk._normalised_correlation on the ratio
measure, and mfsk._centre on the uncentred one), so it is checked here rather
than discovered later -- test_no_false_sync_on_off_air_captures runs against
the real captures in scratch_captures_600ack, and test_a_pure_tone_does_not_sync
specifically against the trap above.

So the repetition metric is used only to *propose* positions, cheaply and
dispersion-immune, and each proposal is then confirmed by a normalised
cross-correlation against the known preamble (_match_score). That is the
measure reported as `confidence`, it is normalised by the window's own energy
in the same way and for the same reason afsk._normalised_correlation is, and
it is what a tone fails: a tone put through a matched filter for an 18-tone
preamble scores about 1/sqrt(18).
"""

from dataclasses import dataclass
from functools import cached_property, lru_cache

import numpy as np
from scipy.signal import hilbert

from whale import framing

SAMPLE_RATE = 48000

# -- what the bench measured ----------------------------------------------

# scripts/measure_band_edges.py, worst of the two directions. Kept as the
# default for candidate generation, but per-profile rather than global: the
# figure was taken with a 2-byte FSK frame, which is the easiest thing this
# channel ever carries, and probe_channel.py measures the same band far more
# directly. If it disagrees, it wins -- that is what it is for.
BAND_LOW_HZ = 600.0
BAND_HIGH_HZ = 2300.0

# -- the keying budget, copied rather than imported ------------------------
#
# Same numbers as afsk.MAX_KEYING_SECONDS / afsk.KEYING_OVERHEAD_SECONDS,
# restated so this module stands alone (experiments/mfsk/mfsk.py does the
# same). The overhead is transport.PTT_LEAD + STREAM_FILL + PTT_TAIL and is
# measured over 88 keyings; the cap is chosen. test_ofdm.py asserts these
# still agree with whale/, which is the only place the coupling shows.
MAX_KEYING_SECONDS = 3.0
KEYING_OVERHEAD_SECONDS = 0.43

# On-air padding either side of the frame. Carried over from
# framing.HEAD_PAD_SECONDS / TAIL_PAD_SECONDS, which were swept on this
# hardware -- the head pad against an HT that blacks out for ~110ms after its
# squelch opens, and it is the expensive one here at 6% of the whole budget.
# Kept at the measured value anyway: this experiment is not the place to
# discover that a receiver allowance was margin all along.
HEAD_PAD_SECONDS = 0.15
TAIL_PAD_SECONDS = 0.03

# As afsk.MAX_CREDIBLE_FRAME_SECONDS: a declared length cannot be checked
# until the CRC after it arrives, so an implausible one has to be rejected on
# its face rather than waited out.
MAX_CREDIBLE_FRAME_SECONDS = 8.0

# The repetition metric only *proposes* positions -- see the module docstring
# on why it cannot be trusted to confirm one. Deliberately loose: a proposal
# that is wrong costs one cross-correlation, while a real preamble missed here
# is a lost frame.
REPETITION_THRESHOLD = 0.55

# The measure that decides, and the one reported as `confidence`: a normalised
# cross-correlation against the known preamble, in [0, 1], on the same scale
# as afsk.CONFIDENCE_THRESHOLD.
#
# Calibrated in software and against the real off-air captures in
# scratch_captures_600ack, the same corpus afsk.CONFIDENCE_THRESHOLD was
# calibrated on. See test_no_false_sync_on_off_air_captures, which is what
# keeps this honest, and RESULTS.md for the measured separation.
CONFIDENCE_THRESHOLD = 0.6

# How far into the cyclic prefix the FFT window is deliberately placed, as a
# fraction of it. An early window is free -- the phase ramp it produces is
# identical on the training symbol and divides out -- while a late one reads
# part of the next symbol and is not. Half a prefix makes the tolerance
# two-sided rather than one-sided.
_WINDOW_GUARD_FRACTION = 0.5

# Peaks closer together than this fraction of a symbol are one lock seen
# twice. As afsk._MIN_PEAK_SEPARATION_SYMBOLS / _MAX_SYNC_CANDIDATES.
_MIN_PEAK_SEPARATION_SYMBOLS = 0.5
_MAX_SYNC_CANDIDATES = 12


# -- constellations --------------------------------------------------------
#
# Gray coded, so that the nearest-neighbour confusions the detector actually
# makes cost one bit rather than several. Under CRC-only framing that changes
# nothing about whether a frame passes -- any single bit error fails it -- but
# it is what makes the symbol error rate the honest thing to measure in
# diagnose_ofdm.py, and it makes adding FEC later a drop-in rather than a
# format change. Same reasoning as mfsk's tone Gray map.


def _gray(i):
    return i ^ (i >> 1)


@lru_cache(maxsize=None)
def constellation(bits_per_carrier: int) -> np.ndarray:
    """Unit-average-power constellation, indexed by the integer the bits
    spell out MSB first. 1/2/3 bits are PSK, 4 is 16-QAM.

    PSK up to 8 rather than QAM at every order, because PSK is
    constant-modulus: with no FEC and a limiter in the transmit path, an
    amplitude-coded constellation spends its margin on exactly the impairment
    this chain is worst at. 16-QAM is generated so the ladder can find out
    whether that reasoning survives contact, not because it is expected to
    win.
    """
    m = 1 << bits_per_carrier
    points = np.zeros(m, dtype=complex)
    if bits_per_carrier <= 3:
        for i in range(m):
            points[_gray(i)] = np.exp(2j * np.pi * i / m)
        return points
    if bits_per_carrier == 4:
        # Gray on each axis independently: 00 -> -3, 01 -> -1, 11 -> +1,
        # 10 -> +3, which is the Gray order along the level axis.
        levels = {0b00: -3.0, 0b01: -1.0, 0b11: 1.0, 0b10: 3.0}
        for value in range(m):
            i_bits, q_bits = (value >> 2) & 0b11, value & 0b11
            points[value] = levels[i_bits] + 1j * levels[q_bits]
        return points / np.sqrt(10.0)
    raise ValueError(f"unsupported bits_per_carrier {bits_per_carrier}")


# -- profile ---------------------------------------------------------------


@dataclass(frozen=True)
class OfdmProfile:
    """One OFDM setting. Everything derived -- subcarrier frequencies, payload
    capacity, symbol rate -- comes off this rather than off module constants,
    so a sweep can hold one parameter fixed and walk the others.

    n_fft is the useful symbol in *samples* at 48 kHz, so subcarrier spacing
    is 48000/n_fft and the useful symbol duration is its reciprocal. Stated in
    samples rather than Hz because it must be an integer for the FFT to be
    exact, and an integer number of samples is the thing that actually has to
    hold.
    """

    name: str
    n_fft: int
    cp: int
    bits_per_carrier: int
    band_low: float = BAND_LOW_HZ
    band_high: float = BAND_HIGH_HZ
    n_train: int = 2
    pilot_interval: int = 0
    papr_db: float = 9.0
    confidence_threshold: float = CONFIDENCE_THRESHOLD

    def __post_init__(self):
        if self.cp <= 0 or self.n_fft <= 0:
            raise ValueError("n_fft and cp must be positive")
        if self.n_fft % 2:
            raise ValueError("n_fft must be even -- the preamble's two halves are n_fft/2")
        if self.n_train < 1:
            raise ValueError("at least one training symbol is needed to estimate the channel")
        if self.pilot_interval < 0:
            raise ValueError("pilot_interval cannot be negative")

    # -- geometry ----------------------------------------------------------

    @property
    def spacing(self) -> float:
        """Subcarrier spacing in Hz."""
        return SAMPLE_RATE / self.n_fft

    @property
    def symbol_samples(self) -> int:
        return self.n_fft + self.cp

    @property
    def symbol_rate(self) -> float:
        """OFDM symbols per second, prefix included."""
        return SAMPLE_RATE / self.symbol_samples

    @property
    def cp_seconds(self) -> float:
        """The delay spread this profile can absorb. The quantity the whole
        argument for OFDM rests on: dispersion shorter than this is removed
        exactly, dispersion longer than this is ISI like anywhere else."""
        return self.cp / SAMPLE_RATE

    @cached_property
    def carriers(self) -> np.ndarray:
        """FFT bin indices carrying data, low to high."""
        lo = int(np.ceil(self.band_low / self.spacing))
        hi = int(np.floor(self.band_high / self.spacing))
        return np.arange(max(lo, 1), min(hi, self.n_fft // 2 - 1) + 1)

    @cached_property
    def sync_carriers(self) -> np.ndarray:
        """The even-indexed subset the preamble uses. Even indices are what
        make the preamble's two halves identical in the time domain, which is
        what the repetition metric looks for -- exp(j*2*pi*k*(n + N/2)/N)
        differs from exp(j*2*pi*k*n/N) by exp(j*pi*k), which is 1 exactly when
        k is even."""
        return self.carriers[self.carriers % 2 == 0]

    @property
    def n_carriers(self) -> int:
        return len(self.carriers)

    @property
    def tone_low(self) -> float:
        return float(self.carriers[0] * self.spacing)

    @property
    def tone_high(self) -> float:
        return float(self.carriers[-1] * self.spacing)

    # -- capacity ----------------------------------------------------------

    @property
    def bits_per_symbol(self) -> int:
        return self.n_carriers * self.bits_per_carrier

    @property
    def raw_bitrate(self) -> float:
        """Channel bits per second while data symbols are going out."""
        return self.bits_per_symbol * self.symbol_rate

    @cached_property
    def max_data_symbols(self) -> int:
        """Data symbols that fit the keying budget once the pads, the
        preamble and the training symbols are paid for."""
        budget = int((MAX_KEYING_SECONDS - KEYING_OVERHEAD_SECONDS) * SAMPLE_RATE)
        fixed = (_pad_samples(HEAD_PAD_SECONDS) + _pad_samples(TAIL_PAD_SECONDS)
                 + (1 + self.n_train) * self.symbol_samples)
        slots = max(0, (budget - fixed) // self.symbol_samples)
        # A tracking pilot follows every complete group of pilot_interval data
        # symbols. Solve the small integer budget directly; this keeps the
        # boundary honest when the last data symbol also incurs a pilot.
        data = slots
        while data + tracking_pilots_for_data_symbols(self, data) > slots:
            data -= 1
        return max(0, data)

    @cached_property
    def max_payload(self) -> int:
        """Largest payload whose whole keying fits MAX_KEYING_SECONDS."""
        bits = self.max_data_symbols * self.bits_per_symbol
        # The length field (2 bytes) and CRC (2 bytes) come out of the same
        # bits, so they are subtracted here rather than budgeted separately.
        return max(0, min(bits // 8 - 4, framing.MAX_PAYLOAD_BYTES))

    @property
    def payload_bitrate(self) -> float:
        """The number this exercise maximises: payload bits delivered per
        second of channel occupancy, dead air included. PROFILE_1200 reaches
        947 and experiments/mfsk's winner 1011."""
        return self.max_payload * 8 / MAX_KEYING_SECONDS

    @property
    def prefix_overhead(self) -> float:
        """Fraction of airtime spent on cyclic prefix. This is what the
        dispersion immunity costs, and it is the only OFDM overhead that
        scales with the protection bought."""
        return self.cp / self.symbol_samples


def _pad_samples(seconds):
    return int(round(seconds * SAMPLE_RATE))


def data_symbols_for_length(profile: OfdmProfile, payload_len) -> int:
    bits = framing.frame_bits_for_length(payload_len)
    return -(-bits // profile.bits_per_symbol)


def tracking_pilots_for_data_symbols(profile: OfdmProfile, n_data: int) -> int:
    """Full-band tracking symbols inserted after complete data groups."""
    if not profile.pilot_interval:
        return 0
    return max(0, n_data) // profile.pilot_interval


def physical_symbols_for_data(profile: OfdmProfile, n_data: int) -> int:
    return n_data + tracking_pilots_for_data_symbols(profile, n_data)


def data_symbols_from_physical(profile: OfdmProfile, n_physical: int) -> int:
    """Data symbols contained in a possibly truncated data/pilot stream."""
    if not profile.pilot_interval:
        return max(0, n_physical)
    data = 0
    used = 0
    while used < n_physical:
        take = min(profile.pilot_interval, n_physical - used)
        data += take
        used += take
        if take == profile.pilot_interval and used < n_physical:
            used += 1  # the tracking pilot after this complete group
    return data


def data_physical_indices(profile: OfdmProfile, n_data: int) -> list[int]:
    """Physical symbol indices of data, relative to first_symbol_start."""
    positions = []
    physical = profile.n_train
    for data_index in range(n_data):
        positions.append(physical)
        physical += 1
        if (profile.pilot_interval
                and (data_index + 1) % profile.pilot_interval == 0):
            physical += 1
    return positions


def frame_samples(profile: OfdmProfile, payload_len) -> int:
    n_data = data_symbols_for_length(profile, payload_len)
    return (_pad_samples(HEAD_PAD_SECONDS)
            + (1 + profile.n_train + physical_symbols_for_data(profile, n_data))
            * profile.symbol_samples
            + _pad_samples(TAIL_PAD_SECONDS))


def frame_seconds(profile: OfdmProfile, payload_len) -> float:
    return frame_samples(profile, payload_len) / SAMPLE_RATE


def keying_seconds(profile: OfdmProfile, payload_len) -> float:
    """PTT key-down to PTT release -- the quantity MAX_KEYING_SECONDS caps."""
    return KEYING_OVERHEAD_SECONDS + frame_seconds(profile, payload_len)


def max_credible_data_symbols(profile: OfdmProfile) -> int:
    return int(MAX_CREDIBLE_FRAME_SECONDS * profile.symbol_rate)


# -- known symbols ---------------------------------------------------------


def _newman_phases(n):
    """Newman's phase sequence, phi_k = pi*k^2/n.

    A multitone with these phases measures 5.4 dB of peak-to-average ratio
    against a random-phase multitone's 12.2, because the quadratic phase
    sweeps the tones' alignment across the symbol instead of letting them all
    line up at one instant. Used for the preamble and the training symbols
    specifically: those are the parts of the frame that must survive intact
    for anything else to be decodable, so they are the parts given the most
    headroom against the limiter.
    """
    k = np.arange(n)
    return np.pi * k * k / n


@lru_cache(maxsize=None)
def training_values(profile: OfdmProfile) -> np.ndarray:
    """The known unit-magnitude symbol on each data carrier. Deterministic --
    both ends build it from the profile, nothing is transmitted about it."""
    return np.exp(1j * _newman_phases(profile.n_carriers))


@lru_cache(maxsize=None)
def sync_values(profile: OfdmProfile) -> np.ndarray:
    """The known symbol on each *sync* carrier. A different phase sequence
    from the training symbol's, so a training symbol cannot score as a
    preamble in the matched filter -- the frame contains several of them and
    they are periodic in exactly the way the correlator looks for."""
    n = len(profile.sync_carriers)
    return np.exp(1j * (_newman_phases(n) + np.pi / 3))


def _symbol_audio(profile: OfdmProfile, carriers, values) -> np.ndarray:
    """One useful symbol (no prefix) as real audio, n_fft samples."""
    spectrum = np.zeros(profile.n_fft // 2 + 1, dtype=complex)
    spectrum[carriers] = values
    return np.fft.irfft(spectrum, n=profile.n_fft)


def _with_prefix(profile: OfdmProfile, useful: np.ndarray) -> np.ndarray:
    """Cyclic prefix: the symbol's own tail, copied in front of it. Cyclic
    rather than a blank guard interval because that is what makes the channel
    look like one multiply per subcarrier -- a linear convolution over a
    window that repeats is a circular one, and a circular convolution is a
    product in the frequency domain. A blank guard would stop the ISI and
    leave the ICI."""
    return np.concatenate([useful[-profile.cp:], useful])


@lru_cache(maxsize=None)
def preamble_audio(profile: OfdmProfile) -> np.ndarray:
    """The preamble's useful part: n_fft samples whose two halves are
    identical, by construction (see OfdmProfile.sync_carriers)."""
    return _symbol_audio(profile, profile.sync_carriers, sync_values(profile))


@lru_cache(maxsize=None)
def _preamble_template(profile: OfdmProfile) -> np.ndarray:
    """The matched filter's target: the *analytic* preamble.

    Analytic on both sides, and getting this wrong is worth recording because
    it fails quietly rather than loudly. Correlating an analytic received
    signal against the real template scores a perfect match at 1/sqrt(2)
    exactly -- the received signal's energy is split between its real part and
    its Hilbert transform, the real template sees only the first, and the
    normalisation divides by both. Every score comes out 0.707 times what it
    should be, which looks like a mediocre channel rather than a bug, and the
    lock threshold silently becomes 0.85 instead of 0.6.
    """
    return hilbert(preamble_audio(profile))


@lru_cache(maxsize=None)
def _pad_audio(profile: OfdmProfile, seconds, seed):
    """Filler either side of the frame: a multitone over the same carriers,
    tiled to length.

    Content is never parsed. What it has to do is occupy exactly the spectrum
    the frame will, so the receiver's AGC settles on the signal's own
    statistics rather than on something narrower -- and *not* look like a
    preamble, since it is adjacent to one. A third phase sequence handles
    that; test_the_pads_do_not_sync keeps it honest.
    """
    rng = np.random.default_rng(seed)
    values = np.exp(2j * np.pi * rng.random(profile.n_carriers))
    tile = _symbol_audio(profile, profile.carriers, values)
    n = _pad_samples(seconds)
    if n <= 0:
        return np.zeros(0)
    return np.tile(tile, -(-n // len(tile)))[:n]


# -- bits <-> symbols ------------------------------------------------------


# The whitening sequence, and the frame it was found by breaking.
#
# Without it, an all-zero payload puts constellation point 0 on every
# subcarrier of every symbol -- identical phases across the band, which is the
# definition of an impulse. That symbol measures 17.1 dB of PAPR against a
# random symbol's 12.2, and _clip_and_filter then flattens it: EVM went to
# -5.9 dB and the frame failed at 3 symbol errors in 35, on a channel with no
# noise, no dispersion and no radios in it.
#
# The important part is that this is not an empty-frame curiosity. Any run of
# repeated bytes does it, and real traffic is full of them -- zero padding,
# whitespace, a run of 0xFF. A sweep sending random payloads (which is what
# sweep_ofdm.py sends, deliberately, so a decoder cannot pass by
# reconstructing what it expects) would never have found this, and it would
# have shipped as "OFDM is unreliable on some files".
#
# So the bits are XORed with a fixed PN sequence before mapping and again
# after demapping. Both ends derive it from nothing but its length; it carries
# no information and costs no airtime. It is emphatically not FEC -- it adds
# no redundancy and corrects nothing. What it does is make the *statistics* of
# the transmitted symbols independent of the payload, so the PAPR the clipper
# sees is the same 12.2 dB whatever the frame happens to contain.
#
# Order 17 (period 131071) covers the largest frame in the candidate space
# (1903 bytes = 15224 bits) without repeating. Taps verified by construction
# rather than trusted from a table, the same way framing._SYNC_TAPS are --
# see test_the_whitening_sequence_is_a_full_period_m_sequence.
_WHITENING_ORDER = 17
_WHITENING_TAPS = (1, 15)


@lru_cache(maxsize=None)
def _whitening_bits(n) -> np.ndarray:
    state, mask = 1, (1 << _WHITENING_ORDER) - 1
    out = np.empty(n, dtype=np.int64)
    for i in range(n):
        out[i] = state & 1
        fb = 0
        for t in _WHITENING_TAPS:
            fb ^= (state >> (t - 1)) & 1
        state = ((state >> 1) | (fb << (_WHITENING_ORDER - 1))) & mask
    return out


def _whiten(bits) -> np.ndarray:
    bits = np.asarray(bits, dtype=np.int64)
    return bits ^ _whitening_bits(len(bits))



def _bits_to_values(bits, bits_per_carrier):
    """Pack a bit list into integers, MSB first, zero-padding the tail. The
    pad costs at most one subcarrier and needs no marker: parse_frame_bits
    reads the exact prefix it needs and ignores whatever follows."""
    b = np.asarray(bits, dtype=np.int64)
    if len(b) % bits_per_carrier:
        b = np.concatenate([b, np.zeros(bits_per_carrier - len(b) % bits_per_carrier,
                                        dtype=np.int64)])
    b = b.reshape(-1, bits_per_carrier)
    weights = 1 << np.arange(bits_per_carrier - 1, -1, -1)
    return b @ weights


def _values_to_bits(values, bits_per_carrier):
    shifts = np.arange(bits_per_carrier - 1, -1, -1)
    return ((values[:, None] >> shifts) & 1).ravel().tolist()


# -- modulate --------------------------------------------------------------


def _band_mask(profile: OfdmProfile, n, transition_hz=150.0):
    """A smooth passband over the profile's own carriers, for filtering the
    splatter off a clipped frame. Raised-cosine skirts rather than a brick
    wall: a brick wall in the frequency domain is a sinc in time, and its
    ringing would spill across the prefix boundaries the whole scheme rests
    on."""
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    lo, hi = profile.tone_low - profile.spacing, profile.tone_high + profile.spacing
    mask = np.ones_like(freqs)
    rising = (freqs > lo - transition_hz) & (freqs < lo)
    falling = (freqs > hi) & (freqs < hi + transition_hz)
    mask[freqs <= lo - transition_hz] = 0.0
    mask[freqs >= hi + transition_hz] = 0.0
    mask[rising] = 0.5 * (1 - np.cos(np.pi * (freqs[rising] - (lo - transition_hz)) / transition_hz))
    mask[falling] = 0.5 * (1 + np.cos(np.pi * (freqs[falling] - hi) / transition_hz))
    return mask


def measured_papr_db(audio) -> float:
    audio = np.asarray(audio, dtype=np.float64)
    rms = np.sqrt(np.mean(audio ** 2))
    if rms <= 0:
        return 0.0
    return float(20 * np.log10(np.max(np.abs(audio)) / rms))


# How far below the target each clipping pass actually clips, and how many
# passes it gets. Filtering the splatter off a clipped signal lets the peaks
# grow part of the way back, so clipping exactly at the target converges to
# it from above and slowly -- 12.2 dB raw reaches only 7.9 dB in three passes
# and still 6.3 dB in twenty. Clipping 1 dB low each pass converges in about
# four. The loop stops early when the frame is inside _PAPR_TOLERANCE_DB of
# the target, so a profile asking for a mild reduction does not pay for
# passes it does not need.
_PAPR_OVERSHOOT_DB = 1.0
_PAPR_TOLERANCE_DB = 0.25
_PAPR_MAX_PASSES = 10


def _clip_and_filter(profile: OfdmProfile, audio, iterations=_PAPR_MAX_PASSES):
    """Bring the frame's peak-to-average ratio down to profile.papr_db.

    Clip, then filter the resulting out-of-band splatter away, then repeat --
    filtering lets some of the peaks grow back, so one pass does not reach the
    target (see _PAPR_OVERSHOOT_DB for the measured convergence).

    Why clip at all, rather than simply turning the drive down: FM deviation
    follows the instantaneous audio level, so the radio is peak-limited, while
    received SNR follows the average. An unclipped frame measures 12.7 dB of
    PAPR, so driving its peaks to full deviation puts its *average* 12.7 dB
    down. Clipping buys that back at the price of in-band distortion landing
    as an error vector on every subcarrier at once.

    Both sides of that trade are measurable in software, and the default comes
    from measuring them rather than from picking a round number. Clipping to
    a target costs the EVM in column 2 and buys the average level in column 3;
    column 4 is what the two come to together, at three channel SNRs quoted at
    the unclipped average level:

        target  achieved   EVM     gain    net SNR at ch = 12 / 15 / 20 dB
          5       5.3     -9.4     7.5        9.0    9.2    9.3
          6       6.3    -11.7     6.6       10.9   11.3   11.6
          7       7.3    -15.3     5.6       13.3   14.2   14.9
          8       8.2    -19.8     4.6       14.9   16.7   18.6
          9       9.3    -25.4     3.6       15.2   17.8   21.4
         10      10.1    -30.3     2.8       14.7   17.5   22.1
         none    12.7     ---      0.2       12.2   15.2   20.2

    9 dB is the optimum and it is a flat one -- anything from 8 to 10 is
    within half a dB of it, at every channel SNR in the range this bench
    plausibly runs at. It is worth about 2.6 dB over not clipping, which is
    most of what a step up in constellation costs.

    Note what the table does *not* model: the radio's own limiter. These
    figures assume the audio chain is linear up to the peak and that all the
    clipping happens here, where the splatter can be filtered off. Whether
    that holds on an HT mic input is exactly what sweep_ofdm.py --drive is
    for, and it is the one part of this module with no bench evidence behind
    it at all.
    """
    if not np.isfinite(profile.papr_db) or profile.papr_db <= 0:
        return audio
    mask = _band_mask(profile, len(audio))
    for _ in range(iterations):
        if measured_papr_db(audio) <= profile.papr_db + _PAPR_TOLERANCE_DB:
            break
        rms = np.sqrt(np.mean(audio ** 2))
        if rms <= 0:
            return audio
        limit = rms * 10 ** ((profile.papr_db - _PAPR_OVERSHOOT_DB) / 20)
        audio = np.clip(audio, -limit, limit)
        audio = np.fft.irfft(np.fft.rfft(audio) * mask, n=len(audio))
    return audio


def _apply_ramp(signal, ramp_ms=5):
    ramp_len = min(int(SAMPLE_RATE * ramp_ms / 1000), len(signal) // 2)
    if ramp_len <= 0:
        return signal
    window = np.hanning(2 * ramp_len)
    signal = signal.copy()
    signal[:ramp_len] *= window[:ramp_len]
    signal[-ramp_len:] *= window[ramp_len:]
    return signal


def data_symbol_values(payload: bytes, profile: OfdmProfile):
    """(n_symbols, n_carriers) of constellation points for `payload`.

    The bracketed body -- length(16) | payload | crc16 -- is byte-identical to
    whale/framing.py's, CRC included, so an integrated version could share a
    parser. Only the mapping from bits onto the channel differs.
    """
    body = len(payload).to_bytes(framing.LENGTH_FIELD_BITS // 8, "big") + payload
    crc = framing.crc16_ccitt_false(body)
    bits = framing.bytes_to_bits(body + bytes([(crc >> 8) & 0xFF, crc & 0xFF]))
    per_symbol = profile.bits_per_symbol
    n_symbols = -(-len(bits) // per_symbol)
    padded = np.zeros(n_symbols * per_symbol, dtype=np.int64)
    padded[:len(bits)] = bits
    values = _bits_to_values(_whiten(padded), profile.bits_per_carrier)
    return constellation(profile.bits_per_carrier)[values].reshape(n_symbols, profile.n_carriers)


def modulate(payload: bytes, profile: OfdmProfile, amplitude=0.9) -> np.ndarray:
    """One keying's worth of audio: `payload` framed and modulated.

    Built as one array and ramped once at each end, for the reason
    afsk.modulate records: separately-ramped pieces concatenated put a fade to
    silence at every join, and with the receiver's AGC pumping through those
    gaps the burst work lost about two thirds of its post-join frames.

    `amplitude` is the *peak*, after clipping, so the average level the radio
    sees is that minus the achieved PAPR: 0.9 peak at 9.3 dB is an RMS of
    0.31, against the 0.42 of afsk.modulate's constant-envelope 0.6.

    That 2.6 dB is a handicap no FSK mode in this repo carries, and it does
    not go away -- reaching 0.42 RMS at 9 dB of PAPR would need a peak of
    1.18, which is past full scale. It is the standing cost of a
    non-constant-envelope mode on a peak-limited transmitter, and it is
    already counted in the link budget in the module docstring.
    """
    pieces = [_pad_audio(profile, HEAD_PAD_SECONDS, seed=1),
              _with_prefix(profile, preamble_audio(profile))]
    train = _symbol_audio(profile, profile.carriers, training_values(profile))
    pieces += [_with_prefix(profile, train)] * profile.n_train
    for i, row in enumerate(data_symbol_values(payload, profile), 1):
        pieces.append(_with_prefix(profile, _symbol_audio(profile, profile.carriers, row)))
        if profile.pilot_interval and i % profile.pilot_interval == 0:
            # This known full-band symbol is both an EQ refresh and a local
            # timing anchor. Unlike the acquisition preamble it need not use
            # even carriers: the receiver already knows approximately where
            # it is and searches only inside one cyclic prefix.
            pieces.append(_with_prefix(profile, train))
    pieces.append(_pad_audio(profile, TAIL_PAD_SECONDS, seed=2))

    audio = np.concatenate(pieces)
    audio = _clip_and_filter(profile, audio)
    peak = np.max(np.abs(audio)) or 1.0
    return _apply_ramp(audio * (amplitude / peak)).astype(np.float32)


# -- demodulate ------------------------------------------------------------


def _repetition_score(audio, half):
    """Normalised correlation between each window and the window one `half`
    later, in [-1, 1]. 1 means the two halves are identical, which is what the
    preamble's even-only carriers arrange.

    Immune to any gain and any phase the channel applies, because it compares
    the signal against itself rather than against a template -- which is what
    makes it the right *first* stage on a dispersive channel, and also why it
    cannot be the last one (see the module docstring: a steady tone scores
    1.000 here).
    """
    n = len(audio) - 2 * half
    if n <= 0:
        return np.zeros(0)
    a, b = audio[:-half], audio[half:]
    prod = np.concatenate([[0.0], np.cumsum(a * b)])
    ea = np.concatenate([[0.0], np.cumsum(a * a)])
    eb = np.concatenate([[0.0], np.cumsum(b * b)])
    p = prod[half:half + n] - prod[:n]
    ra = ea[half:half + n] - ea[:n]
    rb = eb[half:half + n] - eb[:n]
    denom = np.sqrt(np.maximum(ra * rb, 0.0))
    floor = max(1e-12, 0.05 * float(np.mean(denom)) if len(denom) else 1e-12)
    return np.nan_to_num(p / np.maximum(denom, floor), nan=0.0, posinf=0.0, neginf=0.0)


def _match_score(analytic, template, offsets):
    """Normalised cross-correlation magnitude against the known preamble, at
    each offset in `offsets`. In [0, 1]; 1 is an exact match up to arbitrary
    gain and phase.

    Magnitude of a complex correlation rather than a real one, so that the
    single phase rotation the audio chain applies to a narrow band does not
    have to be known or estimated -- which is the same reason the repetition
    metric works, obtained here without its blind spot.

    Normalised by the window's own energy, for the reason
    afsk._normalised_correlation spells out at length: a measure whose
    denominator comes from the whole buffer scores the same frame differently
    depending on how much dead air surrounds it, and that measure reported a
    sync lock on all 30 off-air captures containing no sync word.
    """
    m = len(template)
    t = np.asarray(template, dtype=complex)
    t = t / (np.linalg.norm(t) or 1.0)
    out = np.zeros(len(offsets))
    for i, d in enumerate(offsets):
        if d < 0 or d + m > len(analytic):
            continue
        window = analytic[d:d + m]
        energy = np.linalg.norm(window)
        if energy <= 1e-12:
            continue
        out[i] = abs(np.vdot(t, window)) / energy
    return out


def _propose(audio, profile: OfdmProfile):
    """Candidate preamble positions, in time order: the peak of each distinct
    region where the repetition metric clears its threshold.

    Time order rather than strongest first, for afsk._sync_peaks' reason -- the
    RX buffer routinely holds a garbled self-echo of our own last transmission
    alongside the peer's real reply, and the echo is often the louder of the
    two. The earliest that decodes is the one wanted.
    """
    half = profile.n_fft // 2
    score = _repetition_score(audio, half)
    above = np.flatnonzero(score > REPETITION_THRESHOLD)
    if above.size == 0:
        return [], score
    gap = max(1, int(_MIN_PEAK_SEPARATION_SYMBOLS * profile.symbol_samples))
    splits = np.flatnonzero(np.diff(above) > gap)
    peaks = [int(g[np.argmax(score[g])]) for g in np.split(above, splits + 1)]
    if len(peaks) > _MAX_SYNC_CANDIDATES:
        peaks = sorted(sorted(peaks, key=lambda i: score[i], reverse=True)[:_MAX_SYNC_CANDIDATES])
    return peaks, score


def _refine(analytic, profile: OfdmProfile, proposal):
    """Sample-accurate start of the preamble's useful part, and the match
    score there.

    The repetition metric produces a plateau as wide as the cyclic prefix
    rather than a peak -- every offset that keeps both halves inside the
    preamble scores the same -- so it locates the preamble to within a prefix
    and no better. The cross-correlation is searched across that plateau and
    a prefix either side of it.
    """
    span = np.arange(proposal - profile.cp, proposal + 2 * profile.cp + 1)
    scores = _match_score(analytic, _preamble_template(profile), span)
    best = int(np.argmax(scores))
    return int(span[best]), float(scores[best])


def _equalise(profile: OfdmProfile, audio, first_symbol_start, n_symbols):
    """Equalise data using the initial training and periodic tracking pilots.

    One complex number per subcarrier is the whole equaliser, and that is the
    point of the cyclic prefix: dispersion the prefix covers shows up here as
    a gain and a phase per subcarrier, nothing more. A timing error inside the
    prefix shows up the same way -- as a phase ramp across k -- and since the
    training symbol was read through the same window offset, it carries the
    same ramp and the division removes it exactly. Nothing corrects timing
    anywhere in this module for that reason.

    A tracking pilot follows every ``pilot_interval`` data symbols. Each is
    located again inside a one-prefix neighbourhood, so it is simultaneously
    a symbol-timing anchor and a fresh full-band channel estimate. Data between
    two anchors uses linear interpolation of their complex channel estimates
    and their FFT-window offsets. A final partial group uses the last estimate.

    Returns (equalised data symbols, initial per-carrier channel,
    per-carrier initial-training noise estimate).
    """
    guard = int(_WINDOW_GUARD_FRACTION * profile.cp)
    step = profile.symbol_samples

    def window(index, shift=0):
        start = first_symbol_start + index * step - guard + int(round(shift))
        if start < 0:
            return np.zeros(0)
        return audio[start:start + profile.n_fft]

    estimates = []
    for i in range(profile.n_train):
        w = window(i)
        if len(w) < profile.n_fft:
            return None, None, None
        estimates.append(np.fft.rfft(w)[profile.carriers] * np.conj(training_values(profile)))
    estimates = np.vstack(estimates)
    channel = estimates.mean(axis=0)
    noise = (estimates.var(axis=0, ddof=1) if profile.n_train > 1
             else np.zeros(profile.n_carriers))

    # Build physical positions for data and pilots. Anchor coordinates are in
    # data-symbol time: 0 is the initial estimate, N is the pilot after N data
    # symbols. That makes interpolation independent of the pilots' airtime.
    data_positions = data_physical_indices(profile, n_symbols)
    pilot_positions = []
    physical = profile.n_train
    for data_index in range(n_symbols):
        physical += 1
        if (profile.pilot_interval
                and (data_index + 1) % profile.pilot_interval == 0):
            pilot_positions.append((data_index + 1, physical))
            physical += 1

    anchors = [(0, channel, 0.0)]
    current_channel = channel
    current_shift = 0.0
    expected_values = training_values(profile)
    for data_coordinate, index in pilot_positions:
        # Search locally around the predicted window. Score in the frequency
        # domain against what the current channel says the known pilot should
        # look like. The channel may have moved; normalised magnitude keeps
        # that from moving the timing peak merely because gain changed.
        best = None
        for shift in range(-guard, guard + 1):
            w = window(index, shift)
            if len(w) < profile.n_fft:
                continue
            observed = np.fft.rfft(w)[profile.carriers]
            expected = current_channel * expected_values
            denom = np.linalg.norm(observed) * np.linalg.norm(expected)
            score = abs(np.vdot(expected, observed)) / denom if denom > 1e-12 else 0.0
            if best is None or score > best[0]:
                best = (score, float(shift), observed)
        if best is None:
            break
        _, current_shift, observed = best
        current_channel = observed * np.conj(expected_values)
        anchors.append((data_coordinate, current_channel, current_shift))

    rows = []
    for data_index, index in enumerate(data_positions):
        left = anchors[0]
        right = None
        for anchor in anchors[1:]:
            if anchor[0] > data_index:
                right = anchor
                break
            left = anchor
        if right is None:
            estimate = left[1]
            shift = left[2]
        else:
            fraction = (data_index + 1 - left[0]) / (right[0] - left[0])
            estimate = left[1] + fraction * (right[1] - left[1])
            shift = left[2] + fraction * (right[2] - left[2])
        w = window(index, shift)
        if len(w) < profile.n_fft:
            break
        safe = np.where(np.abs(estimate) > 1e-12, estimate, 1e-12)
        rows.append(np.fft.rfft(w)[profile.carriers] / safe)
    return (np.vstack(rows) if rows else np.zeros((0, profile.n_carriers))), channel, noise


def _demap(symbols, bits_per_carrier):
    points = constellation(bits_per_carrier)
    flat = symbols.ravel()
    values = np.argmin(np.abs(flat[:, None] - points[None, :]), axis=1)
    return values.astype(np.int64)


def _decode_at(audio, profile: OfdmProfile, preamble_start, confidence):
    """Attempts to read a frame whose preamble's useful part starts at
    `preamble_start`. Same return shape as demodulate()."""
    first_symbol_start = preamble_start + profile.n_fft + profile.cp
    available_physical = ((len(audio) - first_symbol_start) // profile.symbol_samples
                          - profile.n_train)
    available = data_symbols_from_physical(profile, available_physical)
    result = {"synced": False, "payload": None, "confidence": confidence,
              "start_index": preamble_start,
              "sync_end_index": first_symbol_start}
    if preamble_start < 0 or available < 1:
        # Not even one data symbol has landed, so there is nothing to read a
        # length out of. Say nothing definitive: this may be a real frame
        # still streaming in, and discarding one costs a retransmit while
        # waiting one more poll costs nothing.
        return result

    take = min(available, max_credible_data_symbols(profile))
    equalised, _, _ = _equalise(profile, audio, first_symbol_start, take)
    if equalised is None or not len(equalised):
        return result

    bits = _whiten(_values_to_bits(_demap(equalised, profile.bits_per_carrier),
                                   profile.bits_per_carrier)).tolist()
    payload = framing.parse_frame_bits(bits)
    length = framing.declared_length(bits)
    needed = data_symbols_for_length(profile, length) if length is not None else None

    result["payload"] = payload
    result["synced"] = payload is not None
    if needed is not None and needed > max_credible_data_symbols(profile):
        # A length no keying at this rate could be sending -- nothing will
        # ever arrive to settle it, so call the sync dead and end it at the
        # preamble rather than measuring a skip with a length just rejected.
        result["end_index"] = result["sync_end_index"]
    elif payload is not None or (needed is not None and len(equalised) >= needed):
        end_symbols = profile.n_train + physical_symbols_for_data(profile, min(needed, take))
        result["end_index"] = first_symbol_start + end_symbols * profile.symbol_samples
    return result


def demodulate(audio, profile: OfdmProfile):
    """Finds and decodes the earliest frame in `audio`. Returns a dict with at
    least 'synced' and 'payload' (None if nothing usable was found).

    Two-stage sync: the repetition metric proposes positions cheaply and
    without caring what the channel did to the signal, and a normalised
    cross-correlation against the known preamble confirms or rejects each one.
    Neither is sufficient alone -- see the module docstring.

    Every confirmed position is tried in time order and the first whose CRC
    checks out wins; when nothing decodes, a frame that may still be arriving
    is preferred over the earliest dead end, so the caller waits one more poll
    rather than throwing away a frame mid-flight.
    """
    audio = np.asarray(audio, dtype=np.float64)
    if len(audio) < 2 * profile.symbol_samples:
        return {"synced": False, "payload": None, "confidence": 0.0}

    proposals, rep = _propose(audio, profile)
    if not proposals:
        return {"synced": False, "payload": None, "confidence": 0.0,
                "repetition": float(np.max(rep)) if len(rep) else 0.0}

    analytic = hilbert(audio)
    near_miss = still_arriving = None
    best_confidence = 0.0
    for proposal in proposals:
        start, confidence = _refine(analytic, profile, proposal)
        best_confidence = max(best_confidence, confidence)
        if confidence < profile.confidence_threshold:
            continue
        result = _decode_at(audio, profile, start, confidence)
        if result["payload"] is not None:
            return result
        if "end_index" in result:
            near_miss = near_miss or result
        else:
            still_arriving = still_arriving or result
    return still_arriving or near_miss or {
        "synced": False, "payload": None, "confidence": best_confidence}


# -- candidate generation --------------------------------------------------


# Cyclic prefix lengths worth generating, as a fraction of the useful symbol.
#
# This is the parameter with the real trade in it. The prefix is pure
# overhead -- prefix_overhead is exactly the throughput it costs -- and what
# it buys is the delay spread the channel is allowed to have. Nothing in this
# repo has ever measured that delay spread, because no mode before this one
# could have used the number: probe_channel.py is what measures it, and until
# it has, generating a range and walking it is the honest approach.
CP_FRACTIONS = (1 / 4, 1 / 8, 1 / 16)

# Useful symbol lengths in samples, i.e. subcarrier spacings of 30, 40, 50, 60
# and 75 Hz. Longer symbols spend proportionally less on the prefix for the
# same absolute delay-spread protection, which argues for the long end; they
# also put more subcarriers in the same band, each with less energy, which
# argues for nothing in particular since the total is the same. The real
# reason not to go arbitrarily long is that a symbol is the unit an impulsive
# hit destroys, and this bench has a receiver known to black out for ~110ms.
N_FFT_CHOICES = (1600, 1200, 960, 800, 640)


def candidates(bits=(1, 2, 3, 4), n_ffts=N_FFT_CHOICES, cp_fractions=CP_FRACTIONS,
               band=(BAND_LOW_HZ, BAND_HIGH_HZ), **kwargs):
    """Every profile the constraints allow, best payload throughput first.

    The sweep walks this in order and stops at the first that decodes 100%
    both directions, which makes "max throughput subject to 100%" a property
    of the ordering rather than of anyone's judgement about which candidate
    ought to win. Same construction as experiments/mfsk.
    """
    out = []
    for bpc in bits:
        for n_fft in n_ffts:
            for frac in cp_fractions:
                cp = int(round(n_fft * frac))
                if cp < 16:
                    continue
                name = (f"ofdm{1 << bpc}_{SAMPLE_RATE // n_fft}hz"
                        f"_cp{frac.as_integer_ratio()[1]}")
                profile = OfdmProfile(
                    name=name, n_fft=n_fft, cp=cp, bits_per_carrier=bpc,
                    band_low=band[0], band_high=band[1], **kwargs)
                if profile.max_payload > 0 and profile.n_carriers >= 4:
                    out.append(profile)
    out.sort(key=lambda p: (-p.payload_bitrate, -p.cp_seconds, p.bits_per_carrier))
    return out


def describe(profile: OfdmProfile) -> str:
    pilots = (f" pilot/{profile.pilot_interval}" if profile.pilot_interval else "")
    return (f"{profile.name:<20} {profile.n_carriers:>3}x{1 << profile.bits_per_carrier:<3} "
            f"sp={profile.spacing:>5.1f}Hz cp={profile.cp_seconds * 1000:>5.2f}ms"
            f"({profile.prefix_overhead * 100:>4.1f}%){pilots} "
            f"band={profile.tone_low:>6.1f}-{profile.tone_high:<6.1f} "
            f"sym={profile.symbol_rate:>5.1f}/s raw={profile.raw_bitrate:>6.0f}bps "
            f"payload={profile.max_payload:>4d}B ={profile.payload_bitrate:>6.1f}bps "
            f"keying={keying_seconds(profile, profile.max_payload):.2f}s")
