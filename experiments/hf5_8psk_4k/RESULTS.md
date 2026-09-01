# hf5_8psk_4k — real-hardware scaling results

Final config: 8PSK @ 1500 baud with mid-frame pilots, ≈4050 bps sustained
net throughput over multi-second frames on the real IC-7300(TX) ->
IC-705(RX) audio path. See "Recommended configuration (round 3, current)"
at the bottom for the definitive parameters and evidence. Everything above
that is the incremental real-hardware search that got there, kept for the
record (originally developed under the working name `hf_singlecarrier`;
some historical log paths below still use that name since they're the
actual on-disk filenames from when the data was collected).

From-scratch single-carrier mode (`sc.py`): one sinusoid at 1500 Hz (centre
of the 300-2700 Hz audio passband), RRC-shaped PSK/QAM, a 63-chip BPSK PN
preamble for joint time/frequency acquisition + single-tap channel
estimate, and a length+CRC32 frame with PN whitening. No FEC. Tested over
the real audio-coupled bench (`bench.radio_pair`), direct
modulate/TX/capture/demodulate per trial, no ARQ layer. All results below
are from actual over-the-air/audio-patch transmissions, 5 trials per step
unless noted.

**Constraint note**: per an updated task constraint, the IC-705 must never
transmit — it is receive-only for this task, so the only valid direction
going forward is IC-7300(TX) -> IC-705(RX). `hardware_test.py` has been
fixed to hardcode that direction (the `ba`/`both` options that keyed the
IC-705 have been removed). One test earlier in this session (step 11's
`both` run) did key the IC-705 toward the IC-7300 before this constraint
was communicated; that already-collected B->A data point is reported below
for completeness but was not repeated and no further ic705-transmit trials
were run. All other rows (steps 1-10 and the A->B half of step 11) are
IC-7300(TX) -> IC-705(RX) only, consistent with the constraint.

