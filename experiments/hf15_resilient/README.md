# HF5 resilient level-3 candidate

HF5 is based on the production HF4 single-carrier waveform: 8PSK at 1500
baud, the same 63-chip synchronizer and 15-symbol pilot blocks.  It adds a
terminated K=7 rate-1/2 convolutional code and a six-row block interleaver.
The fixed frame is sized to retain just over 2,000 bit/s net application
throughput while providing coding and burst-spreading margin for Watterson
fading.

Mode ID is 12 and the mode is experimental.  The target envelope is quiet to
slightly disturbed Watterson at 16 dB SNR/3 kHz; this target is not claimed
until the retained Monte Carlo result is complete.
