# hf14_ofdm_bpsk_watterson — most robust BPSK OFDM geometry for 300-2700 Hz, on Watterson fading

**Bottom line:** across a carrier-spacing sweep from 15 Hz to 100 Hz,
uncoded BPSK OFDM on this PHY is **not spacing-limited on mid-latitude
channels** — every geometry that fills the passband behaves within a few dB
of every other one, and all of them hit the same **irreducible fading error
floor of roughly 60-80% frame success that no amount of SNR removes**. The
two levers that *did* move the result materially are (a) having a mid-frame
time pilot at all (`pilot_interval` 4-8 beats `pilot_interval=0` by ~20
points of success rate), and (b) **not** using comb pilots — this PHY's
comb-pilot path is catastrophically harmful under fading (0-30% success
where the same geometry without it gets 55-80%). On `high_latitude_moderate`
(10 Hz Doppler spread) **no configuration tested decoded anything**:
1/1600 frames across four confirmed candidates × 200 trials × 10 SNRs, and
0/16 at every extreme wide-carrier geometry down to 250 Hz spacing. That is
a wall, not a tuning problem, and it is reported as such.

The AWGN reference is strong and identical for every candidate: **200/200
frames at every SNR from +30 dB down to 0 dB**, with the ~50% failure knee at
-4 dB and total failure at -8 dB.

Every number below is measured by the committed harness with fixed seeds; the
exact commands that produced each table are recorded. Nothing here is
hardware evidence, and per this project's own history (`hf10`'s 16-QAM
episode) simulation is not a go/no-go call for real-radio behaviour — it is
used here only to narrow a 3-axis grid down to four configurations worth
spending airtime on.

## What was built, and what was reused

`sweep.py` is the only new code. It reuses, unmodified:

- **`experiments/hf10_ofdm49_v6/ofdm49_v6.py`** — the parametric OFDM PHY
  (`fft_size`, `cp_len`, `bits_per_symbol`, `pilot_interval`,
  `pilot_comb_stride`, `packet_bytes`) and its `bins_in_band()` helper. Always
  called with `bits_per_symbol=1` (BPSK), `fec_rate=None`, `equalizer="gain"`,
  `n_preamble_symbols=2`, and **every** bin inside 300-2700 Hz active, so each
  geometry fills the passband. (v6 was chosen over hf9/v5 purely because it is
  the same sync/equalizer code with a strictly larger parameter surface; the
  FEC path is never enabled.)
- **`whale/channel.py`** — `WattersonChannel` / `WATTERSON_PRESETS` /
  `AwgnChannel` / `ChannelChain`.
- **`whale/qualification.py`** — `trial_seed` for stable per-trial seeds, and
  the channel wiring convention copied verbatim from
  `channel_factory("watterson", ...)`: Watterson fading first, then
  full-Nyquist AWGN at a waveform-referenced SNR with the `seed ^ 0x5A5A`
  noise seed.
- **`whale/rx_audio.downsample`** — the production 48 kHz -> 12 kHz receive
  decimation, so the receiver sees the same sample stream it sees on hardware.

One trial = modulate a random 64 B payload at 48 kHz -> 50 ms silence lead-in
and lead-out -> `channel.process()` + `channel.drain()` -> `rx_audio.downsample`
-> `mode.demodulate()`. Success means the recovered payload equals the
transmitted payload byte for byte (CRC-passing-but-wrong counts as failure).

### Fixed frame size

Every configuration carries the same **70 packet bytes = 64 B payload**
(LENGTH 2 + payload + CRC 4). Holding the delivered information constant is
what makes the success rates comparable across geometries and keeps net bit
rate an honest *reported* secondary number rather than a hidden axis. All
frames land between **0.32 s and 0.53 s of airtime** — far inside the ~10 s
airtime ceiling the next (hardware) phase needs.

### The cyclic-prefix rule

`cp_len = max(36, round(fraction * fft_size))` samples at the 12 kHz design
rate. The floor of 36 samples is **3.0 ms**, chosen to cover the worst
differential delay among the three swept presets
(`high_latitude_moderate` = 3 ms; `mid_latitude_disturbed` = 2 ms,
`mid_latitude_moderate` = 1 ms). The `fraction` term (0.0625 / 0.125 / 0.25,
default 0.125) keeps the CP a usable share of the symbol for the
narrow-spacing geometries, where 3 ms is a negligible fraction of a 50-75 ms
symbol. The preset table does contain a 7 ms case
(`mid_latitude_disturbed_nvis`, `high_latitude_disturbed`); those were **not**
swept, so the 36-sample floor is *not* claimed to be sufficient for them.

