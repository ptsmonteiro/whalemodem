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
# hardware (radio audio tail / squelch release / our own ramp-down) reliably
# corrupts the last symbol or two of a transmission -- observed on this rig
# as a wrong final byte even on short frames. Since parse_frame_bits only
# reads the exact prefix it needs and ignores anything after, tacking these
# throwaway bits onto the end means it's the padding that eats the tail
# corruption instead of the real CRC/payload bits.
TAIL_PAD_BITS = [0, 1] * 32  # ~213ms at 300 baud


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


def build_frame_bits(payload: bytes):
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload too long ({len(payload)} > {MAX_PAYLOAD_BYTES})")
    body = bytes([len(payload)]) + payload
    crc = crc16_ccitt_false(body)
    crc_bytes = bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    return SYNC_BITS + bytes_to_bits(body + crc_bytes) + TAIL_PAD_BITS


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
