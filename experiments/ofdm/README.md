# OFDM experiment

An attempt at the highest payload throughput this bench can carry inside a
single 3-second keying, with no FEC and no compression.

The bench is an IC-705 transmitting into a Wouxun KG-UV9D Plus HT and back, on
FM, through the audio path in `whale/hw/audio_io.py`. Everything here is
standalone: nothing in `whale/` imports it, and nothing here is shipped.

## Status

Tested over the air on 2026-08-18. The optimistic wide-band QPSK hypothesis did
not survive the bench. The channel probe decoded 5/5 in both directions and
reported 300–3000 Hz usable with 0.82 ms worst-direction RMS delay spread, but
full data frames exposed frequency-selective errors that the repeated training
symbols did not predict.

The confirmed result is BPSK `ofdm2_50hz_cp8` over 650–1950 Hz: a 2.5 ms
prefix, 27 carriers, and a 343-byte payload. It passed 4/4 screening frames and
then 10/10 confirmation frames in each direction (28/28 total), delivering
914.7 payload bit/s inside the 3-second keying cap. That is 1.07× the shipped
1200-baud mode, but below the MFSK experiment's approximately 1011 bit/s
winner, so OFDM is not currently a production integration candidate.

QPSK over the same clean band delivered 691 bytes and 1842.7 bit/s
arithmetically, but passed only 2/4 IC-705→HT and 1/4 HT→IC-705. Shortening it
to 300 bytes improved the stronger direction to 4/4 but the weaker direction
only to 2/4. See `RESULTS.md` for the ladder and diagnostic evidence.

Periodic full-band timing/EQ pilots were subsequently tested every 4, 8, and
16 data symbols. They did not rescue QPSK: the best weak-path result was 1/4
at interval 16, and denser pilots were 0/4. A saved-frame comparison reduced
five wrong decisions to four, leaving scattered errors that tracking cannot
correct. The next meaningful experiment is FEC, not more pilot density.

## Why OFDM, given that MFSK already works

The MFSK experiment found that the binding impairment on this bench is not
noise. It is dispersion: the audio path smears each symbol into the next by a
few milliseconds, and past a certain symbol rate the smear is a larger fraction
of a symbol than the detector can tolerate. Adding transmit power does not help
with that. Slowing down does, which is why MFSK ended up trading rate for
reliability.

OFDM attacks the same problem from the other side. With a cyclic prefix longer
than the channel's delay spread, dispersion stops being intersymbol
interference and becomes *one complex gain per subcarrier* — a number the
receiver measures from the training symbols and divides out. The impairment
does not get smaller; it gets removable. That is the entire argument for being
here, and `test_dispersion_the_prefix_covers_costs_nothing` in `test_ofdm.py`
is the test that states it: dispersion inside the prefix costs nothing, and
dispersion past it breaks the frame.

The second reason is the detector. The shipped modes are non-coherent FSK,
which pays roughly 4 dB for not tracking phase — non-coherent BFSK needs about
13.4 dB Eb/N0 at BER 1e-5 where coherent QPSK needs about 9.6 dB. OFDM's
per-subcarrier equaliser hands us a phase reference for free, so the
constellation can be coherent. That 4 dB is what buys the move from 1 bit per
carrier to 3 or 4.

## What the frame looks like

```
[head pad] [preamble] [training] [data × N] [tracking pilot] ... [tail pad]
```

Each symbol is `n_fft` samples of useful part preceded by `cp` samples of
cyclic prefix. Subcarriers are spaced `48000 / n_fft` Hz and only those falling
inside 600–2300 Hz are used. The training symbols are Schmidl & Cox style:
energy on even-indexed subcarriers only, which makes the time-domain symbol two
identical halves.

More precisely, the acquisition preamble is Schmidl & Cox style. Training and
tracking pilots occupy every data carrier. With `pilot_interval > 0`, a known
full-band pilot follows each complete group of that many data symbols; the
receiver searches locally around it for timing and interpolates per-carrier EQ
between pilots. `pilot_interval == 0` retains the original front-trained frame.

