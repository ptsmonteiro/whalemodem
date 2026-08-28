# qpsk29 on-air results

Measured 2026-08-28 on 10.145 MHz USB-D, transmitting from the IC-7300 to the
IC-705. Trials bypassed the link layer: each was one random full-capacity
payload, one modulation and keying, one capture, and one demodulation. Success
means byte-for-byte equality with no retry. Peak audio was 0.9 with a 9 dB
software PAPR target.

## Result

| Profile | Payload | Net over 5.200 s | Result |
| --- | ---: | ---: | ---: |
| LDPC 2/3, balanced pilots | 914 B | 1,406.2 bit/s | 1/1 |
| LDPC 3/4 gate | 1,028 B | 1,581.5 bit/s | 1/1 |
| LDPC 3/4 confirmation | 1,028 B | 1,581.5 bit/s | 3/3 |

LDPC 3/4 is therefore the fastest coded qpsk29 profile confirmed on this
path: **4/4 total, 4,112 bytes delivered without error**. The three
confirmation captures measured:

- header confidence 0.9935–0.9943;
- carrier offset -8.09 to -8.22 Hz, estimated and removed;
- median per-carrier header SNR 18.8–19.4 dB;
- raw BER 2.16–2.30%; and
- all 17 LDPC codewords converged in at most four iterations.

The machine-readable records are `results/sweeps/gate_ldpc34.json` and
`results/sweeps/confirm_ldpc34.json`. Received audio and matching payloads are
kept locally under `results/captures/` and ignored by Git.

## What the first capture changed

The first LDPC-2/3 frame failed: 14/17 codewords converged. It was still a
useful capture because synchronization (0.9935) and frequency correction
(-8.08 Hz) had already worked. The failure separated into two measured
problems:

1. The IC-705 receive path sharply removed the top of this unusually wide
   frame. Header SNR was 10.7 dB at 2906 Hz, -0.7 dB at 3000 Hz, and -14.9 dB
   at 3094 Hz. Those last two carriers contained 290 of the frame's 353 raw
   bit errors. Per-carrier LLR weighting had a 0.25 floor, so almost-absent
   carriers still asserted confident random values. The floor is now 0.01,
   presenting them to LDPC as erasures.
2. Pilot locations came from a deterministic random permutation, but random
   did not mean balanced. One otherwise-healthy carrier received no pilot
   after payload symbol 70; over the remaining three seconds its phase moved
   1.6 radians. Pilots now occupy an even time/frequency lattice, and tests
   assert coverage in both the first and last quarter on every carrier.

With both changes, the next LDPC-2/3 frame passed with 2.13% raw BER and every
codeword converged in four iterations or fewer. Raising the code rate to 3/4
then passed the gate and all three confirmation trials.

## Limits of the claim

This is a strong-path development result, deliberately using the direction
the request selected. It does not establish a bidirectional mode or a weak-HF
fallback. The uncoded 2,212 bit/s diagnostic profile was not promoted or
tested as an operating mode: two erased carriers guarantee raw errors, so a
CRC-only frame is not a credible candidate on the measured passband.

qpsk29 also occupies 468.75–3093.75 Hz (2625 Hz between outer carrier centres,
or 2718.75 Hz including one carrier-bin width) and keys for 5.200 s. Both exceed the production targets currently
documented for Whalemodem: a 2300 Hz standard HF channel and a 3.0 s keying
cap. The confirmation runner recorded 3.13–3.23 s decode time; narrowing the
header timing search afterward reduced replay of all five passing captures to
1.77–1.82 s on this development PC. It still needs profiling on a
representative low-end target before it can satisfy that project goal.
For those reasons it remains under `experiments/` and is not added to the
negotiated `hf-ssb` ladder.
