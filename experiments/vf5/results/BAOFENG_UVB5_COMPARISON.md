# VF5 previous-HT / Baofeng UV-B5 comparison

## Test control

Both tests used the committed VF5 modem, three full 2,381-byte frames in each
direction, and seed `20260854`. The six payload files in the UV-B5 run are
byte-identical to their baseline counterparts. The IC-705 and Digirig-style
audio/PTT interface were maintained; only the HT was changed from the Wouxun
KG-UV9D Plus recorded by the bench configuration to the Baofeng UV-B5.

Baseline: `final_both_3.json` and `captures/final_both_3/`.

UV-B5 run: `baofeng_uvb5_both_3.json` and
`captures/baofeng_uvb5_both_3/`.

## Unchanged VF5 receiver result

| Link | Decoded | Median raw BER | Median header SNR | Median payload EVM | RS use |
|---|---:|---:|---:|---:|---:|
| Previous HT → IC-705 | 3/3 | 9.98% | 12.38 dB | -10.52 dB | 22–34 bytes |
| UV-B5 → IC-705 | 2/3 | 5.56% | 14.24 dB | -12.03 dB | 0–1 bytes on passes |
| IC-705 → previous HT | 3/3 | 2.53% | 16.54 dB | -13.62 dB | 0 bytes |
| IC-705 → UV-B5 | 0/3 | not reached | not reached | not reached | not reached |

The primary drop-in comparison is therefore **6/6 for the previous HT versus
2/6 for the UV-B5** with the current unmodified receiver.

The UV-B5 transmit path is better when healthy: its two good frames have
4.60% and 5.56% raw BER, versus 9.75–9.99% for the previous HT. It is less
consistent, however. Its first frame fell to 4.56 dB median header SNR and
13.59% BER and did not decode; the next two had 14.24–14.43 dB header SNR.

## UV-B5 receive acquisition diagnosis

All three IC-705 → UV-B5 captures contain a near-silence interval over the
frame opening after squelch activates: about 110 ms in trial 1 and 50 ms in
trials 2 and 3 using a 0.01 RMS threshold. The current acquisition then ranks
a highly repetitive squelch-tail candidate at samples 304,289–305,408 above
the damaged early header and reports `frame truncated`.

An offline diagnostic constrained only to the plausible early-frame window
selects samples 34,012–34,156 and decodes all three stored captures:

| Trial | Start | Raw BER | Header SNR | Payload EVM | RS corrected | Max/block |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 34,074 | 10.21% | 4.61 dB | -4.91 dB | 18 | 3/16 |
| 2 | 34,156 | 4.94% | 6.68 dB | -9.40 dB | 0 | 0/16 |
| 3 | 34,012 | 5.73% | 6.29 dB | -9.30 dB | 1 | 1/16 |

These are diagnostic replays, not passes by the unchanged receiver. They show
that the UV-B5 receive link carries the full VF5 payload once the true frame
start is selected. Its median assisted BER is 5.73%, versus 2.53% for the
previous HT.

## Audio-level observation

The IC-705 → UV-B5 recordings peak at 0.99997 and clip 0.055–0.152% of their
samples. Their capture RMS is 0.117–0.121. The corresponding previous-HT
captures have no full-scale samples, 0.080–0.083 RMS, and 0.586–0.724 peaks.
The UV-B5 receive audio/interface level is therefore roughly 3–4 dB hotter and
would benefit from a small reduction before another matched run.

## Lower-volume follow-up

The receive level was reduced and the same six payloads were repeated with
seed `20260854`. The unchanged receiver still decoded **2/6** frames: two of
three UV-B5 transmissions and none of the three IC-705 transmissions.

The adjustment did fix the level problem. IC-705 → UV-B5 capture RMS fell from
0.117–0.121 to 0.074–0.083 (a 3.2 dB median reduction), and hard clipping fell
from 0.055–0.152% to zero. This is essentially the same RMS range as the
previous HT baseline.

It did not fix acquisition. The receiver now selected incorrect alignments at
samples 18,252–33,644 within the damaged opening instead of the squelch tail,
producing 41–50% raw BER. Constraining the diagnostic replay to the plausible
early-frame window again decoded all three stored captures:

| Trial | Start | Raw BER | Header SNR | Payload EVM | RS corrected | Max/block |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 34,092 | 14.28% | 0.79 dB | +1.47 dB | 112 | 15/16 |
| 2 | 34,047 | 9.75% | 4.34 dB | -5.08 dB | 19 | 3/16 |
| 3 | 34,147 | 10.12% | 4.43 dB | -5.44 dB | 10 | 2/16 |

These remain diagnostic replays rather than unchanged-receiver passes. The
lower level reduced the assisted median header SNR from 6.29 to 4.34 dB,
increased median raw BER from 5.73% to 10.12%, and increased median RS use from
1 to 19 corrected bytes. Trial 1 was close to the RS per-block correction
limit. The lower setting therefore removed clipping but reduced usable receive
margin.

The UV-B5 → IC-705 direction remained variable and again passed 2/3. Its two
passes had 4.45% and 7.96% raw BER; the 8.09% frame failed because one RS block
was overloaded despite a healthy 15.17 dB median header SNR.

## Open-squelch follow-up

With the UV-B5 squelch opened, the same six payloads were repeated at the
reduced audio setting with seed `20260854`. The unchanged VF5 receiver decoded
**6/6 frames byte-for-byte**, without constrained replay or other assistance.

| Link | Decoded | Median raw BER | Median header SNR | Median payload EVM | RS use |
|---|---:|---:|---:|---:|---:|
| UV-B5 → IC-705 | 3/3 | 7.43% | 13.59 dB | -11.68 dB | 0–13 bytes |
| IC-705 → UV-B5 | 3/3 | 2.15% | 17.72 dB | -13.73 dB | 0 bytes |

The IC-705 → UV-B5 starts were selected correctly at samples 34,086–34,135.
All three frames decoded with 2.00–2.46% raw BER and required no Reed-Solomon
corrections. The recordings had no clipped samples. The UV-B5 transmit path
also completed its first 3/3 run.

Under the matched open-squelch configuration, the UV-B5 result is comparable
to or slightly better than the previous HT baseline: 2.15% versus 2.53% median
BER receiving the IC-705, and 7.43% versus 9.98% transmitting to it. This is a
six-frame comparison, so it demonstrates compatibility rather than a
statistically precise radio ranking.

## Conclusion

The UV-B5 is a working VF5 counterpart when its squelch is open: it achieved
the same **6/6** pass count as the previous HT, with similar or better measured
link quality. The closed-squelch tests establish the root cause of the earlier
failure. The UV-B5 suppressed the opening of the frame for 50–110 ms, causing
acquisition to choose a false alignment; opening squelch removed that blackout
and restored ordinary acquisition immediately.

Keep the squelch open for the current modem and do not reduce the audio level
further. A future modem-side improvement could make acquisition reject
implausible candidates or add more tolerance for a receiver that opens late,
but it is not required for this radio configuration.