### Metric

Primary: frame success rate versus waveform SNR per channel, and from it the
**failure boundary** — the lowest tested SNR at or above which *every* tested
SNR still met the success target. `x` in a table means the target was never
met, even at +30 dB; that is reported, never extrapolated. 95% Wilson score
intervals use the same formula as
`experiments/hc2_32qam/benchmark_hc2_snr.py`. Secondary: net bit rate
(payload bits / frame airtime), reported but **never** used for ranking.

## Commands run (in order, all from the repository root)

```
# geometry-only sanity print (no trials)
python experiments/hf14_ofdm_bpsk_watterson/sweep.py geometry \
    --config 800:100:8:0 --config 400:50:8:0 --config 240:36:8:0 --config 120:36:8:0

# Pass 1 -- coarse screening over the whole spacing grid  (131 s, 11 520 trials)
python experiments/hf14_ofdm_bpsk_watterson/sweep.py screen \
    --trials 40 --snrs 30 24 20 16 12 8 4 0 \
    --out experiments/hf14_ofdm_bpsk_watterson/screen.json

# Pass 2 -- cyclic prefix as a secondary axis on the five survivors  (68 s, 6 336 trials)
python experiments/hf14_ofdm_bpsk_watterson/sweep.py cp \
    --trials 24 --snrs 30 20 12 8 4 0 --pilot-interval 8 \
    --config 600:0:8:0 --config 400:0:8:0 --config 240:0:8:0 \
    --config 200:0:8:0 --config 120:0:8:0 \
    --out experiments/hf14_ofdm_bpsk_watterson/cp.json

# Pass 3 -- pilot interval and comb-pilot density  (79 s, 6 720 trials)
python experiments/hf14_ofdm_bpsk_watterson/sweep.py pilot \
    --trials 32 --snrs 30 20 12 8 4 \
    --channels awgn mid_latitude_moderate mid_latitude_disturbed \
    --config 240:36:0:0 --config 400:50:0:0 \
    --out experiments/hf14_ofdm_bpsk_watterson/pilot.json

# Pass 4 -- confirmation, 200 trials/point on the top four  (343 s, 32 000 trials)
python experiments/hf14_ofdm_bpsk_watterson/sweep.py confirm \
    --trials 200 --snrs 30 24 20 16 12 8 4 0 -4 -8 \
    --config 240:36:4:0 --config 240:36:8:0 --config 400:50:8:0 --config 600:38:8:0 \
    --out experiments/hf14_ofdm_bpsk_watterson/confirm.json
```

The master seed is `20260903` in every pass, and `trial_seed(master, config
hash, point index, trial)` keys the point index on a hash of
`"{channel}@{snr}"` rather than its position in `--snrs`, so a screening point
and a confirmation point at the same (channel, SNR) reuse the same seeded
channel realizations and the passes are directly comparable. All four JSON
artifacts are committed alongside this document. Two smaller ad-hoc
diagnostics (the high-latitude probes in the section below) were run as
throwaway one-liners against the same module and are reported with their
trial counts inline.

## Pass 1 — carrier spacing (screening, 40 trials/point)

Frames decoded, at three representative SNRs, `pilot_interval=8`, CP by the
default rule:

| fft_size | spacing | carriers | CP | net bps | mid_mod 30 dB | mid_mod 12 dB | mid_mod 8 dB | mid_dist 30 dB | mid_dist 12 dB | mid_dist 8 dB |
|---|---|---|---|---|---|---|---|---|---|---|
| 800 | 15.0 Hz | 161 | 8.3 ms | 975 | 25/40 | 26/40 | 25/40 | 15/40 | 16/40 | 11/40 |
| 600 | 20.0 Hz | 121 | 6.2 ms | 1138 | 33/40 | 26/40 | 27/40 | 16/40 | 15/40 | 11/40 |
| 480 | 25.0 Hz | 97 | 5.0 ms | 1264 | 29/40 | 28/40 | 25/40 | 22/40 | 20/40 | 15/40 |
| 400 | 30.0 Hz | 81 | 4.2 ms | 1365 | 31/40 | 30/40 | 24/40 | 24/40 | 22/40 | 17/40 |
| 300 | 40.0 Hz | 60 | 3.2 ms | 1298 | 27/40 | 30/40 | 27/40 | 20/40 | 19/40 | 14/40 |
| 240 | 50.0 Hz | 49 | 3.0 ms | 1391 | 33/40 | 28/40 | 23/40 | 26/40 | 26/40 | 13/40 |
| 200 | 60.0 Hz | 41 | 3.0 ms | 1446 | 30/40 | 25/40 | 23/40 | 22/40 | 17/40 | 12/40 |
| 160 | 75.0 Hz | 33 | 3.0 ms | 1425 | 23/40 | 24/40 | 24/40 | 21/40 | 20/40 | 15/40 |
| 120 | 100.0 Hz | 25 | 3.0 ms | 1407 | 30/40 | 23/40 | 18/40 | 23/40 | 15/40 | 17/40 |

