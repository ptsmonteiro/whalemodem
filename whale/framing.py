"""Bit-level framing for the AFSK link: sync word, length+CRC16, bit packing.

Frame layout (bits, MSB first):
    sync word (PN sequence -- good autocorrelation for sync search; its
              length depends on baud, see sync_bits)
    length    (16 bits, big endian, payload length in bytes, 0-65535)
    payload   (8 * length bits)
    crc16     (16 bits, over the length field + payload)
"""

import functools

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
# reason head_pad_bits and tail_pad_bits are: what a radio corrupts is a
# span of time, so a fixed bit count tuned at one baud silently shrinks at
# the next. The sync word was the one thing between those two pads that did
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
# afsk.MAX_KEYING_SECONDS leaves room for a 328-byte payload there, so ~0.5s
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
# What binds frame size now is afsk.MAX_KEYING_SECONDS, which is about how
# long the transmitter holds the channel rather than anything about the
# format. The field is deliberately far wider than that budget will ever
# allow, which is exactly why a declared length must not be taken at face
# value on receive -- see afsk.max_credible_frame_bits.
LENGTH_FIELD_BITS = 16
MAX_PAYLOAD_BYTES = (1 << LENGTH_FIELD_BITS) - 1

# Extra dummy bits appended after the CRC, purely as on-air padding. Real
# hardware corrupts the very end of a transmission -- our own 5ms amplitude
# ramp-down, plus a symbol or so of radio audio tail -- and since
# parse_frame_bits only reads the exact prefix it needs and ignores anything
# after, these throwaway bits eat that instead of the real CRC bits.
#
# This was 213ms, sized against a "tail corruption" that turned out to be a
# software bug, not the radios: audio_io.transmit()'s output callback used
# to raise CallbackStop on the last partial block, and PortAudio then tore
# the stream down with roughly one `latency` -- ~100ms -- of the signal
# still sitting in the device buffer, unplayed. Every transmission was
# silently truncated by that much, and 213ms of padding was what it took to
# keep the truncation off the CRC. With the zero-fill fix in transmit(), a
# bench measurement (modulate a 500ms alternating tail pad, count how many
# of its bits decode at the far end) shows 598-600 of 600 bits surviving in
# both directions even when PTT drops the instant the audio ends -- i.e.
# the genuine on-air tail costs 1-2 bits, ~1ms. 30ms is that plus the 5ms
# ramp plus an order of magnitude of margin.
#
# Still expressed as a duration rather than a bit count: what the radio
# corrupts is a span of time, so a fixed bit count tuned at one baud
# silently shrinks at the next. See scripts/sweep_ptt_timing.py.
TAIL_PAD_SECONDS = 0.03


def tail_pad_bits(baud):
    n = round(TAIL_PAD_SECONDS * baud)
    return [i % 2 for i in range(n)]


# Mirrors tail_pad_bits, but in front of the sync word. Content doesn't matter
# (thrown away, never parsed); what it buys is settling time, scaled to
# duration rather than bit count so it doesn't shrink as baud rises.
#
# Note this is *not* the whole leading allowance, and not the expensive
# part of it: the transmitter needs a few hundred ms between PTT keying and
# being usably on air, and that is bought by transport.PTT_LEAD (see its
# comment for the measurement). By the time these bits go out the radio is
# already up. Their job is the last stretch before the sync word -- give
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
HEAD_PAD_SECONDS = 0.15


def head_pad_bits(baud):
    n = round(HEAD_PAD_SECONDS * baud)
    return [i % 2 for i in range(n)]


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


def build_frame_bits(payload: bytes, baud=300):
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload too long ({len(payload)} > {MAX_PAYLOAD_BYTES})")
    body = len(payload).to_bytes(LENGTH_FIELD_BITS // 8, "big") + payload
    crc = crc16_ccitt_false(body)
    crc_bytes = bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    return (head_pad_bits(baud) + sync_bits(baud)
            + bytes_to_bits(body + crc_bytes) + tail_pad_bits(baud))


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
    """Bits from the start of the length field to the end of the CRC, for a
    frame declaring `length` bytes of payload."""
    return LENGTH_FIELD_BITS + 8 * length + 16


def parse_frame_bits(bits_after_sync):
    """bits_after_sync may be longer than one frame. Returns payload bytes if
    CRC checks out, else None."""
    length = declared_length(bits_after_sync)
    if length is None:
        return None
    total_bits = frame_bits_for_length(length)
    if len(bits_after_sync) < total_bits:
        return None
    body_bits = LENGTH_FIELD_BITS + 8 * length
    body = bits_to_bytes(bits_after_sync[0:body_bits])
    crc_bytes = bits_to_bytes(bits_after_sync[body_bits:total_bits])
    crc_received = (crc_bytes[0] << 8) | crc_bytes[1]
    if crc16_ccitt_false(body) != crc_received:
        return None
    return body[LENGTH_FIELD_BITS // 8:]
