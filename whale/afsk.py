"""Shared audio constants: device sample rates and keying-length figures."""

from whale import rx_audio

SAMPLE_RATE = 48000
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE

# Sync-through-CRC audio budget a keying is meant to stay under. Recorded
# here rather than in whale/policy.py because it long predates the policy
# module and other code still refers to it as afsk.MAX_USEFUL_FRAME_SECONDS
# -- see whale/policy.py's FM.max_useful_frame_seconds and the reasoning
# there (retransmit granularity, half-duplex responsiveness, clock
# tolerance). No shipped FM mode currently sizes its payload from this
# budget: the VF waveforms are fixed-geometry.
MAX_USEFUL_FRAME_SECONDS = 3.0

# Dead air inside a keying that isn't frame bits, PTT key-down to PTT
# release: the measured output-stream startup/fill time. whale/transport.py
# asserts at import that it still agrees with this, which is where it is
# measured and documented.
KEYING_OVERHEAD_SECONDS = 0.36
