# OFDM on-air results

Measured 2026-08-18 on the IC-705 ↔ Wouxun KG-UV9D Plus FM bench, at the
reduced transmit power used after the USB-desense incident. A result requires
100% byte-for-byte decoding in both directions with no ARQ.

## Channel probe

`probe_channel.py --trials 5` decoded all five probes in both directions. It
reported a common usable band of 300–3000 Hz and worst-direction RMS delay
spread of 0.815 ms, implying a prefix of at least 2.45 ms under the probe's 3×
rule. This correctly selected the 2.5 ms prefix, but its usable-band result was
too optimistic for payload data.

The probe estimates SNR from the scatter of 24 repeated, known training
symbols. Full payload symbols have different peak statistics and need every
carrier decision to be right across roughly one hundred symbols. Consequently
"training carrier above 15 dB" did not imply "CRC-only payload survives" at
the band edges. Treat the probe as a prefix measurement and a band proposal,
not as proof that every proposed carrier is payload-safe.

## Wide-band QPSK

The initial ladder used 300–3000 Hz, QPSK, prefixes of at least 2.45 ms, and
four trials per direction. All five profiles failed every frame despite strong
sync confidence. Drive A/B testing at peak/PAPR settings `(0.9, 9)`, `(0.9,
7)`, `(0.9, 12)`, and `(0.6, 9)` also produced zero decodes, ruling out a
simple drive choice.

A deterministic capture of `ofdm4_50hz_cp8` showed 373 wrong constellation
points out of 5665. Ten of 55 carriers, principally 300–550 Hz and the upper
edge, held 83% of the errors. The clock fit was within the predicted tolerance
and applying it made the result worse; error did not correlate significantly
with ideal symbol peak. The failure was frequency-selective, not sync, clock
drift, or obvious clipping.

## Narrow-band QPSK

At 600–2300 Hz, `ofdm4_50hz_cp8` improved to 3/4 IC-705→HT and 0/4
HT→IC-705. Its diagnostic capture had only 21 wrong points out of 3605, but
CRC-only framing needs zero. Errors concentrated around 2000–2200 Hz and at
the lower edge.

At the cleaner 650–1950 Hz band, the full 691-byte QPSK frame passed 2/4 and
1/4 respectively. A shorter 300-byte frame passed 4/4 and 2/4. Errors occur
throughout the frame, so shortening further gives away the throughput gain
without establishing the required reliability.

## Confirmed result

`ofdm2_50hz_cp8`, BPSK over 650–1950 Hz:

- 50 Hz carrier spacing, 27 carriers
- 2.5 ms cyclic prefix
- 343 payload bytes per 3-second keying
- 914.7 payload bit/s, 1.07× shipped 1200-baud AFSK
- initial screen: 4/4 in each direction
- confirmation: 10/10 in each direction
- total: 28/28 successful frames

The same BPSK profile over 600–2300 Hz passed 4/4 IC-705→HT but only 3/4
HT→IC-705. Removing the diagnosed weak edges was therefore necessary, not
cosmetic.

## Decision

Do not integrate OFDM into `whale/` from this result. The confirmed profile is
reliable and proves the cyclic-prefix design works, but its 914.7 bit/s is
slower than the MFSK experiment's roughly 1011 bit/s winner while adding PAPR,
clock-tolerance, equalisation, and codec-negotiation complexity.

The result could change with FEC or pilot-assisted tracking, but either is a
new frame-format experiment rather than completion work on this CRC-only mode.

## Experimental cross-32-QAM follow-up

Cross-32-QAM was added as an explicit `--bits 5` experiment and tested in both
directions. The profile used 650–1950 Hz, 50 Hz carrier spacing, a 2.5 ms
cyclic prefix, peak amplitude 0.6, and a 12 dB software PAPR target. It carried
1734 bytes, or 4624 payload bit/s arithmetically, in the 3-second keying budget.
All eight frames failed payload decoding: 0/4 HT→IC-705 and 0/4 IC-705→HT.
Sync confidence was 0.953–0.954 and 0.970–0.971 respectively.

Saved-capture diagnostics found 791–1831 wrong decisions among 2781 points per
frame (28.4–65.8%). Derotated EVM ranged from -12.8 to -9.3 dB. One capture
contained a fitted +16 ppm clock excursion, far outside this profile's 3.4 ppm
tolerance, but the other captures still contained hundreds of errors without
that excursion. Some excess error remained around 1750–1800 Hz and mild
limiting could not be separated from noise, but neither was the sole cause.

