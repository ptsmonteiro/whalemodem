# Shipped modes and measurements

This is the only maintained mode-results document. It lists production modes
from the current registry and directly recorded pass points. Rates are net
application bits per second for one full-capacity DATA frame: DATA payload bits
divided by complete frame airtime. They exclude ACKs, retries, and turnaround.

`SNR` is the simulator's 3 kHz reference unless noted. `Watterson` names the
simulated fading preset. HF radio results use the standard bench signal: a
4-second deterministic equal-power multitone comb from 300 to 2700 Hz in
60 Hz steps, measured from received audio against inter-tone noise and scaled
to a 3 kHz reference bandwidth. The value comes from
`scripts/measure_hf_bench_snr.py`: the signal is transmitted by the IC-7300,
captured as 12 kHz audio from the IC-705, and measured from the received
audio spectrum. The 2026-09-08 measurement was 33.96 dB, rounded to 34.0 dB
in the table. Drive multipliers and decoder estimates are not SNR.

## VHF FM

| Mode | Frequency span | Modulation geometry | Modulation order | FEC rate | FEC technique | DATA payload/frame | Frame duration | Net/frame | Pure SNR passed | Radio tested |
| --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | --- | --- |
| 300baud | 1,200-1,800 Hz | 2-tone CPFSK | 2 (2-FSK) | none | none | 88 B | 3.983 s | 177 bit/s | not retained as pure SNR | not measured |
| 600baud | 1,200-1,800 Hz | 2-tone CPFSK | 2 (2-FSK) | none | none | 193 B | 3.998 s | 386 bit/s | not retained as pure SNR | not measured |
| 1200baud | 1,200-2,200 Hz | 2-tone CPFSK | 2 (2-FSK) | none | none | 402 B | 3.999 s | 804 bit/s | not retained as pure SNR | not measured |
| vf3 | 468.75-3,140.625 Hz | 58-carrier differential-QPSK OFDM | 4 (QPSK) | 1/2 | terminated K=7 convolutional, interleaved, soft-decision Viterbi | 1,426 B | 5.200 s | 2,194 bit/s | — | not measured |

## HF SSB

| Mode | Frequency span | Modulation geometry | Modulation order | FEC rate | FEC technique | DATA payload/frame | Frame duration | Net/frame | Pure SNR passed | Watterson passed | Radio tested |
| --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | --- | --- | --- |
| hr0 | 562.5-2,015.625 Hz | 32-tone noncoherent FSK | 32 (32-FSK) | 1/2 | terminated K=9 convolutional, interleaved, soft-decision Viterbi | 32 B | 3.860 s | 66 bit/s | -10 dB and above | quiet, moderate, disturbed: 0 dB and above | 2026-09-08, 10/10 at 34.0 dB SNR |
| hc0 | 750-2,156.25 Hz | 16-tone noncoherent FSK | 16 (16-FSK) | 1/2 | terminated K=7 convolutional, interleaved, soft-decision Viterbi | 91 B | 5.012 s | 145 bit/s | -7 dB and above | quiet, moderate, disturbed: -5 dB and above | 2026-09-08, 10/10 at 34.0 dB SNR |
| hc1w | 468.75-2,531.25 Hz | 23-carrier differential-QPSK OFDM | 4 (QPSK) | 1/2 | terminated K=9 convolutional, interleaved, soft-decision Viterbi | 995 B | 5.015 s | 1,587 bit/s | 6 dB (90/100) | quiet: 15 dB (95/100); moderate: not passed at 20 dB (75/100); disturbed: not measured | 2026-09-08, 10/10 at 30.8 dB SNR |
| hf8 | 300-2,700 Hz | 49-carrier 8PSK OFDM | 8 (8PSK) | 2/3 | IEEE 802.11n QC-LDPC, interleaved | 2,444 B | 4.972 s | 3,934 bit/s | 12 dB and above | quiet: 18 dB and above | 2026-09-08, 10/10 at 34.0 dB SNR |
| hf7 | 300-2,700 Hz | 49-carrier 32-QAM OFDM | 32 (32-QAM) | 3/4 | IEEE 802.11n QC-LDPC, interleaved | 4,722 B | 5.000 s | 7,555 bit/s | 20 dB and above | not passed | 2026-09-08, 10/10 at 34.0 dB SNR |

These are snapshots of checked-in shipped code and retained results, not
promises for every radio, path, or direction. When a mode or measurement
changes, edit its row; do not append a history section.
