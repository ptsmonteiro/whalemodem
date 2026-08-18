# MFSK bench results

Bench: IC-705 (STA1) and an HT via a Digirig-style interface (STA2), both
squelched, per `whale/hw/radios.py` — the same setup every script in `scripts/`
assumes. Raw logs and JSON in the sweep's `--out` files.

## The mode

**`4fsk_650bd_x0.833`** — the fastest MFSK profile that decodes 100% both
directions inside the 3.0s keying cap.

| | |
|---|---|
| tones | 663 / 1204 / 1746 / 2287 Hz (M=4, Gray coded) |
| symbol rate | 650 baud |
| tone separation | 541.4 Hz — 0.833 symbol rates |
| raw rate | 1300 bps |
| payload | **379 bytes** per keying |
| keying | 2.99s measured, key-down to release |
| **payload throughput** | **1010.7 bits/s** |
| vs shipped `PROFILE_1200` | **1.07×** (947 bits/s) |

## Evidence for the 100% claim

45 frames each direction, 90 total, across **two independent sessions** (radios
closed and reopened between them, AGC and squelch re-settled). Every frame a
different random payload, checked byte-for-byte. No retransmits — ARQ is bypassed
entirely, so each of these is one unaided modulate → TX → capture → demodulate.

| run | ic705 → ht | ht → ic705 |
|---|---|---|
| session 1, ladder | 5/5 | 5/5 |
| session 1, confirmation | 20/20 | 20/20 |
| session 2, confirmation | 20/20 | 20/20 |
| **total** | **45/45** | **45/45** |

Sync confidence held at 0.957–0.963 on the strong leg and 0.882–0.899 on the
weak one, against a 0.70 lock threshold — no trial came close to a marginal sync.

Margin is real rather than knife-edge. `diagnose_mfsk.py` lined the received
symbols up against the transmitted ones on four further frames, two each
direction: **0 symbol errors in 6128 symbols.** The mode is not passing CRC by
luck.

## The ladder

Candidates walked best-throughput-first, 5 trials per direction, stopping at the
first 100%. The cliff is sharp and it is entirely on the **ht → ic705** leg — the
weaker of the two, and the leg every previous tone-placement change in this repo
has died on. The strong leg decoded 5/5 at *every* candidate tried, including the
one 25% faster than the winner.

| candidate | payload bits/s | ic705→ht | ht→ic705 |
|---|---|---|---|
| `4fsk_800bd_x0.6`  | 1258.7 | 5/5 | 0/5 |
| `4fsk_775bd_x0.6`  | 1216.0 | 5/5 | 0/5 |
| `4fsk_750bd_x0.6`  | 1176.0 | 5/5 | 0/5 |
| `4fsk_725bd_x0.7`  | 1136.0 | 5/5 | 0/5 |
| `4fsk_725bd_x0.6`  | 1136.0 | 5/5 | 0/5 |
| `4fsk_700bd_x0.75` | 1093.3 | 5/5 | 0/5 |
| `4fsk_700bd_x0.7`  | 1093.3 | 5/5 | 0/5 |
| `4fsk_700bd_x0.6`  | 1093.3 | 5/5 | 0/5 |
| `4fsk_675bd_x0.75` | 1053.3 | 5/5 | 1/5 |
| `4fsk_675bd_x0.7`  | 1053.3 | 5/5 | 0/5 |
| `4fsk_675bd_x0.6`  | 1053.3 | 5/5 | 0/5 |
| **`4fsk_650bd_x0.833`** | **1010.7** | **5/5** | **5/5** |

## What is actually binding

Symbol rate and tone spacing are **confounded** in that ladder, and not by
accident — it is forced by the band. Wider spacing needs more room, so at a
0.833 ratio the 600–2300 Hz band caps the symbol rate at 657; every candidate
faster than the winner is necessarily *more* tightly spaced. The ladder alone
therefore cannot say which of the two is doing the damage.

`diagnose_mfsk.py` on the nearest miss (`4fsk_675bd_x0.75`, ht → ic705) settles
it. That frame failed CRC with only **8 wrong symbols out of 1596** — a 0.50%
symbol error rate — and the errors are not distributed the way noise or drift
would put them:

- **All 8 were tone 0 read as tone 1.** Not one error on any other tone pair, and
  not one in the reverse direction. 100% adjacent-tone confusion.
- **Spread evenly through the frame** (first error 16% in, last 95% in, no ramp
  across the deciles). Sample-clock drift produces a ramp — errors rare early and
  common late — because symbol points sit on a rigid grid from the sync peak.
  This is flat, so it is not drift.
- **Per-tone received energy was even** (0.75–0.85 across the four), so this is
  not a tone the audio chain fails to carry.