| Step | Baud | Mod (bps) | Payload | Frame (s) | Net throughput | Hardware result |
|---|---|---|---|---|---|---|
| 1 | 100 | BPSK (1) | 10 B | 1.91 | 42 bps | 5/5 |
| 2 | 300 | BPSK (1) | 10 B | 0.64 | 125 bps | 5/5 |
| 3 | 600 | BPSK (1) | 10 B | 0.32 | 250 bps | 5/5 |
| 4 | 1200 | BPSK (1) | 10 B | 0.16 | 500 bps | 5/5 |
| 5 | 2400 | BPSK (1) | 10 B | 0.08 | — | 0/5 (crc_fail, SNR collapsed to ~2 dB) |
| 6 | 2000 | BPSK (1) | 10 B | 0.10 | 800 bps | 5/5 (SNR ~8.4 dB) |
| 7 | 1200 | QPSK (2) | 10 B | 0.11 | ~1500 bps | 5/5 (SNR ~16 dB) |
| 8 | 1200 | 8PSK (3) | 9 B | 0.09 | ~2200 bps | 5/5 (SNR ~16 dB) |
| 9 | 1200 | 16-QAM (4) | 10 B | 0.08 | ~2400 bps | 4/5 (one CRC fail; SNR ~16 dB, near the constellation's noise margin) |
| 10 | 2000 | 8PSK (3) | 9 B | 0.05 | — | 0/5 (SNR ~8.5 dB, all crc_fail) |
| **11** | **2000** | **QPSK (2)** | **58 B** | **0.16** | **≈2900 bps** | **10/10 (both directions)** |

Step 11 is the recommended operating point: QPSK at 2000 baud, 58-byte
payload per frame, **≈2900 bits/second net** (payload bits / total frame
airtime including the preamble). The A→B (IC-7300 TX -> IC-705 RX) half —
the only direction now in scope — was 5/5 with SNR 8.0-8.9 dB. The B→A
half (5/5, SNR 7.4-7.7 dB) was collected before the IC-705-receive-only
constraint was communicated and is not repeatable under that constraint;
it is kept here only as corroborating evidence that the link is symmetric,
not as part of the recommended (one-way) configuration's evidence.

## Interpretation

- Steps 1-4 (BPSK, rising baud, same small payload) show essentially flat
  ~16 dB channel SNR up to 1200 baud — headroom was obviously unused, so
  scaling continued per the methodology.
- Step 5 is the real ceiling mechanism: at 2400 baud (RRC occupied
  bandwidth ≈ (1+0.35)×2400 ≈ 3240 Hz, centred on 1500 Hz) the signal spills
  past the 300-2700 Hz passband edges and SNR collapses to ~2 dB — this is
  a **hard bandwidth wall from the transceivers' SSB filtering**, not a
  gradual noise-floor effect. 2000 baud (occupied bandwidth ≈ 2700 Hz,
  almost exactly the full passband) is the practical edge; SNR there drops
  to ~8.4 dB even for BPSK.
- Modulation order scales for free up to the bandwidth wall as long as
  per-symbol SNR margin allows it: at 1200 baud (~16 dB SNR) BPSK/QPSK/8PSK
  all ran 5/5, and 16-QAM was the first order to show a crack (4/5) — 16-QAM
  needs several more dB of margin than 8PSK for the same error rate, and
  16 dB is right at that boundary.
- At the bandwidth-limited 2000-baud operating point, SNR is only ~8 dB,
  which is enough for QPSK but not enough for 8PSK (0/5, confirmed) or
  16-QAM. So the two constraints trade off: **you can have high baud with
  low-order modulation, or high-order modulation at a lower baud that fits
  in more SNR headroom — QPSK at 2000 baud is the point on that trade-off
  curve found here with the best throughput before failures started.**
- Every trial across every step measured a consistent, non-zero carrier
  frequency offset — about **-8 Hz on the IC-7300→IC-705 leg and +8 Hz on
  the reverse leg**. This is a fixed offset (not drift within a frame) and
  is almost certainly a small BFO/audio-pitch mismatch between the two
  radios' SSB demod chains, not a soundcard clock error. It is small
  enough that a coarse ±20 Hz / 1 Hz-grid acquisition search plus a
  preamble-phase-ramp refinement (both implemented in `sc.py`) handles it
  completely — but any future single-carrier or multi-carrier mode on this
  path MUST budget for a several-Hz fixed carrier offset per direction, or
  a frame that assumes zero offset will silently rotate its constellation
  across the frame and fail exactly the way the flagged OFDM modes did.

## Most important finding for future mode design

**The IC-7300→IC-705 audio path has a hard bandwidth ceiling around
2000-2200 Hz of occupied bandwidth (not a smooth SNR rolloff) and a
consistent several-Hz fixed carrier offset per direction.** Given that,
a single well-placed carrier with QPSK at ~2000 baud is a solid,
repeatable ~2900 bps link. This is consistent with, and helps explain, the
OFDM intermodulation failures seen earlier (`experiments/hc2_32qam`,
`experiments/hf4`, `experiments/path_probe`): driving many simultaneous
tones across the full passband simultaneously hits both the bandwidth edge
distortion and IMD from the SSB compressor/ALC, whereas one tone well
inside the passband is clean and predictable.

## Recommended configuration (superseded below — see "Round 2")

- **Modulation**: QPSK (2 bits/symbol)
- **Baud**: 2000 symbols/second
- **Carrier**: 1500 Hz, RRC beta=0.35
- **Net throughput**: ≈2900 bits/second (measured, 58 B payload/frame)
- **Evidence**: 10/10 decoded, both directions, SNR 7.4-8.9 dB
  (`logs/mode_sweeps/hf_singlecarrier-20260901T115115Z/result.json`)

---

## Round 2: pushing past QPSK@2000baud/~2900bps

Constraint for this round and after: IC-705 is receive-only. Every trial
below is IC-7300(TX) -> IC-705(RX) only; the `ba`/`both` direction options
were removed from `hardware_test.py` (it now only supports `ab`).

| Step | Baud | Beta | Mod (bps) | Payload | Frame (s) | Net throughput | Result |
|---|---|---|---|---|---|---|---|
| 12 | 2000 | 0.20 | BPSK (1) | 10 B | 0.10 | — | 3/3 (SNR ~7.4 dB, no gain over beta=0.35) |
| 13 | 2400 | 0.20 | BPSK (1) | 10 B | 0.08 | — | 0/3 (SNR ~2.5 dB — narrower beta did not save the bandwidth wall) |
| 14 | 1500 | 0.35 | BPSK (1) | 10 B | 0.13 | — | 3/3 (SNR ~15 dB) |
| 15 | 1500 | 0.35 | 8PSK (3) | 9 B | 0.07 | ~1043 bps | 5/5 (SNR ~15.6 dB) |
| 16 | 1500 | 0.35 | 16-QAM (4) | 10 B | 0.06 | — | 3/5 (SNR ~15.5 dB — worse than the 4/5 seen at 1200 baud; 16-QAM stays unreliable across this whole SNR range) |
| 17 | 1500 | 0.35 | 8PSK (3) | 57 B | 0.15 | ~2961 bps | 5/5 (SNR ~15.5 dB) |
| 18 | 2000 | 0.35 | QPSK (2) | 144 B | 0.33 | ~3470 bps | 5/5 (SNR ~8.9 dB) — apples-to-apples re-check of step 11's config at a bigger payload |
| 19 | 1500 | 0.35 | 8PSK (3) | 144 B | 0.31 | ~3728 bps | 5/5 (SNR ~15.5 dB) — beats step 18 at the same payload size |
| 20 | 1500 | 0.35 | 8PSK (3) | 294 B | 0.58 | ~4091 bps | 5/5 (SNR 15.1-16.3 dB) |
| 21 | 1500 | 0.35 | 8PSK (3) | 1194 B | 2.18 | — | 0/3, all crc_fail (SNR still 15-16 dB per preamble — this is **not** an SNR failure) |
| 22 | 1500 | 0.35 | 8PSK (3) | 594 B | 1.11 | — | 3/5 (marginal, same story) |
| 23 | 1500 | 0.35 | 8PSK (3) | 414 B | 0.79 | **~4198 bps** | **5/5** (SNR 15.3-16.4 dB) |
| 24 | 1500 | 0.35 | 8PSK (3) | 474 B | 0.90 | — | 4/5 (first crack — one step past the reliable edge) |

### What changed and why

- **Lower RRC beta did not help.** Beta 0.20 vs 0.35 made no measurable SNR
  difference at 2000 baud, and did not rescue 2400 baud (still ~2.5 dB,
  0/3). The 300-2700 Hz passband wall is a hard filter-response edge on
  the radios, not something a slightly tighter pulse shape works around.
- **1500 baud is a much better operating point than 2000 baud.** SNR jumps
  from ~8 dB to ~15 dB between the two (both divisors of the 12 kHz design
  rate; nothing in between is representable without off-grid resampling).
  That 7 dB of extra margin is enough to run 8PSK reliably (5/5 across
  every trial size), where at 2000 baud 8PSK failed outright (0/5,
  reported previously). 16-QAM, however, stayed unreliable at 1500 baud
  too (3/5) — it needs more margin than 15-16 dB gives it at this preamble
  length and equalizer design, consistent with the earlier 1200-baud
  result (4/5). Light FEC was considered for 16-QAM (per the task
  suggestion) but the arithmetic doesn't clear the bar here: a rate-3/4
  code on 16-QAM nets 3 effective bits/symbol, i.e. the same raw rate as
  uncoded 8PSK, which is already reliable — so coding 16-QAM could only
  ever tie 8PSK@1500, never beat it, and a higher-rate code (7/8) capable
  of beating it wasn't available off the shelf (`whale.dsp.fec` only
  offers a rate-1/2 K7 code) and building a punctured code was judged not
  worth it for this ceiling search. FEC was not implemented this round.
