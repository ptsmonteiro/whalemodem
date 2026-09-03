# hf16 — non-coherent MFSK for the very low SNR `ic705 -> ic7300` path

Standalone experiment. Nothing in `whale/` imports any of it. It reuses, read
only: `whale.dsp.mfsk` (tone bank, Gray map, non-coherent soft metrics,
pattern correlation, offset estimator), `whale.dsp.PacketCodec` (length/CRC32
packet, whitener, multiplicative interleaver, rate-1/2 K=7 or K=9
convolutional code, soft Viterbi), `whale.transport` and `scripts/bench.py`
for the radios.

```
mfsk_mode.py     the waveform: geometry, framing, acquisition, repetition
hardware_test.py the radio campaign runner -- this is what decides anything
replay.py        offline post-mortem against the known transmitted tones
summarise.py     rank runs by net bit rate at a decode-rate target
```

The sibling `experiments/hf15_lowsnr_ofdm/` holds the path measurement
(`sounder.py`, `coherence.py`) this design was derived from, and the record of
the OFDM attempt that preceded it.

## Why MFSK, and not the OFDM this started as

The task began as an OFDM design for the same 300-2700 Hz passband. On this
path OFDM did not work at all: a 49-carrier BPSK frame at 50 Hz spacing
decoded 0/5, and the pilot-based sounder measured per-carrier SNR of -6 to
-14 dB with a carrier offset that was stable at about +8 Hz but a channel
estimate that fell apart past roughly 0.5 s of coherent integration.

Two things then pointed the same way. First, coherent integration stops
paying at a few hundred milliseconds on this path, so every OFDM mechanism
that spends a phase reference is spending it on credit the channel does not
extend. Second -- and this is the larger effect -- the *idle* receiver noise
floor was measured at 0.035 RMS while the same receiver measured 0.075 RMS
during a transmission whose wanted signal was supposedly 14 dB below the
noise. Those two numbers cannot both describe an additive-noise path. They
are consistent with the transmitter contributing broadband energy of its own,
which is what a hard-limiting SSB transmitter does to a signal with a 10 dB
crest factor.

MFSK is constant-envelope. Peak and RMS are the same number, so a
peak-limited transmitter delivers all of it, and there is no phase reference
anywhere in the receive path to lose. The first MFSK screen on this path
decoded 5/5 at M=64 where the OFDM frames had decoded nothing.

## Geometry

M tones fill 300-2700 Hz with spacing = symbol rate = 2400/M, which is what
orthogonal non-coherent detection requires and what makes every length an
exact integer at both sample rates (symbol = 20M samples at 48 kHz, 5M at
12 kHz) with the lowest tone exactly on 300 Hz.

| M | spacing | symbol | raw bit/s |
|---:|---:|---:|---:|
| 32 | 75.00 Hz | 13.3 ms | 375 |
| 64 | 37.50 Hz | 26.7 ms | 225 |
| 128 | 18.75 Hz | 53.3 ms | 131 |
| 256 | 9.38 Hz | 106.7 ms | 75 |

More tones is *worse* per Hz -- `log2(M)/M` bits per second per Hz of band --
and better per symbol, because each symbol is longer and so collects more
energy. That trade, against a path that fades for seconds at a time, is the
whole experiment.

## What the harness does, and two mistakes it exists to prevent

**Configurations are round-robined, never run as blocks.** The first screen
ran each config's five trials consecutively and produced an impossible
result: a configuration that is strictly more robust than another (same
geometry, half the code rate, same preamble) scored 0/5 where the other
scored 3/5. Offline replay showed acquisition had been perfect in every one
of those frames and the symbol error rate was 50-67% throughout -- the path
had simply faded across that whole block. Running trial 1 of every config,
then trial 2 of every config, is what makes configs comparable at all on a
path that swings about 10 dB over minutes.

**Every raw capture is retained.** A live trial reports one bit, and every
failure looks alike from outside. `replay.py` reconstructs the transmitted
tone sequence from the recorded seed, locates the frame by matched-filtering
the *whole* frame (several seconds of processing gain, far more than the
receiver's own one-second preamble), and then separates the causes:

- `acq_err` -- how far the receiver's own acquisition was from the truth, in
  symbols. So far this has been under 0.15 symbols in every frame recorded,
  including frames that decoded nothing: acquisition has never been the
  failure on this path.
- `snr` -- power in the tone that was actually sent against the mean of the
  M-1 that were not. It uses the known tone rather than the winner, so it
  stays meaningful below the point where the mode works.
- `err/decile` and `snr/decile` -- errors bunched in time are a fade,
  spread evenly are a plain energy shortfall. This is what distinguished
  M=128's bimodal behaviour (error-free, or 80-100% wrong for four seconds
  at a stretch) from M=32's uniform 20-50%.
- `nbr` -- the fraction of errors landing on the immediate frequency
  neighbour, which would indicate a spacing or offset problem. Measured at
  0.00-0.10 throughout, so the geometry and the offset correction are not
  the limitation.

## Long frames need a bigger receive buffer

`whale.transport.RX_BUFFER_SECONDS` is 10 s, and a longer frame is silently
truncated -- losing the head and sync, so it presents as total acquisition
failure rather than as a short buffer. `hardware_test.py` raises it for its
own process to fit the longest configured frame. That is a real constraint on
any deployed long-frame mode, not merely on the harness.

## Safety

`--tx` is keyed; `--rx` is opened structurally receive-only, with no PTT
backend constructed, so nothing in the process can key it. IC-705 HF
transmit is authorised as of 2026-09-03; `--allow-ic705-tx` keeps it an
explicit act rather than a default.
