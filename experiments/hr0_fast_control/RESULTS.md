# Retained fast-control evidence

These are bounded software experiments, not waveform qualification. The
evidence-based decision was to hold all candidates because the required
reliable measured margin remained unestablished. **On 2026-09-06 the owner
subsequently directed replacement anyway:** MARGIN32's 32-FSK geometry is now
production HR0 (mode ID 10), with 1.812/3.860-second short/full frames. This
is an explicit product override of outstanding gates, not a new measurement
or a gate pass. Both endpoints must update; legacy 128-FSK TX/RX is not
negotiated or retained in the production replacement.

Throughout the historical artifacts and tables below, `hr0` means the
**former 128-FSK baseline**, and `fast32_k9_1024` means the geometry now
selected for production. Those historical labels and results are preserved;
current production HR0 must not be substituted for the old baseline when
replaying these comparisons. The preserved `legacy_hr0.py` and
`legacy_hr0_mode.py` provide that baseline; the screening and session scripts
import it explicitly.

## Broad scout and full-frame checks

[scout.json](results/scout.json) contains 160 rows: four modes, four channel
presets (AWGN and quiet/moderate/disturbed Watterson), ten SNR points, six
trials per row, seed 260906. FAST16 and FAST32 delivered all six ACKs at each
of the derived positive-SNR target points (+6 quiet/moderate, +11 disturbed).
Six successes cannot establish a reliability boundary.

[full_nominal.json](results/full_nominal.json) contains 24 rows: four modes,
three Watterson presets, +6/+11 dB, twelve trials per row, seed 920600.
All rows delivered 12/12 packets. Here HR0/FAST16/FAST32 use complete 42-byte
DATA payloads and HC0 uses 64 bytes. This checks full-body operation at the
nominal points; it does not establish full-frame relative margin. MARGIN32
is absent from this campaign.

## Held-out boundary comparison

[held_out_boundary.json](results/held_out_boundary.json) contains 48 rows:
four modes, four presets, -9/-6/-3 dB, 32 trials per row, seed 910600.
The table extracts actual ACK success **3 dB below** the listed full-HC0
DATA comparison point. Each entry is successful packets out of 32.

| Channel | ACK SNR | HC0 DATA SNR | HR0 ACK | FAST16 ACK | FAST32 ACK | HC0 DATA |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| AWGN | -9 | -6 | 32 | 11 | 24 | 32 |
| Quiet | -6 | -3 | 32 | 27 | 30 | 32 |
| Moderate | -6 | -3 | 32 | 24 | 29 | 31 |
| Disturbed | -6 | -3 | 32 | 15 | 24 | 31 |

The faster candidates' lower observed reliability is evidence against
selecting them on this screen. It is not a fitted threshold or a formal
statistical noninferiority test. HR0 provides useful additional margin in
these samples but spends 3.508 s on each ACK.

## One longer-symbol candidate

[longer32_boundary.json](results/longer32_boundary.json) contains eight rows:
MARGIN32 ACK and HC0 DATA at four paired channel points, 32 trials per row,
fresh seed 930600. MARGIN32 uses 32 tones and 1024 samples per symbol;
complete ACK airtime is 1.812 s, 48.3% below HR0.

| Channel | MARGIN32 ACK SNR | HC0 DATA SNR | MARGIN32 successes | HC0 successes |
| --- | ---: | ---: | ---: | ---: |
| AWGN | -9 | -6 | 32/32 | 32/32 |
| Quiet | -6 | -3 | 31/32 | 30/32 |
| Moderate | -6 | -3 | 32/32 | 31/32 |
| Disturbed | -6 | -3 | 30/32 | 30/32 |

This small screen supports further testing. It does not demonstrate a
boundary at a common high delivery probability: even 32/32 has appreciable
uncertainty, and quiet/disturbed include losses. The variant was chosen
after examining earlier candidates, so independent confirmation matters.

## Independent 300-trial confirmation

[longer32_confirmation.json](results/longer32_confirmation.json) contains all
eight expected rows at the same paired points, with 300 trials per row and
independent seed 1930600. This repeats the longer-symbol comparison after
candidate selection.

| Channel | MARGIN32 ACK SNR | HC0 DATA SNR | MARGIN32 successes | HC0 successes |
| --- | ---: | ---: | ---: | ---: |
| AWGN | -9 | -6 | 300/300 | 300/300 |
| Quiet | -6 | -3 | 295/300 | 295/300 |
| Moderate | -6 | -3 | 268/300 | 273/300 |
| Disturbed | -6 | -3 | 265/300 | 270/300 |

