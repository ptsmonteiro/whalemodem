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
whale/hw/radios.py. The HT is a Wouxun KG-UV9D Plus, which is also what the
SNR figures below were taken with, so they stand. (A Baofeng UV-B5 stood in
for a few hours on 2026-08-18 and is what framing.HEAD_PAD_SECONDS is sized
against; these numbers were never re-taken on it.) scripts/measure_snr.py measures in-band SNR (signal RMS
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

import dataclasses
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from scipy.signal import correlate

from whale import framing, rx_audio

SAMPLE_RATE = 48000
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
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

# Fallback link-layer payload bytes per DATA frame, for an ad-hoc Profile
# built by a sweep script that has no opinion about size. Every shipped
# profile below sizes itself from the airtime budget instead -- see
# max_chunk_for_useful_frame.
CHUNK_SIZE = 40

# DATA type and flags/sequence live in the checked packet header; the
# negotiated body contains chunk bytes only.
DATA_FRAME_HEADER_BYTES = 0

# The longest frame the decoder will believe a declared length describes.
#
# A receiver cannot check a length field before buffering everything that
# field claims, because the CRC validating it sits *after* the payload it
# describes. So a declared length is a promise the decoder has to honour or
# reject on its face, and honouring an implausible one is not free: _try_sync
# reports it as "still arriving", which tells whale/link.py's decode loop to
# stop pruning and re-search the whole RX buffer every poll (see
# _prune_stale), turning a ~40ms poll into a ~300ms one that lands straight
# on the turnaround.
#
# With the old 8-bit length field this could not arise. 255 bytes at 300 baud
# is 7.1s, inside transport.RX_BUFFER_SECONDS, so *every* value a garbage
# length byte could take was one the decoder would eventually collect in full
# and reject on CRC. A 16-bit field breaks that by ~200x: at 1200 baud some
# 98% of random length values describe a frame longer than the buffer can
# ever hold, and a false sync peak on noise -- routine, the buffer regularly
# holds a garbled self-echo of our own last transmission -- produces exactly
# a random length value.
#
# The property is therefore restored explicitly rather than left to
# arithmetic that happened to work out. 8s is comfortably longer than any
# useful frame this modem makes (MAX_USEFUL_FRAME_SECONDS is 3.0), with room for
# the sweep scripts, which deliberately transmit past that budget; and
# comfortably shorter than transport.RX_BUFFER_SECONDS, so anything the
# decoder does agree to wait for is something it can still be holding whole
# when the last bit of it lands.
MAX_CREDIBLE_FRAME_SECONDS = 8.0


def max_credible_frame_bits(baud):
    """Sync word through CRC, for the longest frame `baud` could be part-way
    through inside MAX_CREDIBLE_FRAME_SECONDS. A declared length implying
    more than this is a dead sync, not a frame still arriving."""
    return int(MAX_CREDIBLE_FRAME_SECONDS * baud)


class Codec(Protocol):
    """Codec strategy used by a negotiable Profile."""

    tx_sample_rate: int
    rx_sample_rate: int

    def encode(self, payload: bytes, profile: "Profile"): ...
    def decode(self, audio, profile: "Profile"): ...
    def airtime(self, payload_len: int, profile: "Profile") -> float: ...


class CpfskCodec:
    """Current CPFSK + PN-sync + length/CRC framing implementation."""

    tx_sample_rate = SAMPLE_RATE
    rx_sample_rate = RX_SAMPLE_RATE

    def encode(self, payload: bytes, profile: "Profile", *, include_head=True,
               head_seconds=framing.HEAD_PAD_SECONDS):
        return modulate(payload, profile=profile, sample_rate=self.tx_sample_rate,
                        include_head=include_head, head_seconds=head_seconds)

    def decode(self, audio, profile: "Profile", **kwargs):
        return demodulate(audio, profile=profile, sample_rate=self.rx_sample_rate, **kwargs)

    def airtime(self, payload_len: int, profile: "Profile") -> float:
        return frame_seconds(payload_len, profile=profile)


CPFSK_CODEC = CpfskCodec()


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
    codec: Codec = field(default=CPFSK_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self):
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self):
        return self.codec.rx_sample_rate

    @property
    def head_match_allowance_seconds(self):
        """How short a head observation may fall before the link believes it.

        The backward pad matcher gives up a whole PAD_MATCH_WINDOW_SYMBOLS
        window when it trips, so an observation is systematically short by up
        to that much even on a perfectly received head.
        """
        return PAD_MATCH_WINDOW_SYMBOLS / self.baud

    def encode(self, payload: bytes, *, include_head=True,
               head_seconds=framing.HEAD_PAD_SECONDS):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int):
        return self.codec.airtime(payload_len, self)