Sync is two stages, and they have different jobs:

  - the **repetition metric** is the *proposer*. It is cheap, it slides over
    the whole buffer, and it is allowed to be wrong — it only has to nominate
    plausible offsets.
  - a normalised complex cross-correlation against the preamble template is the
    *decider*. It is expensive, so it only ever runs on what the proposer
    nominated.

A pure tone scores well on repetition — a sine wave is trivially two identical
halves — and would sync a repetition-only receiver on every carrier that opened
the squelch. `test_a_pure_tone_does_not_sync` exists because of that.

## Candidates

The grid is `n_fft` in (1600, 1200, 960, 800, 640) × cyclic prefix in (1/4,
1/8, 1/16) × 1–4 bits per carrier: 60 profiles. The single thing the candidates
trade is prefix length, i.e. how much delay spread they survive versus how much
airtime they spend surviving it.

Best profile at each constellation, by arithmetic from the frame layout:

| bits/carrier | profile | CP | carriers | payload | payload bits/s | × shipped 1200 |
|---|---|---|---|---|---|---|
| 1 (BPSK) | `ofdm2_50hz_cp16` | 1.25 ms | 35 | 472 B | 1259 | 1.48× |
| 2 (QPSK) | `ofdm4_50hz_cp16` | 1.25 ms | 35 | 949 B | 2531 | 2.97× |
| 3 (8PSK) | `ofdm8_50hz_cp16` | 1.25 ms | 35 | 1426 B | 3803 | 4.46× |
| 4 (16QAM) | `ofdm16_50hz_cp16` | 1.25 ms | 35 | 1903 B | 5075 | 5.95× |

The reference is `whale.afsk.PROFILE_1200` at **853** payload bits/s, computed
live as `chunk_size * 8 / MAX_KEYING_SECONDS`. Note that `experiments/mfsk`
quotes 947 for the same profile; that figure predates a change to
`framing.HEAD_PAD_SECONDS` and is stale.

Every one of these is the 1/16 prefix, which is exactly what you would expect
when the only cost being counted is airtime. Whether a 1.25 ms prefix survives
this channel is precisely the question the bench has to answer, and the answer
is not in this table.

## What OFDM costs that the FSK modes did not

### Peak-to-average power ratio

This is the first non-constant-envelope mode in the repo. Every previous mode
put the same amplitude out at every instant, so the radio's deviation limiter
never had an opinion. An OFDM symbol is a sum of 35 independent carriers, and
its peaks are far above its average.

That matters because FM deviation is peak-limited while received SNR follows
the *average*. Every dB of PAPR is a dB of average power thrown away. So the
modulator clips and re-filters in software, iteratively, to a target PAPR.

Choosing that target was measured, not assumed. The first default was 6 dB,
which is wrong: at 6 dB the clipping itself puts an EVM floor at −11.8 dB,
barely above what QPSK needs, so the modulator was destroying the signal to
protect it. The measured optimum is **9 dB**, worth about 2.6 dB over not
clipping at all. (A related claim in an early draft — that Newman phases would
give ~3 dB — did not survive measurement either; the real figure was 5.4 dB.)

### Sample-clock offset

The cyclic prefix absorbs clock drift as *timing* without difficulty. It is the
*phase* that kills the frame: drift rotates subcarrier `k` by `2πkτ/n_fft`, and
the frame dies when the top carrier's rotation reaches the constellation's
decision boundary. That gives

```
max_ppm ≈ 1 / (2^(bits+1) · f_top · T_frame)
```

Measured: about **40 ppm at BPSK, 20 at QPSK, 8 at 8PSK**. The two sound cards
on this bench are 3.4 ppm apart (`scripts/measure_clock_offset.py`), so QPSK
has roughly 6× margin but 8PSK has only about 2.4× — and 8PSK is where the
throughput is.