**Reading:** on `mid_latitude_moderate` the whole 15-100 Hz spacing range is
within sampling noise of itself (23-33 of 40 at 30 dB; the 95% Wilson
intervals all overlap heavily at n=40). On `mid_latitude_disturbed` the
narrowest spacings (15 Hz / 20 Hz, i.e. 67 ms and 50 ms symbols) are visibly
the *worst* — 15-16/40 versus 22-26/40 in the 25-50 Hz middle — consistent
with a 1 Hz Doppler spread smearing a 50-75 ms OFDM symbol. Nothing in this
pass reached 90% success on any fading channel at any SNR. Every geometry
reached 40/40 on AWGN at every tested SNR down to 0 dB.

Survivors carried forward: fft 600, 400, 240, 200, 120.

## Pass 2 — cyclic prefix (24 trials/point)

| config | CP | net bps | mid_mod 30/12/8 dB | mid_dist 30/12/8 dB |
|---|---|---|---|---|
| fft600 cp38 | 3.2 ms | 1204 | 17/17/15 | 14/13/11 |
| fft600 cp75 | 6.2 ms | 1138 | 18/18/20 | 10/11/6 |
| fft600 cp150 | 12.5 ms | 1024 | 18/15/16 | 11/8/6 |
| fft400 cp36 | 3.0 ms | 1409 | 16/17/17 | 14/16/8 |
| fft400 cp50 | 4.2 ms | 1365 | 19/16/14 | 14/14/7 |
| fft400 cp100 | 8.3 ms | 1229 | 21/18/11 | 13/12/13 |
| fft240 cp36 | 3.0 ms | 1391 | 18/14/17 | 17/17/11 |
| fft240 cp60 | 5.0 ms | 1280 | 20/15/15 | 10/12/6 |
| fft200 cp36 | 3.0 ms | 1446 | 17/13/12 | 13/10/7 |
| fft200 cp50 | 4.2 ms | 1365 | 19/17/12 | 13/7/7 |
| fft120 cp36 | 3.0 ms | 1407 | 20/11/12 | 12/9/12 |

(All counts out of 24.)

**Reading — an honest null result.** Above the 3 ms floor, CP length is not a
usable robustness lever on these presets: no monotone trend, and the spread
between the shortest and longest CP at a given `fft_size` is inside the n=24
noise. Since a longer CP costs airtime for nothing measurable here, the
recommended configurations all sit at or just above the 3 ms floor. This
should **not** be read as "CP does not matter" in general — it means the
swept presets' differential delays (1-3 ms) are already covered by the floor,
and a 7 ms preset (untested here) would very likely reverse it.

## Pass 3 — pilot structure (32 trials/point) — the one big effect

| config | net bps | mid_mod 30/12/8 dB | mid_dist 30/12/8 dB |
|---|---|---|---|
| fft240 cp36, **pilot_interval 0** (none) | 1590 | 19/12/16 | 9/12/10 |
| fft240 cp36, pilot_interval 4 | 1309 | **30**/21/18 | **21**/13/17 |
| fft240 cp36, pilot_interval 8 | 1391 | 26/21/21 | **23**/21/13 |
| fft240 cp36, pilot_interval 16 | 1484 | 25/22/18 | 18/11/17 |
| fft240 cp36, pi 8 + **comb stride 6** | 1237 | 4/4/0 | 0/0/0 |
| fft240 cp36, pi 8 + **comb stride 3** | 968 | 10/5/0 | 2/0/0 |
| fft240 cp36, pi 0 + **comb stride 3** | 1113 | 2/0/0 | 2/1/0 |
| fft400 cp50, pilot_interval 0 (none) | 1517 | 20/17/20 | 7/13/13 |
| fft400 cp50, pilot_interval 4 | 1241 | 23/24/19 | 15/15/12 |
| fft400 cp50, pilot_interval 8 | 1365 | 24/22/18 | 20/17/13 |
| fft400 cp50, pilot_interval 16 | 1365 | 25/19/24 | 10/16/15 |
| fft400 cp50, pi 8 + comb stride 6 | 1050 | 0/1/0 | 1/0/0 |
| fft400 cp50, pi 8 + comb stride 3 | 910 | 0/0/0 | 0/0/0 |
| fft400 cp50, pi 0 + comb stride 3 | 1050 | 2/1/0 | 0/0/0 |

