"""Bit-level framing for the AFSK link: sync, checked header/body, bit packing.

Frame layout (bits, MSB first):
    head pad  (sync-anchored PN suffix -- leading-loss protection and measurement)
    sync word (PN sequence -- good autocorrelation for sync search; its
              length depends on baud, see sync_bits)
    length    (16 bits, big endian, complete payload length)
    header    (the first 10 payload bytes)
    header crc16 (over length + header)
    body      (the remaining payload bytes, if any)
    body crc16 (over body, present only when the body is non-empty)
"""

import functools
import math

CRC16_POLY = 0x1021
CRC16_INIT = 0xFFFF


def crc16_ccitt_false(data: bytes) -> int:
    crc = CRC16_INIT
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ CRC16_POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _lfsr_bits(num_bits, order, taps, seed=1):
    state = seed & ((1 << order) - 1) or 1
    mask = (1 << order) - 1
    bits = []
    for _ in range(num_bits):
        bits.append(state & 1)
        fb = 0
        for t in taps:
            fb ^= (state >> (t - 1)) & 1
        state = ((state >> 1) | (fb << (order - 1))) & mask
    return bits


# The sync word, as a *duration* rather than a bit count -- for exactly the
# reason head_pad_bits is: what a radio corrupts is a
# span of time, so a fixed bit count tuned at one baud silently shrinks at
# the next. The sync word after the head pad did
# not follow the rule, and it is what a receiver has to survive the start of
# a transmission with.
#
# It was a fixed 63-bit PN word, i.e. 210ms at 300 baud but only 52ms at
# 1200. So the profile with the least margin against a fixed-duration
# impairment was always the fastest one -- the opposite of what you want,
# since the fast profile is also the one carrying the most payload behind
# that sync word. That is not a hypothetical: swapping the bench HT for one
# that blacks out for ~110ms after its squelch opens killed PROFILE_1200
# outright while 300 and 600 baud kept working, because at 1200 the whole
# sync word fitted inside the blackout. See HEAD_PAD_SECONDS.
#
# Every profile now gets SYNC_SECONDS of sync word, so a blackout costs each
# one the same *fraction* of its sync word rather than a fraction that
# doubles with baud. An m-sequence has period 2^order - 1, which doubles per
# order, and baud doubles per profile -- so one order per profile lands
# within 1.2% of a constant duration the whole way up:
#
#     300 baud   order 6    63 bits   210.0ms
#     600 baud   order 7   127 bits   211.7ms
#    1200 baud   order 8   255 bits   212.5ms
#    2400 baud   order 9   511 bits   212.9ms
#
# 0.21s is 300 baud's existing 63 bits, so the control profile's sync word
# is bit-for-bit what it always was and only the faster ones lengthen.
#
# ON-AIR FORMAT CHANGE, on the same footing as LENGTH_FIELD_BITS: a station
# built before this does not share a sync word with one built after at 600
# or 1200 baud, and since the sync word is what a receiver locks on, the two
# do not interoperate at those speeds at all. 300 baud is unchanged, and
# because that is CONTROL_PROFILE, a mismatched pair still fails the way an
# out-of-range station does -- no CONNECT_ACK -- rather than by half-opening
# a session it cannot carry data on.
SYNC_SECONDS = 0.21

# Primitive over GF(2) under _lfsr_bits' tap convention, so each gives the
# full 2^order - 1 period. Verified by construction rather than trusted from
# a table: test_sync_words_are_full_period_m_sequences checks the period and
# the autocorrelation that makes these usable as a correlation target.
_SYNC_TAPS = {6: (1, 6), 7: (1, 7), 8: (1, 3, 4, 8), 9: (1, 3, 5, 9)}


@functools.lru_cache(maxsize=None)
def sync_bits(baud):
    """The sync word for `baud`: the m-sequence whose period comes closest
    to SYNC_SECONDS at this baud.

    Cached because demodulate() rebuilds its correlation template on every
    poll of the RX buffer, several times a second per profile."""
    target = SYNC_SECONDS * baud
    order = min(_SYNC_TAPS, key=lambda o: abs(((1 << o) - 1) - target))
    return _lfsr_bits((1 << order) - 1, order, _SYNC_TAPS[order], seed=1)