# -- the useful-frame budget, and the chunk sizes it produces --------------
#
# Sync-through-CRC audio is capped in duration, and every profile's chunk_size
# is whatever fits inside that cap. The outer head pad and transport startup do
# not consume this budget because adaptive timing will vary them by radio pair.
#
# This is a design choice, not a measurement -- worth saying plainly, because
# the number looks like the kind that gets measured. Nothing on this bench
# fails at 3.0s of useful audio. Three things make a cap worth having anyway:
#
#   - Retransmit granularity. ARQ is stop-and-wait and a frame is all or
#     nothing, so a keying is the unit a single bit error destroys. Longer
#     keyings mean fewer, more expensive retries.
#   - Responsiveness. The link is half duplex and the peer cannot interject
#     while we hold the carrier, so keying length is a floor under turnaround
#     and under how quickly a DISC can be answered.
#   - Clock tolerance. demodulate() lays symbol points on a rigid grid with no
#     timing recovery, so a frame dies when accumulated timing error reaches
#     half a symbol -- a budget of 0.5/n_bits. Longer frames are the first
#     thing a sample-clock offset takes away, and the budget is spent on more
#     bits at every step up in baud. See
#     test_clock_offset_tolerance_is_half_a_symbol_over_the_frame.
#
# None of those has a cliff in it, so the figure is round rather than
# derived. If a radio ever does impose a hard limit -- some receivers mute
# themselves periodically to scan, on a timer set at the *far* station where
# this modem can neither see nor change it -- that would be a measurement,
# and it would belong here in place of this.
MAX_USEFUL_FRAME_SECONDS = 3.0

# Dead air inside a keying that isn't frame bits, PTT key-down to PTT
# release. This is the measured output-stream startup/fill time; leading and
# trailing protection is now entirely represented by on-air framing symbols.
# whale/transport.py asserts at import that it
# still agrees with those, which is where each is measured and documented.
# Not imported from there directly, so this module stays free of the
# sound-card dependencies -- tests/test_afsk_loopback.py runs with no audio
# stack at all.
#
# This was 0.40 when the sizing was first derived, from a nominal 0.13 for
# the stream fill. An acceptance run logs the real thing -- every
# transmission reports its audio length and its keyed length -- and put the
# overhead at 0.42-0.43s over 44 keyings, i.e. the profiles below were each
# ~20ms past the cap they were supposed to sit under. Corrected here rather
# than by widening the cap, because the cap is the part with a measurement
# behind it.
KEYING_OVERHEAD_SECONDS = 0.16


def frame_bits(payload_len, baud, *, include_head=True):
    """Total bits on air for one frame, head pad included."""
    return ((len(framing.head_pad_bits(baud)) if include_head else 0)
            + len(framing.sync_bits(baud))
            + framing.frame_bits_for_length(payload_len))