(All counts out of 32.)

Two clear results, both larger than anything the spacing or CP axes produced:

1. **A mid-frame time pilot is worth ~20 points of frame success under
   fading**, and paying for it is worth more than the airtime it costs.
   `pilot_interval=0` is the worst non-comb row on both fading channels for
   both geometries, despite having the highest net bit rate in the table.
   Between 4, 8 and 16 the differences are inside n=32 noise; 4 and 8 were
   both carried to confirmation.
2. **Comb pilots are catastrophic in this PHY under fading** — 0/32 to
   10/32 where the identical geometry without them gets 19-30/32, while
   costing 15-35% of the net bit rate. This is a *code* property, not a
   physics one: `ofdm49_v6.demodulate()` blends the time-interpolated gain
   50/50 with a per-symbol, frequency-interpolated estimate taken from the
   comb bins alone, so under fading it mixes a well-averaged estimate with a
   single-symbol, single-bin noisy one and corrupts the good estimate.
   **Recommendation: leave `--pilot-comb-stride 0` on hardware, and treat
   fixing (or removing) that blend as separate future work.** It is harmful on
   AWGN too, just less dramatically and only once noise is present: at +30 dB
   the comb configurations still get 31-32/32, but their AWGN 90%-success
   boundary is +12 to +30 dB versus +4 dB for the same geometry without comb
   pilots (e.g. `fft400 cp50 pi8 cs3` collapses to 1/32 at +20 dB). A
   high-SNR-only smoke test would therefore have missed this entirely.

## Pass 4 — confirmation, 200 trials/point, 95% Wilson intervals

Frame success rate (successes/200, [Wilson lo, hi]).

### AWGN reference — identical for all four candidates

| SNR | fft240 pi4 | fft240 pi8 | fft400 pi8 | fft600 pi8 |
|---|---|---|---|---|
| +30 .. +4 dB | 200/200 [0.98,1.00] at every step | 200/200 | 200/200 | 200/200 |
| 0 dB | 200/200 [0.98,1.00] | 198/200 [0.96,1.00] | 200/200 [0.98,1.00] | 200/200 [0.98,1.00] |
| -4 dB | 80/200 [0.33,0.47] | 102/200 [0.44,0.58] | 98/200 [0.42,0.56] | 99/200 [0.43,0.56] |
| -8 dB | 0/200 [0.00,0.02] | 0/200 | 0/200 | 0/200 |

**AWGN failure boundary = 0 dB waveform SNR** for all four (Wilson lower
bound ≥ 0.96 at 0 dB), with a very sharp knee: ~50% at -4 dB, nothing at
-8 dB.

### mid_latitude_moderate (1 ms delay, 0.5 Hz spread)

| SNR | fft240 cp36 pi4 | fft240 cp36 pi8 | fft400 cp50 pi8 | fft600 cp38 pi8 |
|---|---|---|---|---|
| +30 | 155 [0.71,0.83] | 142 [0.64,0.77] | **157 [0.72,0.84]** | 153 [0.70,0.82] |
| +24 | 147 [0.67,0.79] | 149 [0.68,0.80] | 141 [0.64,0.76] | 147 [0.67,0.79] |
| +20 | 135 [0.61,0.74] | 137 [0.62,0.75] | 149 [0.68,0.80] | **154 [0.71,0.82]** |
| +16 | 134 [0.60,0.73] | 140 [0.63,0.76] | 146 [0.66,0.79] | 141 [0.64,0.76] |
| +12 | 137 [0.62,0.75] | 125 [0.56,0.69] | 139 [0.63,0.75] | **146 [0.66,0.79]** |
| +8 | 112 [0.49,0.63] | 116 [0.51,0.65] | **131 [0.59,0.72]** | 120 [0.53,0.67] |
| +4 | 80 [0.33,0.47] | 84 [0.35,0.49] | 90 [0.38,0.52] | **105 [0.46,0.59]** |
| 0 | 52 [0.20,0.32] | 57 [0.23,0.35] | 52 [0.20,0.32] | 50 [0.20,0.31] |
| -4 | 4 [0.01,0.05] | 3 [0.01,0.04] | 5 [0.01,0.06] | 2 [0.00,0.04] |

### mid_latitude_disturbed (2 ms delay, 1.0 Hz spread)

