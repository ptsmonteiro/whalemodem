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

# The settling head: a stretch of the mode's own modulation prepended to
# every keying, in front of that mode's sync preamble. It is not correlated
# against and carries nothing -- it exists so the transmitter's PTT ramp and
# the receiver's audio AGC both have in-band signal to settle on before the
# sync preamble the acquisition actually needs. Shared across every HF
# waveform family (see whale/modes/hr0.py, hc0.py, hc1w.py and
# whale/phy/ofdm49.py) rather than picked per mode, so every mode budgets the
# same leading-loss protection.
#
# 0.6 s is the minimum that covers the measured PTT-and-AGC ramps on the
# bench radios (208 ms and 560 ms in the two directions) with a little
# margin, without spending more air time than that costs.
SETTLING_HEAD_SECONDS = 0.6