- **A new, previously invisible limit showed up once payloads got large
  enough to matter: frame-duration phase drift.** 8PSK@1500 baud is rock
  solid at 15-16 dB SNR right up until the frame itself gets long — it
  failed consistently at 2.18 s (0/3) and was marginal at 1.11 s (3/5),
  while the *preamble-measured* SNR stayed flat at ~15-16 dB the whole
  time. That rules out noise or bandwidth as the cause: `sc.py`'s channel
  model estimates one carrier-frequency offset and one complex gain from
  the preamble and holds both fixed for the rest of the frame, so any
  slow phase drift over the following ~1-2 seconds (independent frequency
  wobble in each radio's own audio chain, not audible until integrated
  over that long) silently rotates the constellation and blows the CRC
  well before it would show up as a preamble SNR drop. Backing off to a
  ≤0.8 s frame (414 B payload) restored 5/5; 0.90 s (474 B) already shows
  one failure in five.

## Recommended configuration (final)

- **Modulation**: 8PSK (3 bits/symbol)
- **Baud**: 1500 symbols/second
- **Carrier**: 1500 Hz, RRC beta=0.35
- **Frame budget**: ≤~0.8 s total airtime (414 B payload demonstrated)
- **Net throughput**: **≈4200 bits/second**, measured
- **Evidence**: 5/5 decoded, IC-7300(TX)->IC-705(RX) only, SNR 15.3-16.4 dB
  (`logs/mode_sweeps/hf_singlecarrier-20260901T143642Z/result.json`)
