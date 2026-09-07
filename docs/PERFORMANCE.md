# Performance and engineering history

This document keeps measurements and lessons useful to future modem work but
too detailed for the repository README. Figures describe the named setup; they
are not promises about every radio path.

## Why turnaround dominates

On a half-duplex link, useful bitrate is only one component of transfer time.
An early 100-byte exchange at 1200 baud took 3.91 seconds on the VHF bench:

| Component | Time |
| --- | ---: |
| Fixed turnaround sleeps | 2.00 s |
| PTT lead and sound-card startup | 0.85 s |
| Payload bits on air | 0.67 s |

Only 17% was payload airtime. Chunks are now sized from a keying budget, which
amortizes fixed per-keying costs by filling the keying. The link remains
stop-and-wait: one DATA chunk is acknowledged before the next is sent.

Two retained changes reduce avoidable delay:

- Reply timing is anchored to where the peer's checked frame ended in the RX
  buffer, so decoder work is absorbed by turnaround rather than added to it.
- The decoder discards audio it has already searched, keeping receive polling
  bounded instead of allowing work to grow with preceding idle audio.

Current timing and head calibration are specified in
[ADAPTIVE_TIMING.md](../ADAPTIVE_TIMING.md). Historical constants should not
be copied back into the implementation without a new measurement.

## The burst experiment

A go-back-N-style attempt put several DATA frames in one keying under a
cumulative ACK. It was rolled back after hardware exposed failures that
loopback tests did not show:

- Separately modulated frames placed a fade to silence at every join.
- Peak-over-median sync confidence depended on buffer composition and
  false-synced on off-air captures.
- An ACK requesting the sender's current base was mistaken for a stale ACK,
  producing full-timeout stalls.
- Replies ignored audio after the final CRC and often keyed over the peer's
  remaining transmission.

After those fixes, the IC-705 → handheld direction still recovered exactly
one frame from most two-frame bursts: the second frame found sync but failed
CRC, while the reverse direction carried the same bursts. The cause was not
understood. Any renewed burst work should reproduce this asymmetry before
changing the protocol.

Work retained independently of bursting includes normalized correlation,
earliest-frame-wins sync search, turnaround anchoring, session-scoped sequence
numbers, and an unambiguous DATA_ACK carrying both the answered sequence and
the sequence wanted next.

## VHF waveform progression

Candidate experiments used the same bidirectional direct-frame method. The
historical ranking was:

| Mode | Net user bit/s | On-air evidence |
| --- | ---: | --- |
| Shipped CPFSK 1200 | 947 | Acceptance runs |
| MFSK `4fsk_650bd` | 1,011 | 45/45 each direction |
| OFDM BPSK | 915 | 28/28 |
| VF3 58-carrier DQPSK | ~2,200 | 6/6 each direction |
| VF4 star-8-QAM + RS | ~2,945 | 6/6 each direction |
| VF5 16-QAM + pilots + RS | ~3,663 | 6/6 each direction |
| VF6 256-QAM + pilots + RS-only | 15,720 frame-useful | full-capacity `flat_nbfm`: 0/20 at 30 dB, 20/20 at 35 and 40 dB RF C/N; experimental |

The evidence is retained in each `experiments/*/RESULTS.md`. VF3 was the first
candidate admitted through the generic `WaveformMode` boundary and carried
complete acceptance sessions without changing ARQ or connection management.

Paired-audio full-stack measurements showed:

| Bytes each way | CPFSK ladder | With VF3 | Gain | Net application rate |
| ---: | ---: | ---: | ---: | ---: |
| 4,000 | 93.9 s | 66.6 s | 1.41× | 681 → 960 bit/s |
| 16,000 | 315.7 s | 161.0 s | 1.96× | 811 → 1,590 bit/s |

The gain is below frame arithmetic because climbing through robust modes is a
fixed cost and every fast DATA keying still receives a robust control-mode
ACK. These observations motivate mode history and a faster proven-peer ACK;
they do not claim that the latter is implemented.

## HF mode split

CPFSK assumes audio frequencies are reproduced accurately and has no carrier
offset estimator, making it a poor SSB fallback. HC1 introduced
offset-corrected differential-QPSK OFDM and works on the strong HF bench
direction. It failed on the weak direction because its acquisition confidence
imposes an effective SNR floor.

HC0 was therefore built as the first robust control rung: non-coherent orthogonal
16-FSK with a known-pattern detector and FEC. At equal transmitted RMS in
band-limited white noise, the recorded comparison was:

| Mode | Decode limit |
| --- | ---: |
| HC1 as actually acquired | +3.5 dB |
| HC0 | -16 dB |

HC0 also has a lower crest factor, allowing greater average power through a
peak-limited transmitter. HC1 remains the fast rung when the path supports it;
HR0 is the control mode below HC0. On 2026-09-06 the owner explicitly selected
the faster 32-FSK MARGIN32 geometry for production, overriding open
qualification gates. Its soft K=9 waveform uses a 1.812-second short frame
for up to 12 waveform bytes (including DATA_ACK), and a 3.860-second full
frame for up to 42 bytes. The short ACK uses 48.3% less airtime than the preceding
3.508-second 128-FSK HR0; full DATA carries 32 application bytes at 66.3 bit/s
before ARQ overhead. These durations include the minimum common lead and
tail, not radio turnaround or retries. Mode ID 10 and its common-lead
signature remain; the changed waveform requires both endpoints to update.
There is no negotiated legacy 128-FSK transmit/decode option.

