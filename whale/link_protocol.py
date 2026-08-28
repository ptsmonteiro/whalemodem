"""Pure wire-format helpers for the Whalemodem link protocol.

This module owns byte-level packet identities and connection/header
serialization.  It deliberately has no transport, threading, or session
state, so protocol compatibility can be tested without constructing a
``Link``.  ``whale.link`` re-exports these names for backwards compatibility.
"""

from whale import afsk, framing


PT_CONNECT = 0x01
PT_CONNECT_ACK = 0x02
PT_DISC = 0x03
PT_DISC_ACK = 0x04
PT_DATA = 0x05
PT_DATA_ACK = 0x06
PT_FLOOR_REQ = 0x09
PT_FLOOR_GRANT = 0x0A
PT_TIMING_ACK = 0x0B
PT_TIMING_CONFIRM = 0x0C

CONTROL_PLANE_TYPES = frozenset({
    PT_CONNECT, PT_CONNECT_ACK, PT_DISC, PT_DISC_ACK, PT_DATA_ACK,
    PT_FLOOR_REQ, PT_FLOOR_GRANT, PT_TIMING_ACK, PT_TIMING_CONFIRM,
})
DATA_PLANE_TYPES = frozenset({PT_DATA})

AIR_HEADER_MAGIC = b"WH"
AIR_HEADER_VERSION = 2
AIR_HEADER_INLINE_BYTES = 2
AIR_HEADER_LEN = framing.BOOTSTRAP_HEADER_BYTES

CONNECT_FORMAT_MAGIC = b"\xffWHL"
CONNECT_FORMAT_VERSION = 4
SESSION_ID_NONE = 0

EOF_BIT = 0x80
SEQ_MASK = 0x7F
SEQ_MODULO = SEQ_MASK + 1

PTYPE_NAMES = {
    PT_CONNECT: "CONNECT", PT_CONNECT_ACK: "CONNECT_ACK",
    PT_DISC: "DISC", PT_DISC_ACK: "DISC_ACK",
    PT_DATA: "DATA", PT_DATA_ACK: "DATA_ACK",
    PT_FLOOR_REQ: "FLOOR_REQ", PT_FLOOR_GRANT: "FLOOR_GRANT",
    PT_TIMING_ACK: "TIMING_ACK", PT_TIMING_CONFIRM: "TIMING_CONFIRM",
}


def air_inline_length(ptype):
    if ptype in (PT_DATA, PT_CONNECT, PT_CONNECT_ACK, PT_DATA_ACK,
                 PT_TIMING_ACK, PT_TIMING_CONFIRM):
        return 2
    return 0


def encode_air_header(ptype: int, body_mode_id: int, body: bytes):
    inline_count = air_inline_length(ptype)
    inline = body[:inline_count]
    remainder = body[len(inline):]
    if len(remainder) > framing.MAX_PAYLOAD_BYTES:
        raise ValueError("packet body is too long")
    header = (AIR_HEADER_MAGIC + bytes([AIR_HEADER_VERSION, ptype, body_mode_id,
                                        len(inline)])
              + len(remainder).to_bytes(2, "big")
              + inline.ljust(AIR_HEADER_INLINE_BYTES, b"\x00"))
    assert len(header) == AIR_HEADER_LEN
    return header, remainder


def decode_air_header(raw: bytes):
    if len(raw) != AIR_HEADER_LEN or raw[:2] != AIR_HEADER_MAGIC:
        return None
    if raw[2] != AIR_HEADER_VERSION:
        return None
    inline_len = raw[5]
    if inline_len > AIR_HEADER_INLINE_BYTES:
        return None
    if any(raw[8 + inline_len:10]):
        return None
    return raw[3], raw[4], int.from_bytes(raw[6:8], "big"), raw[8:8 + inline_len]