- **What limits it from going further**: not SNR (15-16 dB, plenty of
  margin) and not raw bandwidth (1500 baud sits comfortably inside the
  2000-2200 Hz occupied-bandwidth wall) but **frame-duration phase
  drift** — this single-tap-channel design only estimates carrier phase
  once, from the preamble, so frames longer than ~0.8-0.9 s accumulate
  enough uncorrected phase rotation to fail the CRC even though the link
  itself has margin to spare. The natural next lever, if pushed further,
  is a mid-frame pilot (or continuous decision-directed phase tracking)
  to relax that ceiling and let much longer, more preamble-amortized
  frames run at 8PSK@1500 baud or higher order.

---

## Round 3: mid-frame pilots to fix the phase-drift ceiling

Added to `sc.py`: a short (15-chip) known BPSK pilot block, PN-seeded
independently of the preamble, inserted every `pilot_interval` data
symbols (a new `SingleCarrierMode.pilot_interval` field; 0 = old
behaviour, disabled). At RX, each pilot block gives one more (time,
complex-gain) anchor in addition to the preamble; gain for every data
symbol between two anchors is linearly interpolated (independently for
real/imaginary parts) between them, instead of holding the preamble's
single estimate fixed for the whole frame. Structurally independent of
`whale.dsp.equalize` (only looked at for the general technique, per the
task) — `sc.py`'s frame format, PN sequence, and interpolation are its
own. Constraint respected throughout: IC-7300(TX)->IC-705(RX) only.

Simulation first (channel simulator, not hardware) to confirm the
mechanism before spending airtime: injected a synthetic 1 Hz/s linear
frequency drift (on top of a fixed 3 Hz offset) into a 1.1 s 8PSK@1500
frame. Without pilots (`pilot_interval=0`) the frame decoded to `crc_fail`
exactly as seen on real hardware; with `pilot_interval=150` the same
drift decoded cleanly. That gave confidence to go straight to the
previously-marginal/failing real frame lengths rather than re-walking the
whole sweep from scratch.

| Step | Baud | Mod | Pilot interval | Payload | Frame (s) | Net throughput | Result |
|---|---|---|---|---|---|---|---|
| 25 | 1500 | 8PSK | 150 symbols (~0.1 s) | 594 B | 1.22 | ~3899 bps | 5/5 (SNR ~19.5 dB) — same payload that was 3/5 in step 22 without pilots |
| 26 | 1500 | 8PSK | 150 | 1194 B | 2.40 | ~3980 bps | 5/5 (SNR ~19.4-20.0 dB) — same payload that was 0/5 in step 21 without pilots |
| 27 | 1500 | 8PSK | 150 | 1995 B | 3.96 | ~4032 bps | 5/5 (SNR 14.0-20.2 dB) |
| 28 | 1500 | 8PSK | 150 | 2994 B | 5.92 | **~4049 bps** | **5/5** (SNR 15.0-15.5 dB) |
| 29 | 1500 | 16-QAM | 150 | 394 B | 0.64 | — | 2/5 (SNR 14.3-18.8 dB) — pilots do **not** rescue 16-QAM; confirms that failure mode is constellation-margin/SNR, not phase drift |