def max_payload_for_useful_frame(baud, budget=MAX_USEFUL_FRAME_SECONDS):
    """Largest AFSK payload whose sync-through-CRC audio fits in `budget`.

    The outer head pad and transport startup are deliberately excluded:
    adaptive timing will vary them per radio pair.
    """
    spare_bits = budget * baud - frame_bits(0, baud, include_head=False)
    return max(0, min(int(spare_bits // 8), framing.MAX_PAYLOAD_BYTES))


def max_chunk_for_useful_frame(baud, budget=MAX_USEFUL_FRAME_SECONDS):
    """Largest DATA body in one checked packet under the useful budget."""
    size = 0
    while (size < framing.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
           and frame_bits(framing.AIR_HEADER_BYTES + size + 1, baud,
                          include_head=False) / baud <= budget):
        size += 1
    return size


PROFILE_300 = Profile(name="300baud", mode_id=0, baud=BAUD, freq0=FREQ_0, freq1=FREQ_1,
                      chunk_size=max_chunk_for_useful_frame(BAUD))

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
# Frame size is not a reliability limit at this baud: the payload sweep runs
# 100% both directions at every payload up to 255 bytes, so chunk_size comes
# straight from the keying budget above rather than from any measured
# ceiling. The sizes it produces are ones this profile has actually been
# swept at (160 bytes, 12/12 both directions during the 1200/1800 A/B)
# rather than extrapolations, and acceptance runs have since carried such
# frames over the air with no retransmit.
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
# timing and on the sync word itself, not on tone frequency. A 300 baud
# frame scores 0.34 against the 600 baud correlator and vice versa 0.37,
# both far below the 0.7 lock threshold. Those figures were 0.61 and 0.38
# when every profile shared one 63-bit sync word; they improved because
# framing.sync_bits now gives each baud its own m-sequence, so the profiles
# differ in what the correlator is looking for as well as in when. A full
# acceptance run with both profiles live on air produced no near-miss
# decodes, and test_a_frame_does_not_sync_another_profiles_correlator keeps
# every pairing on file.
PROFILE_600 = Profile(name="600baud", mode_id=1, baud=600, freq0=tones(600.0)[0],
                       freq1=tones(600.0)[1], chunk_size=max_chunk_for_useful_frame(600))

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
# wall, not a marginal case).
#
# This used to be the one profile the keying budget did not bind: a full
# keying at 1200 baud carries far more than the 8-bit length field could
# describe, so the chunk stopped at 255 bytes and most of a second of every
# keying went unspent. framing.LENGTH_FIELD_BITS is 16 now and the budget
# binds here like everywhere else -- see that comment for the on-air
# compatibility break it cost.
#
# There is no frame-size ceiling at this baud: the payload sweeps run 100%
# both directions at every payload up to 255 bytes, so chunk_size is a
# turnaround/retransmit-cost choice rather than a reliability one.
#
# One measurement worth keeping from that work, because it rules out a cause
# that would otherwise be re-suspected the next time a long frame fails:
# sample-clock drift between the two sound cards is not a factor.
# scripts/measure_clock_offset.py puts them 3.4 ppm apart (-3.7/+3.1 ppm on
# the two legs, summing to -0.6, i.e. properly reciprocal), some 100x too
# little to cost a bit at any frame size this modem sends -- a frame dies
# when timing error reaches half a symbol, which needs ~366 ppm at 160
# bytes. tests/test_afsk_loopback.py keeps that failure mode on file, since
# from the decoder's output alone it looks like any other size ceiling.
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
                        chunk_size=max_chunk_for_useful_frame(1200))

# Slowest -> fastest. Index order is also step order for mid-session
# adaptation (whale/link.py steps to PROFILES[i-1] / PROFILES[i+1]).
PROFILES = [PROFILE_300, PROFILE_600, PROFILE_1200]
PROFILES_BY_ID = {p.mode_id: p for p in PROFILES}

# Always used for CONNECT/CONNECT_ACK (before speed is agreed) and, per
# whale/link.py's design, for every other control-plane packet (DISC,
# DATA_ACK, floor control) regardless of the current data speed --
# the control plane always runs at the most robust profile so it keeps
# working even when the data channel is struggling. Only PT_DATA/PT_DATA_ACK
# ever use the negotiated profile.
CONTROL_PROFILE = PROFILE_300

# Outer-pad measurement policy. Sparse hard-decision errors are tolerated,
# but a window that exceeds this budget is treated wholly as suspect. Dropping
# the complete failing window is intentional: it prevents the look-ahead
# needed to distinguish clipping from an isolated error from inflating the
# received-symbol count and selecting unsafe timing.
PAD_MATCH_WINDOW_SYMBOLS = 16
PAD_MATCH_MAX_ERRORS = 2


def profiles_for_budget(budget=MAX_USEFUL_FRAME_SECONDS):
    """The three CPFSK profiles, sized for a `budget`-second useful frame.

    The module-level PROFILE_* objects are what this returns for the default
    budget, and are returned unchanged in that case so identity comparisons
    and the PROFILES_BY_ID table keep working. Only chunk_size varies with
    the budget: tones, baud and mode_id are on-air facts and are not a
    station's to choose. See whale/policy.py, which owns the budget.
    """
    if budget == MAX_USEFUL_FRAME_SECONDS:
        return PROFILES
    return [dataclasses.replace(p, chunk_size=max_chunk_for_useful_frame(p.baud, budget))
            for p in PROFILES]


def default_registry(budget=MAX_USEFUL_FRAME_SECONDS):
    """Returns the built-in modes through the link-facing registry API."""
    from whale.waveform import ModeRegistry
    profiles = profiles_for_budget(budget)
    # The control plane keeps whatever position CONTROL_PROFILE holds in the
    # ladder rather than the object itself, so a re-sized ladder does not end
    # up with a control profile from the default one.
    control = profiles[PROFILES.index(CONTROL_PROFILE)]
    return ModeRegistry(profiles, control)


def _cpfsk_tone(bits, sps, sample_rate, freq0, freq1):
    freqs_per_bit = np.where(np.asarray(bits) == 0, freq0, freq1)
    inst_freq = np.repeat(freqs_per_bit, sps)
    phase = 2 * np.pi * np.cumsum(inst_freq) / sample_rate
    return np.cos(phase)


def _apply_ramp(signal, sample_rate, ramp_ms=5):
    """Ramp only the start; the head absorbs it and no tail guard exists."""
    ramp_len = min(int(sample_rate * ramp_ms / 1000), len(signal))
    if ramp_len <= 0:
        return signal
    window = np.hanning(2 * ramp_len)
    signal = signal.copy()
    signal[:ramp_len] *= window[:ramp_len]
    return signal


def modulate(payload: bytes, profile: Profile = PROFILE_300, sample_rate=SAMPLE_RATE,
             amplitude=0.6, *, include_head=True,
             head_seconds=framing.HEAD_PAD_SECONDS):
    """One keying's worth of audio: `payload` framed (sync word, length,
    CRC and head pad) and modulated as one continuous signal.

    A multi-frame variant of this existed briefly, for putting a burst of
    link-layer frames in a single keying. That was rolled back with the
    rest of the burst work; if it returns, note what the first hardware run
    established: the frames have to share one _cpfsk_tone call. Modulating
    each separately and concatenating the audio put a 10ms fade to silence
    at every join because that experiment's symmetric ramp faded each frame
    down and the next back up -- inaudible on a clean channel, but with the receiver's
    AGC/limiter pumping through the gap the frame after each join decoded
    only about a third of the time.
    """
    sps = round(sample_rate / profile.baud)
    bits = framing.build_frame_bits(payload, baud=profile.baud,
                                    include_head=include_head, head_seconds=head_seconds)
    tone = _cpfsk_tone(bits, sps, sample_rate, profile.freq0, profile.freq1)
    audio = amplitude * tone
    ramped = _apply_ramp(audio, sample_rate)
    if not include_head:
        ramped[:int(sample_rate * 0.005)] = audio[:int(sample_rate * 0.005)]
    return ramped.astype(np.float32)


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


def _sync_template(baud, sps, sample_rate, freq0, freq1):
    """The correlation target for `baud`. The sync word's length is a
    function of baud (framing.sync_bits), so this is too -- a template built
    at the wrong baud is a template for a different frame."""
    tone = _cpfsk_tone(framing.sync_bits(baud), sps, sample_rate, freq0, freq1)
    return _tone_energy_diff(tone, sample_rate, sps, freq0, freq1)


def _sync_snr_db(audio, start, profile, sample_rate=SAMPLE_RATE):
    """Effective SNR over a confirmed CPFSK sync word.

    Fit sine and cosine components for each of the two known tones plus DC.
    Separate tone coefficients tolerate ordinary receive-filter gain and
    phase differences. Anything the fitted sync cannot explain -- noise,
    interference, distortion, clipping, or timing error -- is residual, so
    this is deliberately an effective receive SNR rather than an optimistic
    amplitude reading.
    """
    sps = round(sample_rate / profile.baud)
    bits = np.asarray(framing.sync_bits(profile.baud), dtype=np.uint8)
    count = len(bits) * sps
    observed = np.asarray(audio[start:start + count], dtype=np.float64)
    if len(observed) != count:
        return None

    bit_samples = np.repeat(bits, sps)
    reference = _cpfsk_tone(bits, sps, sample_rate, profile.freq0, profile.freq1)
    # _cpfsk_tone returns cosine. Its cumulative phase is known, so derive
    # the matching sine without another phase accumulator.
    freqs = np.repeat(np.where(bits == 0, profile.freq0, profile.freq1), sps)
    phase = 2 * np.pi * np.cumsum(freqs) / sample_rate
    quadrature = np.sin(phase)
    tone0 = bit_samples == 0
    tone1 = ~tone0
    design = np.column_stack((
        reference * tone0, quadrature * tone0,
        reference * tone1, quadrature * tone1,
        np.ones(count),
    ))
    coefficients, _, _, _ = np.linalg.lstsq(design, observed, rcond=None)
    fitted = design @ coefficients
    signal_power = float(np.mean((fitted - coefficients[-1]) ** 2))
    residual_power = float(np.mean((observed - fitted) ** 2))
    if signal_power <= 0.0:
        return None
    return float(10.0 * np.log10(signal_power / max(residual_power, 1e-30)))


def frame_seconds(payload_len=framing.MAX_PAYLOAD_BYTES, profile: Profile = PROFILE_300,
                  *, include_head=True):
    """How long the frame's own audio lasts."""
    return frame_bits(payload_len, profile.baud, include_head=include_head) / profile.baud


def keying_seconds(payload_len=framing.MAX_PAYLOAD_BYTES, profile: Profile = PROFILE_300):
    """How long the channel is occupied, PTT key-down to PTT release --
    frame_seconds plus transport startup. This reports total occupancy; it is
    not the useful-frame sizing restriction."""
    return KEYING_OVERHEAD_SECONDS + frame_seconds(payload_len, profile)


def data_keying_seconds(chunk_len, profile: Profile = PROFILE_300):
    """Complete single-waveform DATA keying."""
    return KEYING_OVERHEAD_SECONDS + frame_seconds(
        framing.AIR_HEADER_BYTES + chunk_len, profile)


def useful_data_seconds(chunk_len, profile: Profile = PROFILE_300):
    """DATA audio from sync through the header and optional body CRC.

    This is the bounded part; outer timing protection and transport latency
    are intentionally absent.
    """
    return frame_seconds(framing.AIR_HEADER_BYTES + chunk_len, profile,
                         include_head=False)


# How many correlation peaks demodulate() will attempt to decode in one
# call, and how close together two peaks may be before they're treated as
# one. A frame's sync word is 63 symbols at the slowest profile and longer
# at every other (framing.sync_bits), so peaks closer than a few symbols are
# the same lock seen twice; the cap keeps a noisy buffer full of marginal
# peaks from turning one poll into hundreds of CRC attempts.
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


def _try_sync(diff, i_star, sps, confidence, max_credible_bits, n_sync, baud,
              head_seconds):
    """Attempts to read a frame at one correlation peak. Same return shape
    as demodulate(). `n_sync` is the sync word's length in bits at this
    profile's baud -- it sets where the length field starts, so it has to
    come from the caller rather than a module constant."""
    first_index = i_star + (sps - 1)
    max_symbols = (len(diff) - 1 - first_index) // sps + 1
    if max_symbols < n_sync + framing.LENGTH_FIELD_BITS:
        # Not even the length field has fully arrived yet -- this may well
        # be a real frame still streaming in; say nothing definitive rather
        # than reporting it as a dead end.
        return {"synced": False, "confidence": confidence, "payload": None}

    num_symbols = min(max_symbols, max_credible_bits)
    sample_indices = first_index + sps * np.arange(num_symbols)
    decoded_bits = (diff[sample_indices] > 0).astype(int).tolist()

    header_valid = framing.header_is_valid(decoded_bits[n_sync:])
    if header_valid is False:
        return {
            "synced": False,
            "confidence": confidence,
            "_hard_bits": decoded_bits,
            "start_index": i_star,
            "sync_end_index": i_star + sps * n_sync,
            "end_index": i_star + sps * min(
                n_sync + framing.frame_bits_for_length(framing.AIR_HEADER_BYTES),
                num_symbols),
            "payload": None,
        }

    payload = framing.parse_frame_bits(decoded_bits[n_sync:])
    length = framing.declared_length(decoded_bits[n_sync:])
    total_bits_needed = n_sync + framing.frame_bits_for_length(length)
    result = {
        "synced": payload is not None,
        "confidence": confidence,
        "_hard_bits": decoded_bits,
        "start_index": i_star,
        # Where the sync word itself ends. A caller giving up on this
        # position only has to step past the sync to guarantee the same
        # peak cannot win again, and stepping past just that discards far
        # less unexamined audio than skipping to the end of a frame whose
        # declared length it has no reason to trust. See whale/link.py's
        # _decode_one.
        "sync_end_index": i_star + sps * n_sync,
        "payload": payload,
    }
    if total_bits_needed > max_credible_bits:
        # The declared length describes a frame longer than this baud could
        # credibly be sending, so no amount of waiting will ever produce the
        # CRC that would settle it -- see MAX_CREDIBLE_FRAME_SECONDS. Report
        # a dead sync rather than a frame in flight, and end it at the sync
        # word: a length we have just called implausible is not a length to
        # measure a skip with.
        result["end_index"] = result["sync_end_index"]
    elif payload is not None:
        head_bits = framing.head_pad_bits(baud, head_seconds)

        def adjacent_matches(expected, indices):
            mismatches = []
            count = 0
            for count, (bit, index) in enumerate(zip(expected, indices), 1):
                # Running outside retained audio is a known hard boundary,
                # not a noisy symbol, so no decision window is needed.
                if index < 0 or index >= len(diff):
                    return count - 1
                mismatches.append((diff[index] > 0) != bool(bit))
                if len(mismatches) > PAD_MATCH_WINDOW_SYMBOLS:
                    mismatches.pop(0)
                if (len(mismatches) == PAD_MATCH_WINDOW_SYMBOLS
                        and sum(mismatches) > PAD_MATCH_MAX_ERRORS):
                    return count - PAD_MATCH_WINDOW_SYMBOLS
            return count

        # Walk backward from sync through the head sequence. A rolling window
        # tolerates isolated hard-decision errors;
        # the whole first failing window is excluded from the count.
        result["head_symbols_received"] = adjacent_matches(
            reversed(head_bits),
            (first_index - sps * offset for offset in range(1, len(head_bits) + 1)),
        )
        # The mode-independent form the link's head feedback works in; the
        # symbol count above stays for the logs and the CPFSK tests.
        result["head_seconds_received"] = result["head_symbols_received"] / baud
        result["end_index"] = i_star + sps * total_bits_needed
    elif max_symbols >= total_bits_needed:
        # Either it decoded, or we had every bit the claimed length says it
        # needed and it *still* didn't check out -- this one's done, safe
        # to skip past. If neither holds, the frame may just still be
        # arriving; leave end_index out so the caller keeps waiting on the
        # same buffer instead of discarding a frame that hasn't finished.
        # `first_index` is shifted by the integrate-and-dump convolution's
        # sps-1 sample group delay. Buffer coordinates are based on the input
        # audio, so do not carry that delay into the consumed boundary. Tail
        # padding used to hide this over-consumption; adjacent unpadded
        # Adjacent unpadded waveforms make the exact boundary observable.
        result["end_index"] = i_star + sps * min(total_bits_needed, num_symbols)
    return result


def demodulate(audio, profile: Profile = PROFILE_300, sample_rate=SAMPLE_RATE, *,
               head_seconds=framing.HEAD_PAD_SECONDS, diagnostics=False):
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
    if audio.ndim != 1 or len(audio) < sps:
        return {"synced": False, "payload": None}

    diff = _tone_energy_diff(audio, sample_rate, sps, profile.freq0, profile.freq1)
    template = _sync_template(profile.baud, sps, sample_rate, profile.freq0, profile.freq1)
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
    credible_bits = max_credible_frame_bits(profile.baud)
    n_sync = len(framing.sync_bits(profile.baud))
    for i_star in peaks:
        result = _try_sync(diff, i_star, sps, float(ncc[i_star]), credible_bits,
                           n_sync, profile.baud, head_seconds)
        if result.get("payload") is not None:
            result["snr_db"] = _sync_snr_db(audio, i_star, profile, sample_rate)
            if not diagnostics:
                result.pop("_hard_bits", None)
            return result
        if "end_index" in result:
            if near_miss is None:
                near_miss = result
        elif still_arriving is None:
            still_arriving = result
    result = still_arriving or near_miss
    if not diagnostics:
        result.pop("_hard_bits", None)
    return result