def valid_air_shape(ptype, profile, body_len, inline, control_mode_id):
    """Apply semantic checks after the header CRC has passed."""
    if ptype == PT_DATA:
        return len(inline) == 2 and body_len <= profile.chunk_size
    if profile.mode_id != control_mode_id:
        return False
    if ptype in (PT_CONNECT, PT_CONNECT_ACK):
        return len(inline) == 2 and 0 < body_len <= 128
    if ptype == PT_DATA_ACK:
        return len(inline) == 2 and body_len == 2
    if ptype in (PT_TIMING_ACK, PT_TIMING_CONFIRM):
        return len(inline) == 2 and body_len == 0
    if ptype in (PT_DISC, PT_DISC_ACK, PT_FLOOR_REQ, PT_FLOOR_GRANT):
        return len(inline) == 0 and body_len == 0
    return False


def seq_ahead(a, b):
    return (a - b) % SEQ_MODULO


def ptype_name(ptype):
    return PTYPE_NAMES.get(ptype, f"0x{ptype:02x}")


def connection_envelope(content):
    return (CONNECT_FORMAT_MAGIC + bytes([CONNECT_FORMAT_VERSION])
            + len(content).to_bytes(2, "big") + content)


def decode_connection_envelope(payload):
    if (len(payload) < 7 or payload[:4] != CONNECT_FORMAT_MAGIC
            or payload[4] != CONNECT_FORMAT_VERSION
            or int.from_bytes(payload[5:7], "big") != len(payload) - 7):
        raise ValueError("invalid connection body envelope")
    return memoryview(payload)[7:]


def call_bytes(call):
    raw = call.encode("ascii")
    if (not 1 <= len(raw) <= 15
            or any(chr(c) not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in raw)):
        raise ValueError("invalid callsign")
    return bytes([len(raw)]) + raw


def take_call(content, offset):
    if offset >= len(content):
        raise ValueError("missing callsign")
    size = content[offset]
    end = offset + 1 + size
    if not 1 <= size <= 15 or end > len(content):
        raise ValueError("invalid callsign length")
    call = bytes(content[offset + 1:end]).decode("ascii")
    call_bytes(call)
    return call, end


def decode_call_pair(payload):
    try:
        content = decode_connection_envelope(payload)
        src, offset = take_call(content, 0)
        dst, _ = take_call(content, offset)
        return src, dst
    except (ValueError, UnicodeError):
        return "", ""


def encode_call_and_modes(a, b, supported_ids, extra_id,
                          session_id=SESSION_ID_NONE):
    modes = bytes(sorted(set(supported_ids)))
    content = (call_bytes(a) + call_bytes(b) + bytes([session_id, len(modes)])
               + modes + bytes([extra_id]))
    return connection_envelope(content)


def decode_call_and_modes(payload):
    content = decode_connection_envelope(payload)
    a, offset = take_call(content, 0)
    b, offset = take_call(content, offset)
    if offset + 3 > len(content):
        raise ValueError("truncated CONNECT")
    session_id, count = content[offset], content[offset + 1]
    offset += 2
    if offset + count + 1 != len(content):
        raise ValueError("invalid CONNECT mode count")
    modes = list(content[offset:offset + count])
    return a, b, modes, content[-1], session_id


def encode_connect_ack(a, b, supported_ids, accepted_tx_id, own_tx_id,
                       session_id=SESSION_ID_NONE):
    modes = bytes(sorted(set(supported_ids)))
    content = (call_bytes(a) + call_bytes(b) + bytes([session_id, len(modes)])
               + modes + bytes([accepted_tx_id, own_tx_id]))
    return connection_envelope(content)


def decode_connect_ack(payload):
    content = decode_connection_envelope(payload)
    a, offset = take_call(content, 0)
    b, offset = take_call(content, offset)
    if offset + 4 > len(content):
        raise ValueError("truncated CONNECT_ACK")
    session_id, count = content[offset], content[offset + 1]
    offset += 2
    if offset + count + 2 != len(content):
        raise ValueError("invalid CONNECT_ACK mode count")
    modes = list(content[offset:offset + count])
    offset += count
    return a, b, modes, content[offset], content[offset + 1], session_id


def negotiate_mode(own_supported_ids, proposed_id, fallback_id=None):
    if proposed_id in own_supported_ids:
        return proposed_id
    return afsk.CONTROL_PROFILE.mode_id if fallback_id is None else fallback_id