So the binding constraint is **tone separation, not symbol rate** — leakage from
the neighbouring tone into the lowest one. That fits the detector: the box
integrator's frequency response is a sinc with its first null one symbol rate
away, so a neighbour at ratio 0.75 contributes |sinc(0.75)| = 0.30 of its
amplitude, against 0.19 at the winner's 0.833.

Why the *lowest* tone specifically, and only ever the lowest, is the open
question. It is the tone with the fewest cycles per symbol, which is the same
quantity `PROFILE_1200`'s failed re-centring attempts pointed at — but the winner
runs its lowest tone at 1.02 cycles per symbol, *fewer* than the 1.08 of the
candidate that failed, so cycles-per-symbol alone does not explain it either.
The reading that fits both is that the lowest tone is the weakest (its energy
estimate is the dirtiest, having the least of a cycle to integrate over) and that
whether that weakness turns into bit errors depends on how hard its neighbour is
leaking into it. Wider spacing protects the weak tone.

That also explains why the winner is where it is: **650 baud at 0.833 is the
widest spacing available at the highest symbol rate that still fits the band.**
At 650 baud a 0.9 ratio no longer fits; at 675 baud, 0.833 no longer fits. The
optimum sits exactly on that corner, which is a reassuring place for it to be —
it is the constraint boundary, not an arbitrary point.

## The software screen was a poor predictor

Worth recording, because it cost bench time rather than saving it.

`screen_mfsk.py` walks each candidate down an AWGN waterfall and reports the SNR
it needs for 20/20, referenced to what `PROFILE_1200` needs on the same yardstick
(14 dB). **19 of 24 candidates cleared that bar. One worked on air.** Candidates
it cleared at 10 dB — 4 dB of apparent margin — decoded 0/5 on the weak leg.

The ordering carried some signal: the winner needed the *least* SNR of anything
screened (9 dB), and the on-air outcome tracked required-SNR better than it
tracked throughput. But the pass/fail bar was near useless, and the reason is
that **noise is not the binding impairment on this bench.** Adjacent-tone leakage
through a real FM audio chain is, and AWGN does not model it. This is the same
lesson the repo already records from the other direction — placements that
decoded at 0.99 confidence in software and 0/6 on air.

Keep the screen for ordering and for catching outright nonsense. Do not use it to
skip bench trials.

## Is this worth integrating?

An honest 1.07×, with a caveat and a consolation.

**The caveat**: the gain is small, and it is bought by running *closer to the
orthogonality limit* rather than by anything structural. `PROFILE_1200` already
runs sub-orthogonal at 0.833; this mode runs the same ratio with four tones
instead of two. It is not a new margin, it is the same bet made twice.

**The consolation is the better half of the result.** The orthogonal 4-FSK
profile `4fsk_550bd_x1` (625/1175/1725/2275 Hz, 550 baud, 318 bytes, 848 bits/s)
decoded 4/4 both directions first try, at 0.90× the shipped throughput — while
running at **46% of `PROFILE_1200`'s symbol rate** and with genuine orthogonal
tone spacing, so there is no leakage bet in it at all. It also carries roughly
half the symbols per frame, which doubles the sample-clock offset a frame
tolerates. On a link that is symbol-rate-limited — and
`scripts/sweep_baud_600_2300.py` failing 1400 baud at the *same tones* that
cleared 1200 says this one is — that is a much larger robustness margin for a 10%
throughput cost. It is the more interesting mode of the two, and it is not the
one "max throughput" selects for.

## Highest-value follow-ups

1. **Re-measure the high band edge.** The winner's top tone sits at 2287 Hz,
   essentially on the measured 2300 Hz ceiling, and the whole ladder is capped by
   it. `measure_band_edges.py` walked it with a *2-byte* frame at 600 baud, which
   is the easiest thing the channel ever carries; it may be pessimistic, or it may
   have stopped early. At a 0.833 ratio, every 100 Hz of extra ceiling is worth
   ~29 baud, ~57 bps raw. A 2500 Hz ceiling would allow 714 baud and ~1110 bits/s.
2. **Test spacing ratios between 0.833 and 1.0 at lower symbol rates.** The
   failure mechanism is leakage, so the interesting sweep is ratio, and the ladder
   only ever tried 0.833 at one symbol rate because nothing wider fit.
3. **FEC.** The nearest miss failed on 8 symbol errors in 1596, all adjacent-tone,
   all in the same direction. That is squarely inside what a light block code over
   Gray-coded symbols would fix, and it would unlock the 1053–1136 bits/s rungs.
   The Gray mapping is already in place for exactly this.
4. **A/B the preamble training equaliser on air** (`sweep_mfsk.py --no-training`).
   It makes no difference in software; whether it does anything against real group
   delay is untested, and if it does nothing it should come out.