This is worth stating plainly because an early draft of the docstring claimed
the opposite, that the prefix absorbed clock offset and the mode was therefore
tolerant. It is not. **This mode is considerably less clock-tolerant than the
shipped FSK profiles**, which manage 235–745 ppm.

### Payload-dependent behaviour

An all-zeros payload maps every subcarrier to the same constellation point,
which is an impulse in the time domain: 17.1 dB PAPR, −5.9 dB EVM, and symbol
errors on a *noiseless* channel. Random payloads never show it, so the sweep —
which sends random payloads — would have passed this and it would have shipped
as "OFDM is unreliable on some files".

The fix is an order-17 PN whitener applied to the bit stream before mapping and
removed after demapping. It is worth being explicit that **this is not FEC**:
it adds no redundancy and corrects nothing. It only decouples the transmitted
symbol statistics from the payload's content, so that the mode behaves the same
way on a run of zeros as on compressed data.

## Files

| File | What it is |
|---|---|
| `ofdm.py` | The modem: profiles, constellations, whitening, PAPR clipping, sync, equaliser, demodulator |
| `test_ofdm.py` | 22 tests, all passing, including the dispersion argument and the sync traps |
| `probe_channel.py` | On-air per-subcarrier channel probe — measures `\|H\|` and delay spread directly |
| `sweep_ofdm.py` | The on-air ladder: ranks candidates, then confirms the winner |
| `RESULTS.md` | 2026-08-18 on-air results, failure diagnosis, and confirmed profile |

`probe_channel.py` is the one to run first on air, because it measures the
quantity the whole candidate grid is parameterised by. It uses a deliberately
wider band (300–3000 Hz) and a longer prefix than any candidate, so that it can
see the band edges and cannot itself be broken by the dispersion it is
measuring. Its delay-spread estimate carries a −20 dB floor so that it measures
the channel rather than its own noise; validated in software, it recovered
0.44 ms from a channel with a true spread of 0.41 ms.

## Bugs found in software, before spending any airtime

Recorded because the repo's convention is that wrong turns get written down,
and because three of these four would have presented on air as "the channel is
worse than expected" rather than as a bug:

1. **Sync confidence capped at exactly 0.707.** The correlator built an
   analytic (Hilbert) signal from the received audio and correlated it against
   a *real* template. The cap is √2/2, which is a suspiciously round number if
   you notice it and a mediocre channel if you do not. Fixed by making the
   template analytic too.
2. **PAPR default set to 6 dB by assumption**, where measurement says 9 dB.
3. **The all-zeros impulse**, above.
4. **The clock-offset claim was backwards**, above.

## Running it

```
python experiments/ofdm/test_ofdm.py       # software only, no radios
python experiments/ofdm/probe_channel.py   # KEYS THE RADIO
python experiments/ofdm/sweep_ofdm.py      # KEYS THE RADIO
```

The last two transmit. `sweep_ofdm.py` takes `--only` to restrict the candidate
set, `--drive` to A/B amplitude and PAPR target, and `--confirm` to run the
10-each-way confirmation on a single profile.

The methodology follows `experiments/mfsk`: both directions are measured, the
worst direction decides, and ARQ is bypassed so that a failure is a failure
rather than something the retry loop hides. `ht→ic705` has historically been
the weaker leg.

## A note on the bench itself

During setup for the first on-air run, a high-power transmission desensed the
USB bus: the output stream raised `PaErrorCode -9996`, the CI-V un-key got no
reply and raised out of a `finally` block, and the radio's CI-V stayed
unresponsive on both COM ports afterwards. A commanded transmit with an
unacknowledged un-key is the worst failure this codebase can produce.

Reducing transmit power avoids the trigger. It does not remove the failure
path, which is being addressed separately in `whale/hw/`. If you are running
anything in this directory, run it at low power.