| SNR | fft240 cp36 pi4 | fft240 cp36 pi8 | fft400 cp50 pi8 | fft600 cp38 pi8 |
|---|---|---|---|---|
| +30 | **124 [0.55,0.68]** | 116 [0.51,0.65] | 114 [0.50,0.64] | 96 [0.41,0.55] |
| +24 | **121 [0.54,0.67]** | 121 [0.54,0.67] | 93 [0.40,0.53] | 93 [0.40,0.53] |
| +20 | 114 [0.50,0.64] | **123 [0.55,0.68]** | 100 [0.43,0.57] | 94 [0.40,0.54] |
| +16 | 108 [0.47,0.61] | **116 [0.51,0.65]** | 89 [0.38,0.51] | 80 [0.33,0.47] |
| +12 | 95 [0.41,0.54] | **113 [0.50,0.63]** | 88 [0.37,0.51] | 93 [0.40,0.53] |
| +8 | 81 [0.34,0.47] | 82 [0.34,0.48] | **90 [0.38,0.52]** | 81 [0.34,0.47] |
| +4 | 50 [0.20,0.31] | 51 [0.20,0.32] | **60 [0.24,0.37]** | 65 [0.26,0.39] |
| 0 | 18 [0.06,0.14] | **34 [0.12,0.23]** | 26 [0.09,0.18] | 36 [0.13,0.24] |
| -4 | 0 | 0 | 0 | 3 [0.01,0.04] |

### high_latitude_moderate (3 ms delay, 10 Hz spread) — a wall

**1 success in 8 000 trials** (fft240 cp36 pi4, +30 dB, 1/200). Every other
(config, SNR) cell is 0/200, Wilson upper bound 0.02. Additional probes
looking for any geometry that survives it:

- Wide-carrier extremes, 16 trials each at +30 dB, `pilot_interval` 1 and 2:
  fft 160/120/100/80 all **0/16**; fft 60 (200 Hz spacing, 12 carriers)
  **1/16**; fft 48 (250 Hz spacing, 9 carriers) **1/16** and **0/16**.
- Very short frames (so the frame is inside the coherence time), 16 trials at
  +30 dB: fft240 with a 4 B payload (0.12 s frame) **0/16**; fft60 with a 4 B
  payload (0.10 s frame) **4/16**. The same short frames on
  `mid_latitude_disturbed` got 13-14/16, so the harness is not broken — the
  channel is.
- Diagnostics show the **preamble correlator still syncs** (12/12 syncs,
  confidence ~0.75) while the payload is destroyed, and that
  `pilot_interval=1` (a pilot before every single data symbol) does not help.
  The failure is therefore intra-symbol ICI from the 10 Hz Doppler spread, not
  stale channel tracking: at the best geometry tested the spread is still
  ~13% of the OFDM symbol rate.

**Conclusion: uncoded BPSK OFDM on this PHY does not work on
`high_latitude_moderate` at any spacing, CP, pilot density, frame length or
SNR tested. Do not plan a hardware trial around it.**

## Recommended configurations for the hardware phase

Ranked on robustness (fading success at and below +12 dB, and plateau
height), **not** on throughput. All four are constructible today, all frames
are well under 1 s of airtime, and all use BPSK with no FEC.

| rank | fft_size | cp_len | carriers | spacing | OFDM symbol | symbol rate | pilots | packet bytes | payload | frame | net bit rate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 240 | 36 (3.0 ms) | 49 (300-2700 Hz) | 50.0 Hz | 23.0 ms | 43.48 Bd | `pilot_interval 8`, no comb | 70 | 64 B | 0.368 s (16 symbols) | **1391 bps** |
| 2 | 240 | 36 (3.0 ms) | 49 (300-2700 Hz) | 50.0 Hz | 23.0 ms | 43.48 Bd | `pilot_interval 4`, no comb | 70 | 64 B | 0.391 s (17 symbols) | 1309 bps |
| 3 | 400 | 50 (4.2 ms) | 81 (300-2700 Hz) | 30.0 Hz | 37.5 ms | 26.67 Bd | `pilot_interval 8`, no comb | 70 | 64 B | 0.375 s (10 symbols) | 1365 bps |
| 4 | 600 | 38 (3.2 ms) | 121 (300-2700 Hz) | 20.0 Hz | 53.2 ms | 18.81 Bd | `pilot_interval 8`, no comb | 70 | 64 B | 0.425 s (8 symbols) | 1204 bps |

Measured failure boundaries (lowest SNR still meeting the target; `x` = never
met at any tested SNR up to +30 dB):

