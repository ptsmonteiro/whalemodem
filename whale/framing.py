"""Shared framing constants used across the FM/HF waveform modes."""

# The length field's width, kept only to size MAX_PAYLOAD_BYTES: every
# shipped mode's own packet codec independently bounds how long its own
# frames can be, but link.py's packet layer still needs one shared ceiling
# on a payload it will accept.
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

# Settling time bought before a mode's own sync/preamble: the transmitter
# needs a stretch between PTT keying and being usably on air, and it also
# gives the receiver's audio AGC in-band tone to settle on. Shared across
# waveform families (see whale/modes/hr0.py, hc0.py, hc1w.py) rather than
# picked per mode, so every mode budgets the same leading-loss protection.
HEAD_SECONDS = 1.0
