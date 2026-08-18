"""Bit-level framing for the AFSK link: sync word, length+CRC16, bit packing.

Frame layout (bits, MSB first):
    SYNC_BITS (63 bits, PN sequence -- good autocorrelation for sync search)
    length    (16 bits, big endian, payload length in bytes, 0-65535)
    payload   (8 * length bits)
    crc16     (16 bits, over the length field + payload)
"""

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


# x^6 + x + 1, primitive -> period 2^6-1 = 63. A fixed, known-good PN word
# used purely as a correlation target for sync search.
SYNC_BITS = _lfsr_bits(63, 6, (1, 6), seed=1)

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


# Mirrors tail_pad_bits, but in front of SYNC_BITS. Content doesn't matter
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
# was margin rather than a measured floor. Swapping the HT produced one
# that does need it, and the failure is worth spelling out because nothing
# about it points at this constant: PROFILE_1200 stopped working in one
# direction while 300 and 600 kept running, which reads like a tone-placement
# or frame-size problem at the fast profile and is neither.
#
# What the new HT does is take ~110-130ms to settle after its squelch opens
# -- it slams into a clipping transient as the carrier arrives and its AGC
# overshoots, leaving the audio ~2x its own steady-state level and still
# moving. Aligning a real capture against the frame that was sent, by the
# frame body, puts the damage entirely in front:
#
#            head pad     sync word      bit errors
#   600      0-80ms       80-185ms       0 of 63 sync, 0 of 850 body
#   1200     0-80ms       80-132ms      14 of 63 sync, 1 of 868 body
#
# So the *body* arrives essentially perfect at 1200 baud and the frame is
# thrown away anyway, because 14-15 of the sync word's 63 symbols land
# inside the transient every time and hold the normalised correlation at
# 0.60-0.62 against afsk.CONFIDENCE_THRESHOLD's 0.7. A frame nobody can
# sync on is indistinguishable from one that never arrived.
#
# 1200 baud fails alone because the sync word is a fixed 63 *bits*, so its
# duration halves at every step up -- 210ms at 300 baud, 105 at 600, 52 at
# 1200. At 600 the sync word outlasts the transient and the tail of it
# still carries the correlation; at 1200 the whole thing fits inside. The
# profile with the least settling margin is the fastest one, which is the
# opposite of what a fixed pad in front of a fixed bit count implies.
#
# Measured on the weak leg (ic705->ht), 1200 baud, 100-byte payload:
#
#    80ms   1/5, 0/4, 0/8   confidence pinned at 0.6
#   150ms   5/5
#   200ms   7/8             one near-miss at 0.70
#   280ms   8/8             confidence 1.0
#   350ms   8/8             no better than 280
#
# 280ms is the knee and is what this is now. It is a floor, not margin, and
# it is the first value here derived from the worst radio rather than from
# the bench agreeing it was unnecessary -- a receiver slower to settle than
# this HT would push it up again. The same change takes 300 and 600 baud on
# that leg from 75% to 100%: the new HT degrades every profile, 1200 is
# just the one where it crosses the sync threshold.
#
# It costs 200ms of every keying, which under afsk.MAX_KEYING_SECONDS comes
# out of the payload -- chunk sizes drop ~9% (78->70, 170->155, 355->325).
# Buying it back belongs in the receiver, not here: the transient is an
# amplitude excursion, and afsk._tone_energy_diff normalises each tone
# branch over the whole buffer, so a gain that moves *within* the sync word
# still warps its shape against a flat-amplitude template. A per-window
# normalisation would let the pad shrink again.
HEAD_PAD_SECONDS = 0.28


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
    return head_pad_bits(baud) + SYNC_BITS + bytes_to_bits(body + crc_bytes) + tail_pad_bits(baud)


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