# The length field, and everything it can express.
#
# This was 8 bits / 255 bytes, which was the binding constraint at 1200 baud:
# the useful-frame duration budget leaves room for a larger payload there, so
# of every keying went unspent purely because the field could not describe
# it. 16 bits spends that, and leaves headroom for whatever faster profile
# turns up -- it covers a full keying up to roughly 230 kbaud, which is far
# past anything a 3 kHz audio channel will ever carry.
#
# ON-AIR FORMAT CHANGE: stations built either side of this do not
# interoperate, and unlike the mode negotiation there is no way to straddle
# it -- the length field has to be read before the frame that would say who
# is transmitting.
#
# What binds frame size now is afsk.MAX_USEFUL_FRAME_SECONDS. The field is
# deliberately far wider than that budget will ever
# allow, which is exactly why a declared length must not be taken at face
# value on receive -- see afsk.max_credible_frame_bits.
LENGTH_FIELD_BITS = 16
MAX_PAYLOAD_BYTES = (1 << LENGTH_FIELD_BITS) - 1

# Fixed decoded size of the checked header carried after the one sync in every
# link-layer keying. Its fields are specified in FRAMING.md and encoded
# by whale.link. Keeping the size here lets airtime budgeting remain in the
# physical layer without importing the link protocol.
AIR_HEADER_BYTES = 10
# Compatibility alias for code outside the package. This is no longer a
# separately modulated bootstrap frame.
BOOTSTRAP_HEADER_BYTES = AIR_HEADER_BYTES

# The outer head timing pad is both measurement and protection, so it needs
# deterministic, alignment-safe content. Its order-15 maximal-length sequence
# has a period comfortably longer than every supported pad.
_PAD_LFSR_ORDER = 15
_PAD_LFSR_SEED = 0x5A5A
_HEAD_PAD_TAPS = (1, 15)


# The head pad buys settling time, scaled to
# duration rather than bit count so it doesn't shrink as baud rises.
#
# Note this is *not* the whole leading allowance, and not the expensive
# part of it: the transmitter needs a few hundred ms between PTT keying and
# being usably on air. These symbols now buy that entire interval: audio-stream
# setup begins immediately after PTT assertion, with no separate lead sleep.
# Their job before the sync word is also to give
# the receiver's audio AGC in-band tone to settle on, and keep modulate()'s
# 5ms amplitude ramp-in off the front of the sync word, where it would eat
# real correlation energy.
#
# This was 80ms, on a sweep that decoded 100% at every value from 0 up --
# i.e. no receiver on the bench at the time needed any of it, so the figure
# was margin rather than a measured floor. Swapping the HT produced one that
# does need it, and it is worth knowing what that failure looked like,
# because nothing about it pointed here: PROFILE_1200 stopped working in one
# direction while 300 and 600 kept running, which reads like a tone-placement
# or frame-size problem at the fast profile and is neither.
#
# What that HT does is black out for ~110ms after its squelch opens. Not
# attenuate -- black out.
#
# That HT is a Baofeng UV-B5, and it is no longer on the bench: it went back
# to the Wouxun KG-UV9D Plus the same day, once a failing battery in the
# Baofeng made it useless as a reference. So the figures below describe a
# radio the bench can no longer reproduce, which is the main reason they are
# written down in this much detail. Do not "re-measure" this constant on the
# current bench and conclude it is oversized -- the Wouxun does not need it,
# and did not need it before the swap either.
#
# A UV-B5 is about as minimal as an HT gets, which makes it a fair stand-in
# for the worst radio likely to be plugged into this modem. That is the
# spirit of the figure: not the worst case that exists, but a cheap radio
# rather than a mid-range one. See whale/hw/radios.py. Measured on a real capture by comparing energy at
# the profile's own tones against energy at 3000 Hz, where nothing is ever
# transmitted: for the first 110ms the two are equal to within 3 dB, i.e.
# what arrives is broadband transient with no tone in it at all, and at
# +110ms the in-band figure jumps to +14 dB and stays there. The carrier
# hitting the squelch produces a clipping transient (the capture pegs at
# 1.000) and the receiver's AGC spends that long recovering from it.
#
# Two things follow, and the second is the one that took a wrong turn first.
#
# The signal is absent, not distorted, so no receiver-side cleverness
# recovers it. An earlier version of this comment proposed normalising
# afsk._tone_energy_diff per-window instead of over the whole buffer, on the
# theory that the transient was an amplitude excursion warping the
# correlation. It is not, and that would not have helped: there is nothing
# under the transient to normalise. Anything that reaches the sync word
# before the receiver has settled is simply lost, and the only defences are
# to start the sync word later or to need less of it.
#
# The other is that the sync word is where the loss lands. Aligning a real
# capture against the frame that was sent, by the frame body:
#
#            head pad     sync word      bit errors
#   600      0-80ms       80-185ms       0 of 63 sync, 0 of 850 body
#   1200     0-80ms       80-132ms      14 of 63 sync, 1 of 868 body
#
# The 1200-baud *body* arrives essentially perfect and the frame is thrown
# away anyway, because 14-15 of its 63 sync symbols land inside the blackout
# every time and hold the correlation at 0.60-0.62 against
# afsk.CONFIDENCE_THRESHOLD's 0.7. A frame nobody can sync on is
# indistinguishable from one that never arrived. And 1200 baud was alone in
# failing because its sync word was the shortest in *time* -- see SYNC_SECONDS
# above, which is the structural half of this fix and the reason a blackout
# now costs every profile the same fraction of its sync word rather than a
# fraction that doubles with baud.
#
# So there are two allowances against a blackout, and they buy different
# things per millisecond of air time:
#
#   - this pad, 1:1 -- every ms of it is a ms of blackout the sync word
#     never sees;
#   - the sync word's length, at about 0.4:1 -- a lock survives losing
#     roughly the first 40% of the sync word (identical at all three
#     profiles, and holding to 40% while breaking at 50%; see
#     test_a_lock_survives_losing_the_opening_of_the_sync_word).
#
# Total blackout tolerance is therefore about HEAD_PAD_SECONDS + 0.4 *
# SYNC_SECONDS, or pad + ~85ms. Sizing this is picking how much margin to
# hold over the worst receiver you expect; the sync word covers the rest and
# costs the same at every profile.
#
# Measured on the weak leg (ic705->ht), 1200 baud, 100-byte payload, with
# the duration-scaled sync word in place:
#
#    20ms   1/8            blackout eats ~42% of the sync word
#    50ms   8/8  conf 0.8
#    80ms   8/8  conf 0.9
#   150ms   8/8  conf 0.9
#   280ms   8/8  conf 1.0
#
# 50ms is the knee on this radio and 150ms is what ships: ~235ms of total
# tolerance against the 110ms this HT actually needs, so a receiver twice as
# slow as the worst one seen still works. Before the sync word was scaled,
# the same sweep needed 280ms to reach 8/8 and failed outright at 80 --
# the 130ms saved is what the structural fix bought back, and it is spent
# here on margin rather than returned to the payload.
#
# Note this pad is also the cheaper allowance to grow later: it is the one
# that does not have to be paid at every profile equally, since a slow
# receiver is a fixed number of ms regardless of what baud is running.
HEAD_PAD_SECONDS = 1.0