Step 28's frame keys the radio for ~6.1 s (+1 s capture tail ≈ 7.1 s total),
which is the point scaling was stopped: it is comfortably inside
`RX_BUFFER_SECONDS` (10 s) but close enough to the "well under ~8 s"
single-frame guidance that going materially longer stops being a fair
test of the channel and starts being a test of the capture buffer.
Nothing in steps 25-28 shows a real-hardware failure or SNR decline with
frame length — the SNR bounces around 14-20 dB across all of them with no
downward trend — so the phase-drift ceiling found in round 2 looks fully
removed within the range tested, not just pushed out further.

### Interpretation

- **Pilot tracking fully fixed the phase-drift limit for 8PSK**, at least
  up to the ~6 s frame lengths tested (a >7x increase over the round-2
  ceiling of ~0.8 s). Frames that previously failed outright (2.18 s,
  0/5) or were marginal (1.11 s, 3/5) are now 5/5 with no sign of running
  out of room.
- **Net throughput went up modestly, not dramatically**, from ~4200 bps
  (round 2's optimum, a small 414 B frame with pilots off) to **~4050
  bps sustained at nearly 6 s of payload** (round 3). The two numbers
  aren't in tension: round 2's ~4200 bps was the peak of a narrow sweet
  spot right before the drift wall hit; round 3 trades a little of that
  peak for a design that holds a similar rate over payloads ~7x larger,
  which is a materially more useful operating point for real traffic
  (fewer, bigger frames instead of one precisely-tuned burst size). The
  ~150 bps gap is pilot overhead (15 symbols out of every 150+15, i.e.
  ~9% of symbols spent on pilots rather than data) plus interpolation
  edge effects on shorter frames.
- **16-QAM's failure is orthogonal to phase drift.** Adding pilots to a
  16-QAM frame short enough that round-2 already showed drift wasn't the
  cause (0.64 s) did not change its ~2-4/5 success rate. 16-QAM needs
  more constellation margin than this channel's ~15-16 dB gives it,
  independent of tracking quality — confirming the round-2 conclusion
  that FEC, not phase tracking, is the only lever left for 16-QAM.

## Recommended configuration (round 3, current)

- **Modulation**: 8PSK (3 bits/symbol)
- **Baud**: 1500 symbols/second
- **Carrier**: 1500 Hz, RRC beta=0.35
- **Pilot interval**: 150 data symbols (15-chip BPSK pilot block, ~9%
  overhead)
- **Frame**: up to ~5.9 s tested (2994 B payload) with no sign of failure;
  not pushed further to stay well clear of the 10 s RX capture buffer
- **Net throughput**: **≈4050 bits/second sustained** over multi-second
  frames (vs. ≈4200 bps in a single tuned ~0.8 s burst without pilots, or
  ≈2900 bps at the original QPSK@2000baud config)
- **Evidence**: 5/5 decoded at each of four frame lengths from 1.2 s to
  5.9 s, IC-7300(TX)->IC-705(RX) only, SNR 14-20 dB throughout
  (`logs/mode_sweeps/hf_singlecarrier-20260901T144539Z/result.json` for
  the largest, 2994 B, frame)
- **What still limits it**: not phase drift (fixed by pilots) and not SNR
  for 8PSK (14-20 dB throughout, no downward trend even at ~6 s) — the
  practical stopping point here was the receive capture-buffer safety
  margin, not a real-hardware failure. 16-QAM remains the next
  modulation-order ceiling, blocked by raw constellation SNR margin
  (~15-16 dB available, more needed), which pilot tracking does not
  address — FEC (not attempted this round; see round 2's rate-arithmetic
  note) is the remaining lever there.
