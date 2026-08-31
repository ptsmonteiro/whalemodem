# HC2b bounded-screen results

## Conclusion

Longer differential-QPSK frames are useful only as a high-rate mode for quiet HF
conditions. They do not fix HC1's moderate/disturbed fading weakness. The
2.055 s frame does meet that experiment's nominal frame-rate screen—1,176 payload
bit/s, 54% above the correctly timed 0.775 s HC1 baseline's 764 bit/s—but it
does not yet meet the reliability/confidence gate, and nominal frame rate is
not application throughput.

The 1.521 s variant is the most useful follow-up candidate. At quiet 5 dB it
delivered 18/20 and an observed 980 payload bit/s versus HC1's 17/20 and 650;
at quiet 15 dB it delivered 20/20 and 1,089 bit/s versus 20/20 and 764. The
2.055 s variant was fastest at quiet 10 dB (19/20, 1,117 observed bit/s), but
its longer exposure to fading made it less consistent across the quiet grid.

Twenty trials are insufficient for a qualification confidence bound. These
figures select experiments; they do not promote a mode.

## Method

The screen ran all four frame lengths through identical, paired Watterson
channel realizations at 0, 5, 10, and 15 dB waveform SNR for the standard
quiet, moderate, and disturbed presets. There were 20 trials per
`(preset, SNR, length)` point, full-capacity deterministic random payloads,
and the production HC1 acquisition, equalization, differential QPSK, K=7
soft Viterbi decoder, CRC, carrier geometry, and common HF lead.

Command:

    /Users/pedro/miniconda3/envs/gnuradio/bin/python \
      -m experiments.hc2b.benchmark_hc2b \
      --out logs/scratch/hc2b_screen.json

The artifact is scratch evidence because the tree contains unrelated active
HF-lead work and because this is a bounded 20-trial experiment.

## Selected points

| Preset | SNR | 0.775 s HC1 | 1.095 s | 1.521 s | 2.055 s |
| --- | ---: | ---: | ---: | ---: | ---: |
| quiet | 5 dB | 17/20; 650 b/s | 18/20; 862 b/s | 18/20; 980 b/s | 16/20; 941 b/s |
| quiet | 10 dB | 19/20; 726 b/s | 18/20; 862 b/s | 16/20; 871 b/s | 19/20; 1,117 b/s |
| quiet | 15 dB | 20/20; 764 b/s | 20/20; 957 b/s | 20/20; 1,089 b/s | 18/20; 1,058 b/s |
| moderate | 10 dB | 16/20; 611 b/s | 15/20; 718 b/s | 14/20; 762 b/s | 8/20; 470 b/s |
| moderate | 15 dB | 18/20; 688 b/s | 16/20; 766 b/s | 14/20; 762 b/s | 13/20; 764 b/s |
| disturbed | 15 dB | 4/20; 153 b/s | 4/20; 191 b/s | 4/20; 218 b/s | 1/20; 59 b/s |

Rates in the table are decoded payload bits divided by total keyed seconds
across the 20 trials; they exclude ACKs and turnaround.

## Next experiment

Carry the 1.521 s variant and HC1 into a 100-trial paired quiet sweep around
5-15 dB, then test a punctured higher-rate inner code on the same 1.521 s
geometry. Frame length alone has now yielded most of its plausible gain; the
next candidate must increase code rate or use coherent modulation while
retaining HC1 fallback for moderate/disturbed paths.