| config | AWGN @90% | mid_mod @50% | mid_dist @50% | high_mod | mid_mod plateau | mid_dist plateau |
|---|---|---|---|---|---|---|
| 1. fft240 cp36 pi8 | **0 dB** | +8 dB | **+12 dB** | x | ~65-75% | ~55-62% |
| 2. fft240 cp36 pi4 | 0 dB | +8 dB | +16 dB | x | ~67-78% | **~58-62%** |
| 3. fft400 cp50 pi8 | 0 dB | +8 dB | +30 dB | x | **~70-78%** | ~45-57% |
| 4. fft600 cp38 pi8 | 0 dB | +4 dB | x | x | ~70-77% | ~40-48% |

No configuration reached 90% success on **any** fading channel at **any**
SNR, so the 90% column is honest only for AWGN; the fading columns use a 50%
target and the plateau column states the ceiling directly. Rank 1 is
recommended first because it holds the highest disturbed-channel success at
the mid SNRs (+12 to +20 dB) that a real HF link actually sits at, and it is
also the highest net bit rate of the four. Ranks 3 and 4 buy a slightly better
`mid_latitude_moderate` plateau at a real cost on `mid_latitude_disturbed`, so
they are worth carrying to hardware as the "quiet-path" alternatives, not as
substitutes.

### Exact hardware command lines (run 2026-09-03)

`experiments/hf10_ofdm49_v6/hardware_test.py` exposes every parameter these
configurations use and defaults the active bin set to all in-band bins for the
given `--fft-size` (exactly what the sweep used). Its safe default is radio A
transmitting with radio B structurally receive-only. Reverse operation is
available only with both `--direction ba` and the conspicuous
`--allow-ic705-tx` acknowledgement; there is no `both` direction.

```
# 1. fft240 / cp36 / pilot 8  (recommended first)
python experiments/hf10_ofdm49_v6/hardware_test.py --a ic7300 --b ic705 \
    --fft-size 240 --cp-len 36 --bps 1 --packet-bytes 70 \
    --pilot-interval 8 --pilot-comb-stride 0 --equalizer gain --trials 10

# 2. fft240 / cp36 / pilot 4
python experiments/hf10_ofdm49_v6/hardware_test.py --a ic7300 --b ic705 \
    --fft-size 240 --cp-len 36 --bps 1 --packet-bytes 70 \
    --pilot-interval 4 --pilot-comb-stride 0 --equalizer gain --trials 10

# 3. fft400 / cp50 / pilot 8
python experiments/hf10_ofdm49_v6/hardware_test.py --a ic7300 --b ic705 \
    --fft-size 400 --cp-len 50 --bps 1 --packet-bytes 70 \
    --pilot-interval 8 --pilot-comb-stride 0 --equalizer gain --trials 10

# 4. fft600 / cp38 / pilot 8
python experiments/hf10_ofdm49_v6/hardware_test.py --a ic7300 --b ic705 \
    --fft-size 600 --cp-len 38 --bps 1 --packet-bytes 70 \
    --pilot-interval 8 --pilot-comb-stride 0 --equalizer gain --trials 10
```

To reproduce any single configuration in simulation before keying a radio:

```
python experiments/hf14_ofdm_bpsk_watterson/sweep.py confirm --trials 200 \
    --snrs 30 24 20 16 12 8 4 0 -4 -8 --config 240:36:8:0
```

(`--config` is `fft_size:cp_len:pilot_interval:comb_stride[:packet_bytes]`.)

## Honest caveats

- **The Watterson results are simulation evidence.** The hardware campaign
  below validates a benign cabled radio path only; it does not reproduce a
  Watterson channel or qualify any fading envelope.
