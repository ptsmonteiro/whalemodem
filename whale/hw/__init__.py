"""This bench's hardware: sound cards and PTT.

The mechanism is in audio_io.py (WASAPI device lookup, simultaneous
play/record) and ptt.py (CI-V and RTS keying). Which of them belongs to
which radio is the one bench-specific fact in the repo, and it lives in
radios.py alone -- every experiment selects a radio by name from RADIOS
there rather than naming a device or a COM port itself.

Copied from radiomodem's shark/hw/ (unchanged) so whalemodem has no
dependency on a sibling checkout.
"""
