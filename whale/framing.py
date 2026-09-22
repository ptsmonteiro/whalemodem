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
# waveform family (see whale/phy/hr0.py, hc0.py, hc1w.py and
# whale/phy/ofdm49.py) rather than picked per mode, so every mode budgets the
# same leading-loss protection.
#
# Sized against the analogue ramp alone: the transmitter's PTT ramp plus
# the receiver's AGC attack, which together measure 30-60 ms where they can
# be read cleanly on the bench radios. Acquisition itself needs no head at
# all -- every HF receiver searches for the sync preamble and decodes a
# keying with no head in front of it -- so the only thing this number buys
# is analogue readiness, and every millisecond of it is air time. On-air
# A/B, IC-705 <-> IC-7300, 90 keyings over hf7/hf8/hc1w/hc0/hr0
# (logs/head/2026-09-21_shipped_ab.jsonl and _ba.jsonl): 89 of 90 decoded,
# and four of the five modes decoded 3/3 with no head at all; the one
# failure was an hf7 keying at head 0. 0.1 s is still twice the measured
# ramp, leaving margin for radios and amplifiers slower than the bench pair.
SETTLING_HEAD_SECONDS = 0.1

#: Configured settling head shared by every analog-FM waveform. The head
#: carries each mode's own modulation so the transmitter, receiver squelch,
#: and receive audio path settle before synchronization begins.
FM_SETTLING_HEAD_SECONDS = 0.6
