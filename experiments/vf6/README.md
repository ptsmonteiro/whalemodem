# VF6 — experimental square-256-QAM VHF FM high-rate mode

VF6 is production-integrated as mode ID `6`, but remains opt-in through
`whale.modes.experimental_registry()`. It keeps VF5's 48 kHz, 58-carrier,
214-symbol, 5.200-second OFDM geometry and ten payload pilot symbols. The 189
data symbols carry eight bits per carrier using normalized Gray-labelled square
256-QAM. Its receiver slices the real and imaginary 16-PAM axes independently.

The 87,696-bit grid is 10,962 bytes. Forty-three byte-interleaved shortened
RS(254,238) codewords occupy 10,922 bytes and leave 40 bytes unused. The
protected packet is 10,234 bytes: a two-byte length, at most 10,228 payload
bytes, and CRC32. After the link's ten-byte air header, one DATA chunk carries
at most 10,218 application bytes. There is no convolutional inner code; this is
deliberately a high-SNR mode for excellent VHF FM channels.

The full 10,218-byte DATA chunk occupies 5.200 seconds, or exactly 15,720
useful bit/s before ARQ turnaround. A deterministic 2026-08-31 smoke sweep
sent full-capacity frames through the project's complete complex-IQ
`flat_nbfm` recipe (300--3000 Hz TX and RX filters, 75 us pre/de-emphasis,
0.8 limiter, 2.5 kHz deviation, 7.5 kHz IF, and squelch), using master seed
20260831. Results were 0/20 at 30 dB RF C/N, 20/20 at 35 dB, and 20/20 at
40 dB. The retained artifact is
`logs/mode_qualification/vhf-fm/vf6/2026-08-31/flat_nbfm_frame_smoke.json`.

This establishes a repeatable excellent-channel simulation operating point
and a useful frame rate above VARA FM Narrow's published 12,750 bit/s
reference. It is not yet an application-throughput or promotion claim: the
run is smaller than the qualification Monte Carlo gate, uses a synthetic
project recipe rather than a measured radio preset, and does not include ARQ
turnaround, sessions, hardware, interoperability, or resource evidence.

### Fine RF C/N characterization

A follow-up fixed-seed campaign exercised the same full-capacity production
decode path. At 31.25, 31.5, and 31.75 dB RF C/N, respectively, 131/300,
164/300, and 188/300 frames decoded. The pointwise Wilson 95% intervals were
38.2--49.3%, 49.0--60.2%, and 57.1--67.9%, placing the statistically bounded
50% transition between 31.25 and 31.75 dB under this synthetic recipe.

At 33, 33.5, 34, 34.5, 35, 35.5, and 36 dB the results were 256, 276, 280,
284, 285, 290, and 297 successes out of 300. Pointwise Wilson bounds support
a 90% threshold between 33 and 34.5 dB. They only bound the 95% threshold
between 33.5 and 36 dB; the observed delivery rate reaches 95% at 35 dB, but
its lower confidence bound is 91.9%. Runs at 36.5 and 37 dB each delivered
393/400 (98.25%, Wilson 95% interval 96.4--99.1%), so this campaign does not
statistically establish 99% frame delivery or an upper threshold for it.

All SNR points above are RF carrier-to-noise ratios referenced to the full
Nyquist bandwidth of the complex-IQ channel. They are not the receiver's
per-carrier estimates, whose median saturates near 33--34 dB in this setup,
nor a baseband audio SNR. The retained qualification JSON includes the raw
58-carrier SNR estimates, 214 symbol EVM values, and per-block RS corrections.
The evidence remains specific to synthetic `flat_nbfm`, maximum-size frames,
zero RF frequency/sample-clock error, and otherwise excellent conditions.
