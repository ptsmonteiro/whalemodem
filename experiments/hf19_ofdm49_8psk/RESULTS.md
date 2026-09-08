# hf19_ofdm49_8psk — HF7's geometry at 8PSK: what robustness costs, and what it buys

**Bottom line: on HF7's own 49-carrier plan and 2 ms guard, swapping 32-QAM
for 8PSK moves the 90%-delivery AWGN boundary from 20 dB to 12 dB — 8 dB —
and rate-2/3 coding, doubled pilot density and a short frame buy the first
measured fading envelope this PHY family has: 90% delivery on quiet Watterson
from 16 dB, a channel on which HF7 manages 7/40 at 24 dB and never reaches
90% at any SNR tested. The price is 3,298 bit/s against HF7's 7,805.**

> **Correction, 2026-09-08.** Every quiet-Watterson boundary in this document
> is a 40-trial screen, and the headline 16 dB figure did not survive the
> bounded `MODE_QUALIFICATION.md` section 3 campaign run afterwards. **The
> qualified boundary is 18 dB**: at 300 trials, 16 dB delivers 90.0% with a
> Wilson lower bound of 86.1% (FER upper 13.9%, over the 10% limit) and 18 dB
> delivers 95.0%, lower bound 91.9% (FER upper 8.1%). The screen's rule --
> observed delivery >= 90% -- passed 16 dB on a raw 90.0% that the interval
> rejects. Treat the boundaries below as relative comparisons between arms,
> which is what they were good for, and not as envelope claims. Data:
> `logs/mode_qualification/hf-ssb/hf19/s3_watterson/`.

**The frame length turned out to matter more than any waveform parameter** on
a fading channel, and it points the opposite way from HF7's frame reasoning.
See pass 3.

**Moderate Watterson is not in the envelope** — the chosen arm reaches 50%
from 16 dB and 68% at 24 dB — and neither is disturbed. That is a real limit
of this waveform, not a tuning gap: see "What did not work" below, where a
longer guard, denser block pilots, a lower code rate and comb pilots were each
tried and none of them moved that boundary.

**The hardware runs agree, in both directions, by more than the simulation
predicted (pass 4).** IC-7300 -> IC-705: HF8's breakpoint is 12 dB of
transmit audio below HF7's. IC-705 -> IC-7300, the weaker and non-clipping
path: **HF7 does not deliver a single frame at any drive level, while HF8
delivers 10/10 with 3 dB to spare.**
That is this project's usual worry inverted — `docs/SIMULATION_RADIO_DIFFERENCES.md`
records simulation repeatedly *over*-promising on this hardware, including a
16-QAM configuration on this same PHY family that passed simulation and was
1/5 on the air. Here the simulated 8 dB was conservative.

The mode was installed as a **DEFAULT** rung by owner decision on 2026-09-07,
and its quiet-class section 3 gate was closed at 18 dB on 2026-09-08 (see the
correction above). One bench path at one drive ladder is still not the
benign/static operating-envelope campaign `MODE_QUALIFICATION.md` requires.
The hardware runs do not touch the fading claims — this bench has no
ionosphere in it — but they are not required to: section 3 makes the fading
envelope a simulated gate, and the hardware gate a benign-path one.

## Method

`sweep.py`, a simulation-only harness, reusing without modification:

- `experiments/hf10_ofdm49_v6/ofdm49_v6.py`, the parametric OFDM PHY that HF6
  and HF7 already wrap. HF7's installed configuration is the baseline arm.