The 32-trial optimism did not hold for moderate/disturbed: observed delivery
is about 89%/88% for the candidate at these points. These counts neither
establish a high-reliability boundary nor prove statistical equivalence to
HC0 3 dB above it. The candidate has five more failures than HC0 in each of
those classes; this small difference alone is not a demonstrated violation
of a particular noninferiority tolerance, which has not been specified.
The evidence-based decision at this stage was **hold, no promotion**: the required reliable measured margin
has not been demonstrated. AWGN success and equal quiet counts cannot close
the fading-channel gates. Further boundary measurements at a stated common
reliability would be required before selecting a replacement.

## One-dB higher Watterson bracket

[longer32_upper_bracket.json](results/longer32_upper_bracket.json) contains
six completed rows, 300 trials per row, independent seed 2930600. Raising
both sides by 1 dB keeps the three-dB comparison while moving closer to a
reliable DATA boundary.

| Channel | MARGIN32 ACK at -5 dB | HC0 DATA at -2 dB | ACK 95% Wilson interval | DATA 95% Wilson interval |
| --- | ---: | ---: | ---: | ---: |
| Quiet | 299/300 | 298/300 | 98.14–99.94% | 97.60–99.82% |
| Moderate | 280/300 | 286/300 | 89.93–95.64% | 92.32–97.20% |
| Disturbed | 277/300 | 283/300 | 88.76–94.84% | 91.11–96.43% |

At moderate/disturbed the HC0 lower confidence bound is above 90%, while the
candidate lower bound remains below 90%. This screen therefore does not
clear a gate requiring a 95% Wilson lower bound of at least 90% on both
sides. The overlapping intervals also mean it does not by itself establish
that the true margin is less than 3 dB. It brackets useful operating points
and left candidate selection on hold pending a measured common-reliability
boundary and the remaining impairment gates. The subsequent owner product
override changes deployment, while leaving this evidence assessment intact.

## Clean link integration

[session_smoke.json](results/session_smoke.json) records complete connect,
128-byte transfer in each direction, and disconnect for the then-production 128-FSK HR0 and
all three candidates. The isolated test ladder is `(control, HC0)`; candidate
mode ID 240 is synthetic and test-only. All four sessions passed. Adapter
head feedback and the timing allowance are needed by calibration and DATA
reception; focused tests pin those interfaces.

| Control waveform | Setup audio | Transfer audio | Disconnect audio | Total audio |
| --- | ---: | ---: | ---: | ---: |
| HR0 | 25.232 s | 109.736 s | 8.808 s | 143.776 s |
| FAST16 | 10.107 s | 32.104 s | 2.429 s | 44.640 s |
| FAST32 | 11.344 s | 37.416 s | 2.792 s | 51.552 s |
| MARGIN32 | 14.032 s | 49.000 s | 3.624 s | 66.656 s |

These are summed transmitted audio across both directions, including adaptive
lead, not elapsed session latency. These short transfers remain largely on
the lowest DATA rung: the total gain includes shorter full DATA frames as
well as ACKs, so it cannot be attributed to ACK savings alone. The clean
synthetic audio path establishes
integration only; it does not measure faded-session recovery or retry-inclusive
latency. The focused production/qualification/experiment tests passed 66
tests after pinning completed failed-frame consumption semantics. No radio hardware evidence is included.

## Measured occupied bandwidth

Maximum equal-tail 99%-power occupied bandwidth across five complete keyings
per listed workload, in hertz:

| Waveform | ACK scout | Full-DATA campaign |
| --- | ---: | ---: |
| HR0 | 2256.3 | 2272.7 |
| HC0 (full DATA in both) | 1506.1 | 1505.3 |
| FAST16 | 1497.5 | 1506.8 |
| FAST32 | 1982.1 | 1993.9 |
| MARGIN32 | 1539.7 | Not measured in these artifacts |

All measured keyings are below 2300 Hz. The MARGIN32 entry comes from its
own boundary artifact. Neither nominal tone-bank width nor these five
payloads certify every possible frame or radio passband.

See [README.md](README.md) for commands, SNR reference details, artifact
completeness checks, replay limitations, and the remaining qualification gates and subsequent product override.

## Production replacement validation

After the explicit owner selection, production HR0's encoded samples were
checked byte-for-byte against MARGIN32 for 0/12/13/42-byte payloads with minimum
and extended leads. Decoder, common-lead, malformed-input, and registry checks
passed. Full 42-byte production frames also passed two fixed-seed Watterson
trials per nominal point (+6 dB quiet/moderate, +11 dB disturbed). The default
HF ladder passed its full paired-audio session with 600 bytes in each direction.
These checks validate the replacement wiring; they do not close the measured
3 dB-margin or radio qualification gates.
