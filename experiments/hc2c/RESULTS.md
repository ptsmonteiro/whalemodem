# HC2c paired pilot-screen results

## Conclusion

Eight payload pilots materially improve a 1.521-second QPSK frame when the
channel evolves during the payload, but they do not solve HC1's underlying
frequency-selective-fade failure. Against exactly paired channel realizations,
the piloted frame delivered more often at every tested moderate and disturbed
point. Its observed frame goodput also improved at every one of those points,
despite carrying 188 bytes instead of 207.

Quiet-channel results were mixed. Pilots won decisively at 5 dB (20/20 and
989 observed payload bit/s versus 15/20 and 816), tied delivery but lost the
pilot overhead at 0 and 15 dB, and lost both delivery and goodput at 10 dB.
With only 20 trials, none of those differences establishes a reliable
operating boundary.

This is positive evidence for payload tracking, not for promoting HC2c. No
moderate or disturbed point approaches the qualification FER gate.

## Controlled comparison

Both candidates use the same:

- 1.521-second keyed duration and common HF lead;
- 19 carriers, 93.75 Hz spacing, and 2.67 ms cyclic prefix;
- differential QPSK;
- rate-1/2 K=7 convolutional FEC, soft Viterbi, interleaving, and CRC32; and
- HC1 acquisition, frequency correction, timing, and header equalizer.

HC2c replaces eight of 90 payload symbols with known full-band pilots. It
unwraps and interpolates per-carrier phase from the header through those
pilots and resets differential decoding after each pilot. Capacity falls
from 207 to 188 physical-layer bytes, reducing nominal payload rate from
1,089 to 989 bit/s.

Every `(preset, SNR, trial)` used the identical channel seed for both modes.
This historical screen used the retired full-Nyquist waveform convention at
0, 5, 10, and 15 dB (equivalent to 9.03, 14.03, 19.03, and 24.03 dB SNR/3 kHz)
for quiet, moderate, and
disturbed Watterson presets. It is deliberately below qualification size.

## Results

Each cell is `delivered/20; observed payload bit/s`.

| Preset | Legacy full-Nyquist SNR | No payload pilots | Eight pilots |
| --- | ---: | ---: | ---: |
| quiet | 0 dB | 13; 708 | 13; 643 |
| quiet | 5 dB | 15; 816 | 20; 989 |
| quiet | 10 dB | 19; 1,034 | 17; 840 |
| quiet | 15 dB | 20; 1,089 | 20; 989 |
| moderate | 0 dB | 1; 54 | 3; 148 |
| moderate | 5 dB | 5; 272 | 8; 395 |
| moderate | 10 dB | 8; 435 | 10; 494 |
| moderate | 15 dB | 11; 599 | 14; 692 |
| disturbed | 0 dB | 1; 54 | 2; 99 |
| disturbed | 5 dB | 0; 0 | 2; 99 |
| disturbed | 10 dB | 2; 109 | 4; 198 |
| disturbed | 15 dB | 4; 218 | 5; 247 |

Command and scratch artifact:

    /Users/pedro/miniconda3/envs/gnuradio/bin/python \
      -m experiments.hc2c.benchmark_hc2c \
      --out logs/scratch/hc2c_paired_screen.json

## Next experiment

Run a three-way ablation on the same frame and channel seeds:

1. no pilots;
2. pilots resetting the differential chain but no interpolated phase track;
3. pilots with the current per-carrier phase interpolation.

That will distinguish the benefit of shortening differential error chains
from actual channel tracking. Use 100 trials around quiet 5-10 dB and
moderate 10-15 dB before changing pilot spacing or moving toward a registry
mode.