- `whale/channel.py`'s `WattersonChannel` / `AwgnChannel` / `ChannelChain`,
  wired exactly as `whale/qualification.py`'s `channel_factory("watterson",
  ...)` does it: fading first, then full-Nyquist AWGN at a waveform-referenced
  SNR with the `seed ^ 0x5A5A` noise seed.
- `whale/rx_audio.downsample`, so the receiver sees the same 48 kHz → 12 kHz
  decimated stream it sees on hardware.
- `experiments/hf14_ofdm_bpsk_watterson/sweep.py`'s structure, Wilson
  interval, boundary rule and per-trial seeding.

The **boundary** reported throughout is that file's rule: the lowest tested
SNR at or above which *every* tested SNR still met the target. `x` means the
grid never bracketed it — never an extrapolation.

Channels are AWGN plus the three fading classes `SPEED_LADDERS.md` names:
quiet (0.5 ms / 0.1 Hz), moderate (1.0 ms / 0.5 Hz) and disturbed
(2.0 ms / 1.0 Hz), as `mid_latitude_quiet` / `_moderate` / `_disturbed`.

## Pass 1: the constellation, coding, guard and pilot grid

27 8PSK arms — guard 2/3/4 ms × LDPC 3/4, 2/3, 1/2 × a full pilot symbol
every 20, 10 or 6 — against HF7's configuration, all at a common 1,000-byte
frame so delivery is compared at equal delivered information. 10 trials per
point, SNRs 9/14/19/24 dB. Full data in `screen.json`, console log in
`screen.log`.

The result is dominated by one number:

| arm | net bps | AWGN 90% | quiet 90% | moderate 90% | disturbed 90% |
|---|---:|---:|---:|---:|---:|
| HF7 (32-QAM, 3/4, pilots/20) | 7,229 | 19 dB | x | x | x |
| **every** 8PSK arm, all 27 | 2,510–4,462 | **14 dB** | 19–24 dB or x | x | x |

(Both boundaries land one 5 dB step high at this trial count; pass 3 relocates
them to 20 dB and 12 dB on a 2 dB grid at 40 trials.)

**All 27 8PSK arms share an AWGN boundary of 14 dB, against HF7's 19.** The
constellation is the entire AWGN story on this grid; guard, code rate and
pilot spacing did not move that boundary by a single grid step. What they
moved was fading, and only on the quiet class:

| arm | net bps | AWGN | quiet | moderate @24 dB | disturbed @24 dB |
|---|---:|---:|---:|---:|---:|
| `bps3_cp24_fec23_pi10` | 3,805 | 14 | **19** | 50% | 30% |
| `bps3_cp36_fec12_pi20` | 2,905 | 14 | 19 | 20% | 0% |
| `bps3_cp48_fec23_pi10` | 3,488 | 14 | 19 | 30% | 0% |
| `bps3_cp24_fec34_pi20` (HF7's tuning, 8PSK only) | 4,462 | 14 | x | 0% | 0% |

Three arms reached a 19 dB quiet boundary. `cp24_fec23_pi10` is the fastest
of them **and** the best of all 27 on both moderate and disturbed at the top
of the grid, so it is the candidate: HF7's guard, rate 2/3, pilots every 10.

Reading the rate cost of each robustness step, at a matched frame:

| step | net bps | what it bought |
|---|---:|---|
| HF7 as installed | 7,229 | — |
| → 8PSK, nothing else changed | 4,462 | the whole 8 dB of AWGN floor |
| → rate 2/3 as well | 3,972 | — |
| → and pilots every 10 | 3,805 | a quiet-Watterson boundary at all |
| → and the short frame (pass 3) | 3,429 | that boundary down to 16 dB |

The constellation swap costs 38% of the rate and buys the entire floor; the
coding and pilot changes cost a further 15% and are what make any fading
envelope reachable; the frame costs 10% more and sets where it lands.

## Pass 2: comb pilots — a negative result

The pass-1 pattern (every arm identical on AWGN, every arm stuck under target
on moderate fading) says the binding constraint under fading is channel
tracking through a frequency-selective, time-varying channel, not link
margin. The PHY already supports comb pilots — carriers present in *every*
OFDM symbol — with four receiver-side ways to spend them. `comb.json` /
`comb.log` screen 12 arms: two bases × stride 0/7/4 × `legacy`, `residual`,
`confidence`.

**Every comb arm lost, on every channel, while also giving up rate.** At
24 dB, best case per channel:

| arm | net bps | AWGN | quiet | moderate | disturbed |
|---|---:|---:|---:|---:|---:|
| `cp24_fec23_pi10` (no comb) | 3,805 | 100% | 90% | 50% | 30% |
| `…_cs7res` | 3,286 | 100% | 70% | 0% | 0% |
| `…_cs7con` | 3,286 | 100% | 80% | 0% | 10% |
| `…_cs4res` | 2,824 | 100% | 70% | 20% | 0% |
| `…_cs7leg` | 3,286 | 90% | 10% | 0% | 0% |
| `…_cs4leg` | 2,824 | 0% | 0% | 0% | 0% |

Two things to keep:

- **Comb pilots are not the answer on this PHY.** Spending 14% or 25% of the
  carriers on them lost more to the reduced data-carrier count and the
  interpolation than per-symbol tracking gained back.
- **`comb_tracking="legacy"` is broken, not merely worse.** It fails on AWGN
  alone — 0/10 at 14 and 19 dB at stride 4. The `common`/`confidence`/
  `residual` paths build their reference as
  `_comb_bin_symbols * exp(1j*phase_schedule) * amp_taper`; `legacy` divides
  by `_comb_bin_symbols` alone, but the transmitter applies the phase
  schedule and taper to comb bins a second time on top of the copy already
  baked into `_comb_bin_symbols`. Any future use of comb pilots on this PHY
  should fix that path or delete it; this experiment did not modify the PHY.

## Pass 3: confirmation at production frame sizes

40 trials per point, 2 dB SNR steps, on `awgn`, quiet and moderate. HF7 at
its installed 4,738-byte frame against the candidate waveform at four frame
sizes, every one an exact whole number of rate-2/3 codewords with no padding.
Data in `confirm.json` / `confirm.log` (4,738 / 2,430 / 1,026 B) and
`frame_size.json` / `frame_size.log` (540 / 270 B).

**The finer grid moved the AWGN result in the candidate's favour and the
fading result against it, and revealed that the frame length — not any
waveform parameter — is what governs the fading envelope.**

| arm | frame | net bps | AWGN 90% | quiet 90% | quiet @24 dB | moderate @24 dB |
|---|---:|---:|---:|---:|---:|---:|
| HF7, 4,738 B | 4.840 s | 7,821 | **20 dB** | x | 7/40 | 0/40 |
| 8PSK, 2,430 B | 4.862 s | 3,988 | 14 dB | x | 19/40 | 2/40 |
| 8PSK, 1,026 B | 2.090 s | 3,904 | **12 dB** | 24 dB | 36/40 | 6/40 |
| 8PSK, 540 B | 1.144 s | 3,734 | **12 dB** | 20 dB | 39/40 | 18/40 |
| **8PSK, 270 B** | **0.616 s** | **3,429** | **12 dB** | **16 dB** | **38/40** | **27/40** |

Two things to take from this table.

**The AWGN floor is 8 dB, not the 5 dB the coarse screen suggested.** HF7 is
0/40 at 16 dB and 25/40 at 18; the 8PSK arms are 40/40 from 12 dB. The
10-trial screening grid at 5 dB steps had put both boundaries one step high.

**Frame length dominates the fading envelope, and it points the opposite way
from HF7's frame reasoning.** Holding the waveform *completely* fixed and
varying only the frame, the quiet-Watterson boundary moves 4.862 s → never,
2.090 s → 24 dB, 1.144 s → 20 dB, 0.616 s → 16 dB, and moderate-class
delivery at 24 dB goes 5% → 15% → 45% → 68%. A long frame straddles deep
fades, and no amount of coding inside it recovers a codeword that spent its
life in a null. HF7's own frame was sized *up* to 4.84 s to amortize the
bench's fixed ~155 ms per-keying cost to ~3%; that is the right trade for a
peak-rate mode on a good path and the wrong one here.

The 270-byte frame is therefore the design point: 5 codewords, 0.616 s,
3,298 bit/s of net application throughput (chunk bits over frame airtime, the
`SPEED_LADDERS.md` denominator). Its quiet-class boundary — 16 dB by this
screen, 18 dB once bounded properly — clears the +19 dB quiet point that
document sets for its Level-3 "fast data" rung either way, at 1.6x that
rung's 2,000 bit/s throughput floor.

**The keying overhead this frame does not amortize is real and is not in that
number.** At ~155 ms per keying — HF7's measured bench figure, not re-measured
here — a 0.616 s frame is ~20% overhead on air, so 3,298 bit/s of waveform
throughput is roughly 2,614 bit/s keyed. That is still above the Level-3
floor, and it is the honest number to compare against any mode quoted on air
rather than on frame.

## Pass 4: on the radios

`ab_drive_sweep.py`, IC-7300 -> IC-705 with the IC-705 opened receive-only,
2026-09-07. Retained at
`logs/mode_qualification/hf-ssb/hf19/20260907T194237Z-drive-sweep/`
(`result.json`, per-trial `trials.log`), with the first smoke run at
`.../20260907T194026Z/`.

The bench cannot test HF8's claim at full drive: it is a short, strongly
coupled path where HF7 already delivers, so both modes would sit at 100% and
prove nothing. The claim is a *floor*, so the transmit audio is walked down
instead -- the method `scripts/hf4_tx_volume_sweep.py` established here --
and each mode's breaking point is read off. Arms alternate trial by trial
with the order flipping each round, so drift cannot land on one arm; both
arms see every level.

| TX drive | HF8 | HF7 |
|---|---:|---:|
| x1 (0 dB) | **10/10** | **10/10** |
| x0.5 (-6 dB) | **10/10** | 5/10 |
| x0.25 (-12 dB) | **9/10** | 0/10 |
| x0.125 (-18 dB) | 0/10 | 0/10 |
| x0.0625 (-24 dB) | 0/10 | 0/10 |
| x0.03125 (-30 dB) | 0/10 | 0/10 |

**Breakpoint at 90% delivery: HF8 at x0.25, HF7 at x1 -- 12 dB apart**, against
the 8 dB the simulation predicted. Two supporting observations from the same
session:

- **At equal drive the raw BER separates the modes cleanly**: HF8 ran
  0.0000 through the full-drive block while HF7 ran 0.011-0.018 on the same
  path minutes apart. HF7 is leaning on its LDPC to deliver at a level where
  HF8's demodulator is simply not making errors.
- **Both modes fall off a cliff, not a slope.** HF8 goes 9/10 to 0/10 in one
  6 dB step. That is the LDPC waterfall, and it means the breakpoint is sharp
  enough to locate at this resolution but that the 6 dB ladder puts HF8's
  true breakpoint somewhere in (-18, -12] dB; a finer ladder would place it.

What this run does **not** establish:

- **It is not an SNR measurement.** Backing off transmit audio lowers received
  signal against a fixed noise floor, which is the right direction, but the
  drive multiplier is not the calibrated SNR-per-3-kHz reference `CHANNELS.md`
  defines, and this demodulator's own docstring disclaims its reported
  `channel_snr_db`. The 12 dB is a *relative ordering of two modes through one
  path*.
- **It says nothing about fading.** The entire quiet-Watterson result -- the
  reason this mode exists -- is untested on hardware. This bench is a benign
  audio-coupled path.
- **The receive audio clips**, at peak ~4.0-4.7 with several thousand clipped
  samples per capture. That is this bench's standing state, not a regression:
  the retained hf18 runs behind HF7's 94/100 record clipped identically
  (peak 4.4-4.8, every trial), so the comparison is like for like. It does
  mean both arms are measured through a compressed receive path, and HF7 --
  32-QAM, amplitude-bearing -- has more to lose from that than HF8's constant-
  envelope 8PSK. Some unknown part of the 12 dB may be that rather than the
  constellation's noise margin. Fixing the receive level and re-running is the
  obvious next step.
### The reverse path: IC-705 -> IC-7300

Run immediately after, same session, same harness with `--direction ba`.
Retained at `logs/mode_qualification/hf-ssb/hf19/20260907T195827Z-ba-drive-sweep/`.
This path is the weaker of the two and, usefully, **it does not clip**:
capture peak ~0.6, against ~4.5 in the forward direction. It is therefore the
cleaner measurement of the two, and it removes the clipping caveat above
rather than inheriting it.

| TX drive | HF8 | HF7 |
|---|---:|---:|
| x1 (0 dB) | **10/10** | 0/10 |
| x0.7 (-3.1 dB) | **10/10** | 0/10 |
| x0.5 (-6.0 dB) | 3/10 | 0/10 |
| x0.35 (-9.1 dB) and below | 0/10 | 0/10 |

**HF7 does not work on this path at all**, at any drive level, including full
drive. HF8 delivers 10/10 with 3 dB to spare. The dB gap between the two
breakpoints is therefore not measurable here -- HF7 has no breakpoint to
measure -- which is a stronger statement than the forward path's 12 dB, not a
weaker one.

The per-level means show why, and they are worth reading carefully:

| drive | HF8 raw BER | HF8 SNR | HF7 raw BER | HF7 SNR |
|---|---:|---:|---:|---:|
| x1 | 0.0076 | 20.0 dB | 0.058 | 23.4 dB |
| x0.7 | 0.0106 | 18.2 dB | 0.090 | 21.8 dB |
| x0.5 | 0.114 | 13.6 dB | 0.239 | 17.1 dB |

**HF7 reports 3.4 dB *more* SNR than HF8 at every level and still fails.**
The reported figure is a per-bin estimate over the same path, so the two arms
are genuinely seeing a comparable channel; what differs is how much of it each
constellation needs. A 32-QAM symbol at 23 dB on this path is already at
5.8% raw bit error, far past what rate-3/4 LDPC over 78 codewords can carry,
while 8PSK at 20 dB sits at 0.8% and its 5 codewords converge. This is the
mode's whole design argument, measured, on radios, in the direction where it
matters.

It also lands directly on the ladder-coverage question `GOALS.md` frames:
this is a real path, between two real radios, on which the installed default
top rung delivers nothing and HF8 delivers everything.

## What did not work

Recorded so it is not re-tried blind:

- **A longer guard buys nothing.** 3 ms and 4 ms guards were screened against
  HF7's 2 ms across every code rate and pilot spacing. Not one boundary
  improved on any channel; several arms were worse, since the guard is paid
  for in rate. What limits this waveform under fading is frequency
  selectivity, not intersymbol interference the guard can absorb.
- **A lower code rate does not reach moderate fading.** Rate 1/2 costs 24% of
  the rate against 2/3 and left the moderate boundary exactly where it was —
  unreached. The failures there are not marginal-SNR failures.
- **Denser block pilots stop helping at 10.** Pilots every 6 symbols were
  consistently at or below pilots every 10 while costing more rate.
- **Comb pilots, in every arrangement tried.** See pass 2.

## What this experiment did not measure

- **Fading on a radio.** The hardware pass covers the drive/floor claim
  only; every Watterson result here is simulation.
- **The benign/static class**, which `SPEED_LADDERS.md` requires for Level 3
  and 4 qualification and explicitly says AWGN evidence cannot substitute
  for: it must retain a complete filter, frequency-offset, drift, level and
  nonlinearity description.
- **The disturbed class at 40 trials.** It was screened at 10 trials only,
  where no arm came close; the confirmation passes dropped it.
- **Drive level.** Both modes were simulated at the harness default; HF8
  inherits HF7's bench-calibrated `drive_scale=0.008`, which HF7's own
  documentation says does not generalize.
- **Occupied bandwidth as a campaign.** HF8's 99%-power occupied bandwidth
  computes to 2,446.7 Hz against the 2,500 Hz gate, asserted on every run by
  `tests/test_hf8_mode.py`, but that is a single computed measurement rather
  than the 300-trial bounded campaign `MODE_QUALIFICATION.md` describes.

## Open work

- **The obvious next lever is a shorter frame still.** The trade curve above
  is monotone and was not walked to its end; 0.616 s was chosen because it
  clears the Level-3 quiet point, not because the curve flattened there. A
  shorter frame should buy more margin, and the ~155 ms keying overhead sets
  where that stops being worth it.
- **Moderate fading needs something this PHY does not have.** Every lever it
  does have was tried here. Time diversity across frames, or a channel
  estimator that tracks per-bin rather than interpolating block pilots, are
  the candidates — neither is a parameter change.