@functools.lru_cache(maxsize=16)
def _head_pad_anchor_bits(num_bits):
    """Return the immutable PN prefix whose end is anchored beside sync."""
    return tuple(_lfsr_bits(num_bits, _PAD_LFSR_ORDER, _HEAD_PAD_TAPS,
                            seed=_PAD_LFSR_SEED))


def head_pad_bits(baud, seconds=HEAD_PAD_SECONDS):
    n = math.ceil(seconds * baud)
    anchor_n = math.ceil(HEAD_PAD_SECONDS * baud)
    if n < 0 or n > anchor_n:
        raise ValueError(
            f"head duration must be between 0 and {HEAD_PAD_SECONDS:g} seconds")
    if n == 0:
        return []
    # Every operational duration is a suffix of the protocol-fixed
    # calibration head. The symbol touching sync therefore stays at the same
    # PN phase when feedback changes the duration, so a receiver measuring
    # with its earlier expectation still compares the correct symbols.
    return list(_head_pad_anchor_bits(anchor_n)[-n:])


def bytes_to_bits(data: bytes):
    bits = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits


def bits_to_bytes(bits) -> bytes:
    assert len(bits) % 8 == 0
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return bytes(out)


def build_frame_bits(payload: bytes, baud=300, *, include_head=True,
                     head_seconds=HEAD_PAD_SECONDS):
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload too long ({len(payload)} > {MAX_PAYLOAD_BYTES})")
    length = len(payload).to_bytes(LENGTH_FIELD_BITS // 8, "big")
    # Retain the generic codec's small-payload form for physical-layer tests
    # and non-link users. Link packets are always at least AIR_HEADER_BYTES.
    if len(payload) < AIR_HEADER_BYTES:
        checked = length + payload
        checked += crc16_ccitt_false(checked).to_bytes(2, "big")
        return ((head_pad_bits(baud, head_seconds) if include_head else []) + sync_bits(baud)
                + bytes_to_bits(checked))
    header, body = payload[:AIR_HEADER_BYTES], payload[AIR_HEADER_BYTES:]
    header_crc = crc16_ccitt_false(length + header).to_bytes(2, "big")
    checked = length + header + header_crc
    if body:
        checked += body + crc16_ccitt_false(body).to_bytes(2, "big")
    return ((head_pad_bits(baud, head_seconds) if include_head else []) + sync_bits(baud)
            + bytes_to_bits(checked))


def declared_length(bits_after_sync):
    """The payload length this frame *claims*, or None if the length field
    hasn't fully arrived yet.

    Untrusted by construction, and the reason that matters is the ordering:
    the CRC that would validate this number sits *after* the payload it
    describes, so nothing can check it until that many bits have been
    buffered. A receiver therefore has to decide whether the claim is
    credible before it can afford to wait for it -- a false sync on noise
    yields a uniformly random 16-bit length, and honouring one of those
    means waiting for a frame that will never complete. See
    afsk.max_credible_frame_bits, which is where that judgement is made.
    """
    if len(bits_after_sync) < LENGTH_FIELD_BITS:
        return None
    field = bits_to_bytes(bits_after_sync[0:LENGTH_FIELD_BITS])
    return int.from_bytes(field, "big")


def frame_bits_for_length(length):
    """Bits from the length field through the final applicable CRC."""
    if length < AIR_HEADER_BYTES:
        return LENGTH_FIELD_BITS + 8 * length + 16
    body_len = length - AIR_HEADER_BYTES
    return (LENGTH_FIELD_BITS + 8 * AIR_HEADER_BYTES + 16
            + 8 * body_len + (16 if body_len else 0))


def header_is_valid(bits_after_sync):
    """Whether a complete fixed header is present and passes its CRC."""
    length = declared_length(bits_after_sync)
    if length is None:
        return None
    if length < AIR_HEADER_BYTES:
        small_needed = frame_bits_for_length(length)
        if len(bits_after_sync) < small_needed:
            return None
        checked_end = LENGTH_FIELD_BITS + 8 * length
        checked = bits_to_bytes(bits_after_sync[:checked_end])
        received = int.from_bytes(
            bits_to_bytes(bits_after_sync[checked_end:small_needed]), "big")
        return crc16_ccitt_false(checked) == received
    prefix_bits = LENGTH_FIELD_BITS + 8 * AIR_HEADER_BYTES
    needed = prefix_bits + 16
    if len(bits_after_sync) < needed:
        return None
    prefix = bits_to_bytes(bits_after_sync[:prefix_bits])
    received = int.from_bytes(bits_to_bytes(bits_after_sync[prefix_bits:needed]), "big")
    return crc16_ccitt_false(prefix) == received


def parse_frame_bits(bits_after_sync):
    """bits_after_sync may be longer than one frame. Returns payload bytes if
    CRC checks out, else None."""
    length = declared_length(bits_after_sync)
    if length is None:
        return None
    total_bits = frame_bits_for_length(length)
    if len(bits_after_sync) < total_bits:
        return None
    if length < AIR_HEADER_BYTES:
        checked_end = LENGTH_FIELD_BITS + 8 * length
        checked = bits_to_bytes(bits_after_sync[:checked_end])
        received = int.from_bytes(bits_to_bytes(bits_after_sync[checked_end:total_bits]), "big")
        if crc16_ccitt_false(checked) != received:
            return None
        return checked[LENGTH_FIELD_BITS // 8:]
    if header_is_valid(bits_after_sync) is not True:
        return None
    header_start = LENGTH_FIELD_BITS
    header_end = header_start + 8 * AIR_HEADER_BYTES
    header = bits_to_bytes(bits_after_sync[header_start:header_end])
    body_len = length - AIR_HEADER_BYTES
    if not body_len:
        return header
    body_start = header_end + 16
    body_end = body_start + 8 * body_len
    body = bits_to_bytes(bits_after_sync[body_start:body_end])
    received = int.from_bytes(bits_to_bytes(bits_after_sync[body_end:body_end + 16]), "big")
    if crc16_ccitt_false(body) != received:
        return None
    return header + body