This is not close enough for shorter CRC-only frames, pilot-density changes,
or another small drive sweep to be meaningful. Any further high-order OFDM
work should begin with FEC and interleaving, using these captures as its input,
before spending more airtime.

The nominally stronger IC-705→HT path was not materially better. Its captures
had 1262–1476 wrong decisions (45.4–53.1%) and -11.5 to -10.5 dB derotated EVM.
Five to seven edge carriers were consistently worse, and one frame showed a
-18 ppm clock excursion, but derotation still left 1087 errors. This rules out
path asymmetry as a way to make the uncoded mode useful.

## 16QAM dense-pilot follow-up

16QAM was tested on IC-705→HT with the same 650–1950 Hz band, 50 Hz spacing,
2.5 ms prefix, peak 0.6, and 12 dB PAPR target. Full-band tracking pilots were
inserted every 4, 2, and 1 data symbols, updating the channel estimate every
90, 45, and 22.5 ms. The profiles carried 1116, 927, and 684 bytes per frame
respectively. Each failed all four trials despite 0.970–0.971 sync confidence.

Representative pilot/4, pilot/2, and pilot/1 captures contained 156/2241
(7.0%), 149/1863 (8.0%), and 136/1377 (9.9%) wrong constellation decisions.
Their EVM was approximately -13 dB. Denser tracking therefore did not improve
the residual and in this sample increased the error fraction; channel-estimate
age is not the binding impairment for uncoded 16QAM.

## Interleaved tracking-pilot follow-up

Pilot-assisted tracking was implemented after the initial result. A known
full-band symbol is inserted after every configurable group of data symbols.
The receiver searches locally for each pilot (making it a symbol-timing
anchor), refreshes every carrier's complex channel estimate, and interpolates
timing and EQ between adjacent pilots. Pilot airtime is included in
`max_payload`; intervals 4, 8, and 16 therefore deliver 556, 617, and 650-byte
QPSK payloads respectively over 650–1950 Hz.

The on-air screen was deliberately run only on the established weak
HT→IC-705 path. Results, four full-budget frames each:

| pilot interval | payload | payload bit/s | decoded |
|---|---:|---:|---:|
| 16 | 650 B | 1733.3 | 1/4 |
| 8 | 617 B | 1645.3 | 0/4 |
| 4 | 556 B | 1482.7 | 0/4 |

No candidate reached weak-path confirmation, so the stronger path was not
keyed. This is now the sweep's normal strategy: screen and confirm the known
weak path first, then validate the strong path once for the final winner.

A weak-path-only drive repeat on interval 16 compared `(peak, PAPR)` settings
`(0.9, 9)`, `(0.9, 7)`, `(0.9, 12)`, and `(0.6, 9)`. They decoded 0/4, 0/4,
3/4, and 0/4 respectively. The chosen `(0.9, 12)` setting then repeated at
2/4. Higher software PAPR is clearly the least damaging setting on this radio,
but it remains far from the 100% criterion; the stronger path was again not
run.

A deterministic interval-16 capture separated estimator behavior from frame
luck. Front-only EQ left five wrong QPSK decisions; full interleaved
per-carrier replacement left four, as did smoothed and common-phase-only
updates. The remaining errors were isolated at 750, 800, 1000, and 1600 Hz,
not concentrated late in the frame or at a band edge. Tracking is therefore
real but not the binding impairment: the CRC-only frame now needs coding gain,
not denser pilots.

### Expanded interval sweep

A follow-up filled in both sides of the original 4/8/16 grid, again screening
only HT→IC-705 at `(0.9, 12 dB)` drive:

| pilot interval | payload | payload bit/s | screen |
|---|---:|---:|---:|
| 32 | 671 B | 1789.3 | 1/4 |
| 24 | 664 B | 1770.7 | 0/4 |
| 12 | 637 B | 1698.7 | 0/4 |
| 6 | 596 B | 1589.3 | 0/4 |
| 3 | 515 B | 1373.3 | 0/4 |
| 2 | 461 B | 1229.3 | 0/4 |
| 1 | 340 B | 906.7 | 4/4, then 8/10 confirmation |

Interval 1 found a genuine density threshold but not a usable mode. Spending
every second OFDM symbol on a pilot reduced the payload rate slightly below
the confirmed BPSK OFDM profile (914.7 bit/s), and two failures remained in
ten confirmation frames. Since weak-path confirmation failed, IC-705→HT was
not tested. This closes the pilot-density question from 1 through 32 symbols:
tracking alone cannot meet the CRC-only reliability requirement.