- **The fading plateau is the dominant fact, and it is a property of the
  uncoded frame, not of the geometry.** A 512-bit payload with a CRC needs
  every bit right; one deeply-faded subcarrier or one deep flat fade fails the
  whole frame. That is why every geometry converges to 60-80% no matter how
  much SNR is applied. **FEC plus interleaving across subcarriers and symbols
  is the obvious next lever, and it was deliberately out of scope here**
  (the task asked for BPSK, and `ofdm49_v6`'s LDPC path was left disabled).
  Expect the ranking among these four to change once FEC is added.
- **The comb-pilot result is a finding about this PHY's implementation**, not
  about comb pilots as a technique. `ofdm49_v6`'s fixed 50/50 blend is the
  likely culprit; a properly weighted or 2-D-interpolated estimator could
  plausibly turn comb pilots from a large negative into a positive.
- **The SNR convention under fading is generous by construction.** Waveform
  SNR references the *faded* frame's own mean power, so a frame that spends
  most of its airtime in a deep fade still gets noise scaled to that reduced
  mean — the instantaneous in-fade SNR is much worse than the label. This is
  the same convention `whale/qualification.py` and
  `experiments/hc2_32qam/benchmark_hc2_watterson.py` use, kept for
  comparability.
- **Only three fading presets were swept**, all with differential delays of
  1-3 ms. The 7 ms presets (`mid_latitude_disturbed_nvis`,
  `high_latitude_disturbed`) were not tested and the 3.0 ms CP floor is not
  claimed to cover them.
- **CP and spacing were swept at 24-40 trials in the screening passes.** Their
  null/weak results are honest statements that no effect was resolvable at
  that resolution on these presets, not proof that no effect exists. Only the
  four confirmed configurations carry 200-trial evidence.
- **Payload size was held fixed at 64 B and is not itself a swept axis.** The
  short-frame probes in the high-latitude section show it matters a great deal
  under fading (14/16 versus 5/16 on `mid_latitude_disturbed` for a 4 B versus
  64 B payload at one geometry), so a real link controller will want frame
  length as a rung of its own ladder. That sweep was not run here.

## Hardware validation — COMPLETE BENIGN-PATH SCREEN

The receiver-side blocker recorded in `hardware/blocker.json` cleared before
the resumed run: a receive-only two-second capture on each IC-705 input API
measured RMS 0.0476-0.0478 and peak 0.194-0.208. A fresh rank-1 smoke frame
then decoded with zero errors. The IC-705 remained structurally receive-only;
all 51 valid frames below were IC-7300 TX -> IC-705 RX.

The smoke plus all five planned 10-frame batches completed successfully. Raw
BER covers all 560 packet bits; payload BER covers the 512 delivered bits.

| run | spacing | result | raw BER | payload BER | mean channel SNR | mean per-bin SNR min/median/max | max peak | clipped |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| resume smoke, fft240/cp36/pi8 | 50 Hz | 1/1 | 0/560 | 0/512 | 15.9 dB | 11.6/16.8/39.1 dB | 0.220 | 0/1 |
| rank 1, fft240/cp36/pi8 | 50 Hz | 10/10 | 0/5600 | 0/5120 | 16.3 dB | 11.6/17.1/35.3 dB | 0.254 | 0/10 |
| rank 2, fft240/cp36/pi4 | 50 Hz | 10/10 | 0/5600 | 0/5120 | 17.4 dB | 12.8/17.9/36.0 dB | 0.257 | 0/10 |
| rank 3, fft400/cp50/pi8 | 30 Hz | 10/10 | 0/5600 | 0/5120 | 15.0 dB | 9.3/16.3/38.0 dB | 0.242 | 0/10 |
| rank 4, fft600/cp38/pi8 | 20 Hz | 10/10 | 0/5600 | 0/5120 | 15.4 dB | 9.7/16.5/36.9 dB | 0.275 | 0/10 |
| control, fft120/cp36/pi8 | 100 Hz | 10/10 | 0/5600 | 0/5120 | 15.3 dB | 10.5/16.5/31.5 dB | 0.234 | 0/10 |

Each run retained `result.json` and every raw `.npy` receive capture under
`hardware/resume_smoke_rank1`, `hardware/rank1_fft240_cp36_pi8`,
`hardware/rank2_fft240_cp36_pi4`, `hardware/rank3_fft400_cp50_pi8`,
`hardware/rank4_fft600_cp38_pi8`, and `hardware/control_fft120_cp36_pi8`.
The four candidate commands are listed above; the control used the same
arguments with `--fft-size 120 --cp-len 36 --pilot-interval 8`.

**Interpretation:** on this benign radio path, all four simulation-selected
geometries and the deliberately wide 100 Hz control are error-free at this
sample size. The result validates basic real-radio equivalence and finds no
spacing penalty from 20 to 100 Hz here. It does not validate the simulated
Watterson success plateaus, rank the candidates under fading, or constitute
a promotion-sized hardware qualification campaign.

### Operator-reduced channel run

The same matrix was repeated after the operator reduced the IC-7300 -> IC-705
channel level. The mechanism, attenuation, radio power, and radio settings
were not supplied to the harness, so this is retained as a **reduced-level
run**, not a calibrated RF-SNR point. The IC-705 again remained structurally
receive-only and all transmissions came from the IC-7300.

| run | spacing | result | raw BER | payload BER | mean channel SNR | mean per-bin SNR min/median/max | mean RMS | max peak |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| smoke, fft240/cp36/pi8 | 50 Hz | 1/1 | 0/560 | 0/512 | 16.4 dB | 11.3/17.9/38.3 dB | 0.0106 | 0.087 |
| rank 1, fft240/cp36/pi8 | 50 Hz | 10/10 | 0/5600 | 0/5120 | 16.7 dB | 10.4/18.1/34.9 dB | 0.0103 | 0.095 |
| rank 2, fft240/cp36/pi4 | 50 Hz | 10/10 | 0/5600 | 0/5120 | 17.6 dB | 11.7/18.8/36.5 dB | 0.0107 | 0.101 |
| rank 3, fft400/cp50/pi8 | 30 Hz | 10/10 | 0/5600 | 0/5120 | 16.3 dB | 9.8/17.7/34.6 dB | 0.0102 | 0.085 |
| rank 4, fft600/cp38/pi8 | 20 Hz | 10/10 | 0/5600 | 0/5120 | 16.6 dB | 9.6/18.0/36.4 dB | 0.0108 | 0.096 |
| control, fft120/cp36/pi8 | 100 Hz | 10/10 | 0/5600 | 0/5120 | **12.7 dB** | **7.6/14.3/25.0 dB** | 0.0099 | 0.114 |

No trial clipped. Compared with the first campaign, capture RMS fell from
roughly 0.030-0.033 to 0.010-0.011 (about 9-10 dB in amplitude level), while
the four candidates' decoder SNR estimates remained roughly 16-18 dB. That
means reducing level did not generally reduce signal relative to impairment;
AGC or a common scaling of signal and noise is consistent with the evidence.
The 100 Hz control did measure lower SNR, including weaker per-bin SNR, but
10 frames are enough only to show that this point still works, not to locate
its failure boundary.

The exact commands were the five commands above with unchanged waveform
arguments, `--save-captures`, a `"hf14 reduced-SNR ..."` label, and these
separate output directories: `hardware/reduced_snr_rank1_fft240_cp36_pi8`,
`hardware/reduced_snr_rank2_fft240_cp36_pi4`,
`hardware/reduced_snr_rank3_fft400_cp50_pi8`,
`hardware/reduced_snr_rank4_fft600_cp38_pi8`, and
`hardware/reduced_snr_control_fft120_cp36_pi8`. The preliminary frame is in
`hardware/reduced_snr_smoke_rank1`. Each directory contains `result.json`
and every raw 12 kHz `.npy` capture.

### Historical blocker and invalid dry run

The earlier attempt found the IC-705 input effectively muted and produced one
invalid 0/1 dry-run capture at RMS 2.57e-09. Its normalised-correlation
confidence and frequency offset were noise artefacts, not measurements. That
capture and the contemporaneous diagnostic remain under `hardware/dryrun_rank1`
and `hardware/blocker.json` as reproducible failure evidence; they must not be
combined with the 51 valid frames above.

### Harness changes made for this phase

Three additive edits, all made before the blocker was hit, all covered by the
existing suite (**585 passed**):

1. **`whale/transport.py` / `scripts/bench.py` / `tests/test_ptt_safety.py`** —
   `RadioTransport(name, receive_only=True)` opens the audio device and
   never constructs a PTT backend, so `self.ptt is None`, `send()` raises
   before touching anything, and `close()` has no transmitter to un-key.
   `radio_pair(..., b_receive_only=True)` opens station B that way. Two new
   safety tests cover the refusal and the PTT-less close.
2. **`experiments/hf10_ofdm49_v6/hardware_test.py`** — the receiving station
   is opened without a PTT backend. The normal `ab` path therefore makes the
   IC-705 structurally receive-only. A later operator-authorized reverse test
   added `ba`, guarded by a mandatory `--allow-ic705-tx` acknowledgement; on
   that path the IC-7300 is structurally receive-only. There is no `both`
   option. The receive-only construction also removed the original blocker:
   PTT discovery no longer has to succeed before a listener's audio device
   can be opened.
3. **Diagnostics** — `ofdm49_v6.demodulate()` now additionally returns
   `per_bin_snr_db` (and `_min` / `_median` / `_max`): the per-subcarrier
   post-equalisation SNR, computed from the per-bin residuals against the
   known preamble and time-pilot symbols that the LLR demapper already
   builds. `hardware_test.py` records capture `rms` / `peak` /
   `clipped_samples` per trial, prints a `CLIP(n)` marker, aggregates both
   into the summary, and gained `--save-captures`. **No existing output
   changed and no decode path was touched** — `channel_snr_db`, both BER
   figures and every outcome are computed exactly as before, which the
   full suite confirms.

The per-carrier SNR path was first verified in simulation (a clean loopback
frame at fft240/cp36/pi8 reports 49 bins at min/median/max =
48.8 / 51.0 / 52.3 dB against a scalar `channel_snr_db` of 50.8 dB). The
resumed campaign then exercised it on all 51 valid real captures; the
aggregate hardware values are reported in the table above.
