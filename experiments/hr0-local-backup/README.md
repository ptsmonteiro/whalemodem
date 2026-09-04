# HR0 oracle-start viability prototype

HR0 is a fresh, isolated experiment for the bottom HF speed rung. It is not
registered as a production mode and deliberately does not reuse conclusions
from earlier waveform experiments.

The first screen keeps frame acquisition and frequency-offset estimation out
of scope: decoding starts at the exact first sample and assumes zero CFO. This
separates payload/FEC geometry from acquisition risk. The fixed 8.04 s frame
uses 53 real BPSK OFDM carriers from 300 through 2250 Hz (about 1988 Hz
occupied), 37.5 Hz spacing, a 2.67 ms cyclic prefix, and a repeating P-DD
schedule of full-symbol pilots. Pilot estimates are projected onto an
eight-tap delay response and interpolated in time per carrier.

A physical payload of up to 53 bytes is protected by a one-byte length and
CRC32. The resulting 464 bits split evenly across two systematic IEEE
802.11n length-648, rate-1/2 LDPC blocks. Each block has 92 known-zero filler
bits which are not transmitted; the 232 live systematic and 324 parity bits
are sent. The combined 1112 coded bits are interleaved across time/frequency
and repeated eight or nine times in 9646 data cells. Full-payload useful rate
is 52.75 bit/s, above the 18 bit/s published reference for the bottom VARA HF
standard rung, before link/ARQ overhead.

Run the bounded checks and three-trial screen with:

```powershell
python -m pytest experiments/hr0/test_hr0.py -q
python experiments/hr0/screen_hr0.py --trials 3 --snr -15
```

The screen uses the repository's canonical SNR/3kHz AWGN and standard
mid-latitude quiet, moderate, and disturbed Watterson presets. Three trials
per point are only a deterministic viability screen, not a reliability or
qualification claim. Hardware behavior, acquisition, CFO/drift, sample-clock
error, passband shape, clipping/PAPR, and adequate Monte Carlo confidence all
remain open.
