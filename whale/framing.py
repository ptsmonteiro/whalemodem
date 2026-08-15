"""Bit-level framing for the AFSK link: sync word, length+CRC16, bit packing.

Frame layout (bits, MSB first):
    SYNC_BITS (63 bits, PN sequence -- good autocorrelation for sync search)
    length    (8 bits, payload length in bytes, 0-255)
    payload   (8 * length bits)
    crc16     (16 bits, over length_byte + payload)
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

MAX_PAYLOAD_BYTES = 255

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
# real correlation energy. 80ms is comfortably more than both need; the
# bench sweep decoded 100% at every value tested from 0 up, so this is
# margin rather than a measured floor.
HEAD_PAD_SECONDS = 0.08


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
    body = bytes([len(payload)]) + payload
    crc = crc16_ccitt_false(body)
    crc_bytes = bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    return head_pad_bits(baud) + SYNC_BITS + bytes_to_bits(body + crc_bytes) + tail_pad_bits(baud)


def parse_frame_bits(bits_after_sync):
    """bits_after_sync may be longer than one frame. Returns payload bytes if
    CRC checks out, else None."""
    if len(bits_after_sync) < 8:
        return None
    length = bits_to_bytes(bits_after_sync[0:8])[0]
    total_bits = 8 + 8 * length + 16
    if len(bits_after_sync) < total_bits:
        return None
    body = bits_to_bytes(bits_after_sync[0:8 + 8 * length])
    crc_bytes = bits_to_bytes(bits_after_sync[8 + 8 * length:total_bits])
    crc_received = (crc_bytes[0] << 8) | crc_bytes[1]
    if crc16_ccitt_false(body) != crc_received:
        return None
    return body[1:]