The 2026-09-06 control requirement prioritizes short ACK airtime with a 3 dB
sensitivity advantage over Level-1 DATA at equal packet-delivery reliability.
Its nominal quiet/moderate and disturbed targets are +6 and +11 dB SNR/3 kHz.
See [SPEED_LADDERS.md](../SPEED_LADDERS.md) for the distinction between those
target values and a measured advantage over HC0, the current next data mode.
Production selection does not establish that margin or complete waveform
qualification.

The [fast-control experiment](../experiments/hr0_fast_control/RESULTS.md)
retains the former 128-FSK HR0 baseline and the 1.215 s, 1.396 s, and selected
1.812 s candidates. At -5 dB moderate/disturbed, the selected geometry
returned 280/300 and 277/300 ACKs, with 95% Wilson lower bounds of 89.93%
and 88.76%; HC0 DATA at -2 dB returned 286/300 and 283/300, clearing the 90%
confidence floor. These measured-boundary comparisons did not establish a
qualified 3 dB margin. They do not establish nominal +6/+11 dB envelope
failure or statistical inferiority. The owner elected to ship the faster
waveform with those evidence gaps open.

Clean paired-audio sessions passed for all candidates. The selected geometry
used 66.656 seconds of summed transmitted audio versus 143.776 seconds for
the old HR0 in a connect/128-byte-each-direction/disconnect exercise. That
gain includes full DATA geometry and adaptive lead as well as ACKs; it is
not elapsed session latency or faded-channel retry performance. Historical
+4 dB short-frame smoke checks refer to the old 128-FSK implementation.
See [FRAMING.md](../FRAMING.md) for current geometry and compatibility, and
[MODE_QUALIFICATION.md](../MODE_QUALIFICATION.md) for the explicit product
override and remaining gates.

A 2026-08-30 qualification campaign
(`logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-30/INDEX.md`) found HC1's
disturbed-preset (CCIR-Poor, 2 ms delay spread, 1.0 Hz Doppler) ceiling stuck
around 45-60% frame delivery even at 20 dB SNR: several adjacent carriers,
spaced close to the path's coherence bandwidth, fade together for the life of
a frame, costing the rate-1/2 K=7 code more coded bits at once than it
reliably recovers. `experiments/hc2/` explored a faster rung -- differential
8-PSK plus a rate-1/2 K=9 code, same carrier geometry -- to see whether a
stronger code could buy back the extra bits/symbol's SNR penalty. It could
not: HC2 wins throughput (+54% payload bytes at the same frame duration) in
AWGN and CCIR-Good/quiet conditions but loses badly under moderate/disturbed
conditions, because raising bits/carrier raises the cost of exactly the
correlated-carrier-fade loss HC1 already struggles with. See
`experiments/hc2/RESULTS.md` for the numbers and what a next attempt (outer
burst coding or frequency diversity, not a higher modulation order) should
try instead. Not qualified, not on any registry, no on-air mode ID.

## Control-frame loss

Lost control traffic is tested because a clean DATA loop does not exercise
half-open connections or retry exhaustion. The measured VHF retry cycle,
created by suppressing the first five DATA_ACKs, produced a 44.4-second worst
peer silence. This includes retransmission keyings omitted by simple timeout
arithmetic and is why inactivity policy has a wide margin.

Recovery is covered by `tests/test_link_recovery.py` and
`scripts/hw_half_open_recovery.py`. Protocol semantics are in
[LINK.md](../LINK.md); current policy values and their reasoning live beside
the code in `whale/policy.py`.

## Frame duration and clock tolerance

CPFSK's useful sync-through-CRC audio is capped at 3.0 seconds. This balances
retransmit granularity, half-duplex responsiveness, and rigid-grid clock
tolerance. It does not cap the entire modem: VF3 uses a cyclic prefix and
per-carrier equalization and keys for about 5.2 seconds.

Rigid CPFSK decoding has approximate clock tolerance `0.5 / n_bits`. With the
keying budget filled, the historical figures were about 745 ppm at 300 baud,
370 ppm at 600 baud, and 235 ppm at 1200 baud. The original sound cards
measured 3.4 ppm apart. Other hardware should be measured rather than assumed
to behave like that bench.

## Open performance work

- Explain the asymmetric second-frame CRC failure before reconsidering burst
  transmission.
- Reduce robust-control ACK cost after a peer demonstrates a faster receive
  mode, without weakening recovery.
- Re-run hardware qualification after mode geometry, radio setup, or DSP
  behavior changes.
- Report useful throughput with retries, channel parameters, and both link
  directions.

Promotion decisions belong in
[MODE_QUALIFICATION.md](../MODE_QUALIFICATION.md), not in this history.
