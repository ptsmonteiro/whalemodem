# Shipped modes and measurements

This is the only maintained mode-results document. It lists production modes
from the current registry and directly recorded pass points. Rates are net
application bits per second for one full-capacity DATA frame: DATA payload bits
divided by complete frame airtime. They exclude ACKs, retries, and turnaround.

`Frequency span` is the lowest to the highest carrier or tone centre,
reported by each mode's `describe()`; it is not occupied bandwidth.
`SNR` is the simulator's 3 kHz reference unless noted. `Watterson` names the
simulated fading preset. HF radio results use the standard bench signal: a
4-second deterministic equal-power multitone comb from 300 to 2700 Hz in
60 Hz steps, measured from received audio against inter-tone noise and scaled
to a 3 kHz reference bandwidth. The value comes from
`scripts/measure_hf_bench_snr.py`: the signal is transmitted by the IC-7300,
captured as 12 kHz audio from the IC-705, and measured from the received
audio spectrum. The 2026-09-08 measurement was 33.96 dB, rounded to 34.0 dB
in the table. Drive multipliers and decoder estimates are not SNR.

## FM

FM radio results use a handheld at one end. Its volume knob is an analog
control that is never at exactly the same setting twice, so receive audio
level is uncontrolled: two runs of the same mode on the same pair of radios
are not level-matched, and two different handhelds are not comparable on
level at all.

The default FM ladder, in rate order. vf14-4 is the control mode. `Sim C/N
passed` is the simulated `flat_nbfm` RF C/N pass point.

| Mode | Frequency span | Modulation geometry | Modulation order | FEC rate | FEC technique | DATA payload/frame | Frame duration | Net/frame | Sim C/N passed | Radio tested |
| --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | --- | --- |
| vf14-4 | 600-2,400 Hz | 4-tone noncoherent FSK, 600 Bd | 4 (4-FSK) | 3/4 | punctured K=7 convolutional, interleaved, soft-decision Viterbi | 264 B | 3.360 s | 628.6 bit/s | 0 dB (20/20); not passed at -1 dB (14/20) | passed on bench radios; SNR not measured |
| vf13 | 600-2,850 Hz | 16-tone combinatorial noncoherent MFSK, 6 of 16 tones per symbol, 150 Bd | 4,096 (of C(16,6)=8,008) | 7/8 | punctured K=7 convolutional, interleaved, soft-decision Viterbi | 1,427 B | 7.983 s | 1,430.0 bit/s | 2 dB (20/20); not passed at 1 dB (2/20) | passed on bench radios; SNR not measured |
| vf16 | 500-3,000 Hz | 51-carrier 8PSK OFDM (43 data, 8 comb pilots) | 8 (8PSK) | 2/3 | IEEE 802.11n QC-LDPC, interleaved | 1,928 B | 4.947 s | 3,117.8 bit/s | not measured | passed on bench radios; SNR not measured |
| vf12 | 500-3,000 Hz | 51-carrier 16-QAM OFDM (43 data, 8 comb pilots) | 16 (16-QAM) | 3/4 | IEEE 802.11n QC-LDPC, interleaved | 2,900 B | 4.947 s | 4,689.7 bit/s | not measured | passed on bench radios; SNR not measured |

## HF

Every HF mode prepends a 0.2 s settling head -- a stretch of its own
modulation, in front of its sync preamble, that the receiver never
correlates against -- to cover PTT ramp and receiver AGC settling. The
durations below include it.

| Mode | Frequency span | Modulation geometry | Modulation order | FEC rate | FEC technique | DATA payload/frame | Frame duration | Net/frame | Pure SNR passed | Watterson passed | Radio tested |
| --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | --- | --- | --- |
| hr0 | 562.5-2,015.625 Hz | 32-tone noncoherent FSK | 32 (32-FSK) | 1/2 | terminated K=9 convolutional, interleaved, soft-decision Viterbi | 32 B | 3.945 s | 65 bit/s | -10 dB and above | quiet, moderate, disturbed: 0 dB and above | 2026-09-08, 10/10 at 34.0 dB SNR |
| hc0 | 750-2,156.25 Hz | 16-tone noncoherent FSK | 16 (16-FSK) | 1/2 | terminated K=7 convolutional, interleaved, soft-decision Viterbi | 91 B | 5.097 s | 143 bit/s | -7 dB and above | quiet, moderate, disturbed: -5 dB and above | 2026-09-08, 10/10 at 34.0 dB SNR |
| hf9 | 300-2,700 Hz | 49-carrier QPSK OFDM | 4 (QPSK) | 1/2 | IEEE 802.11n QC-LDPC, interleaved | 105 B | 1.056 s | 795 bit/s | 6 dB (97/100) | moderate: 8 dB (96/100); not passed at 6 dB (77/100) | not measured |
| hc1w | 468.75-2,531.25 Hz | 23-carrier differential-QPSK OFDM | 4 (QPSK) | 1/2 | terminated K=9 convolutional, interleaved, soft-decision Viterbi | 995 B | 5.095 s | 1,562 bit/s | 6 dB (90/100) | quiet: 15 dB (95/100); moderate: not passed at 20 dB (75/100); disturbed: not measured | 2026-09-08, 10/10 at 30.8 dB SNR |
| hf8 | 300-2,700 Hz | 49-carrier 8PSK OFDM | 8 (8PSK) | 2/3 | IEEE 802.11n QC-LDPC, interleaved | 2,444 B | 5.456 s | 3,584 bit/s | 12 dB and above | quiet: 18 dB and above | 2026-09-10, IC-7300->IC-705 29/30, IC-705->IC-7300 28/30 (SNR not calibrated this session) |
| hf7 | 300-2,700 Hz | 49-carrier 32-QAM OFDM | 32 (32-QAM) | 3/4 | IEEE 802.11n QC-LDPC, interleaved | 4,722 B | 5.324 s | 7,095 bit/s | 20 dB and above | not passed | 2026-09-10, IC-7300->IC-705 30/30, IC-705->IC-7300 55/55 (SNR not calibrated this session) |

These are snapshots of checked-in shipped code and retained results, not
promises for every radio, path, or direction. When a mode or measurement
changes, edit its row; do not append a history section.
