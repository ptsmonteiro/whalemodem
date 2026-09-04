# HR1-B small real-receiver boundary screen

## Decision

**Redesign before the full campaign.** Preserve HR1-B's full-frame result as
evidence that the guarded GF(16)+RS physical layer is promising, but redesign
the tiny ACK/framing/ARQ budget and the frame duration before doing
`PLAN.md`'s statistical campaign. Do not register HR1-B in production.

The full class has a real-receiver AWGN transition between -24 and -23 dB and
has no high-SNR impairment floor in the three requested disturbed Watterson
presets. It delivered 30/30 exploratory frames at -19 dB disturbed mid, -17
dB disturbed NVIS, and -20 dB disturbed high. The tiny class is not reliable
at those same points (15/30, 19/30, and 7/30), so retry-weighted stop-and-wait
throughput is far below the fixed 20 bit/s Level 0 target. Independently, the
19.54 s full frame cannot fit the current production 10 s RX buffer or the HF
policy's 8 s useful-frame budget.

This is a small screen, not an operating envelope or qualification result.
No receiver parameter was changed.

## Reproducible contract

All runs used `/Users/pedro/miniconda3/envs/gnuradio/bin/python`, master seed
`20260831`, four worker processes, and the experiment-local matched benchmark.
The aggregate machine-readable index is
[`results/hr0b_boundary_screen_20260831.json`](results/hr0b_boundary_screen_20260831.json).
Every individual artifact retains its command, derived seeds, per-trial
result, channel description, timing, decoder work, and replay command.

SNR is repository `SnrSpec(WAVEFORM)`: signal mean square over the complete
half-open keying `[0, tx_samples)`, including lead, guards, body, and tail;
real AWGN occupies the complete 0--24 kHz Nyquist band at 48 kHz. Canonical
Watterson means independent per-frame fading followed by AWGN normalized from
that frame's post-Watterson signal power. It is not fixed N0 or continuous
fading. A 3 kHz white-noise SNR is analytically 9.03 dB above the reported
waveform SNR.

The full workload is the frozen 64-byte physical frame (10-byte checked air
header plus 54-byte DATA body), 937,920 samples and 19.540 s. The tiny workload
is a 12-byte physical ACK-class frame (10-byte air header plus two ACK body
bytes), 175,296 samples and 3.652 s.

## Full-class screen

Acquisition and checked-body delivery are deliberately separate:

| Channel | Waveform SNR | Acquisition | Checked delivery | Body failures |
| :--- | ---: | ---: | ---: | ---: |
| AWGN | -25 dB | 30/30 | 0/30 | 30 |
| AWGN | -24 dB | 30/30 | 17/30 | 13 |
| AWGN | -23 dB | 30/30 | 30/30 | 0 |
| AWGN | -22 dB | 30/30 | 30/30 | 0 |
| disturbed mid | -19 dB | 30/30 | 30/30 | 0 |
| disturbed mid | -20 dB | 30/30 | 22/30 | 8 |
| disturbed mid | -21 dB | 30/30 | 12/30 | 18 |
| disturbed NVIS | -17 dB | 30/30 | 30/30 | 0 |
| disturbed NVIS | -18 dB | 30/30 | 28/30 | 2 |
| disturbed NVIS | -19 dB | 30/30 | 21/30 | 9 |
| disturbed high | -19 dB | 30/30 | 30/30 | 0 |
| disturbed high | -20 dB | 30/30 | 30/30 | 0 |
| disturbed high | -21 dB | 30/30 | 7/30 | 23 |

The required 10-trial +20 dB floor checks were 10/10 in all three presets.
Ten-trial scouts at 0, -10, -15, and -20 dB, followed by one-dB bracketing,
found no coherent-pair impairment ceiling. The exploratory full-frame edges
are therefore between -19/-20 dB disturbed mid, approximately -17/-18 dB
disturbed NVIS under a strict 30/30 screen convention, and -20/-21 dB
disturbed high. Thirty trials cannot establish a qualification boundary:
even 30/30 has only a 0.886 Wilson 95% lower bound.

Compared only as exploratory repository simulations, HR1-B's disturbed-mid
30/30 point at -19 dB is 8 dB below HC0's last 30/30 point at -11 dB. HC0
also had a checked-body structural floor in both 7 ms presets at +30 dB,
whereas HR1-B crossed them near -17 and -20 dB. This strongly supports the
guarded full-class geometry, but it is not a radios or VARA comparison.

## Tiny ACK and retry-weighted throughput

The tiny class transitions about 1 dB above the full class in AWGN:

| Channel | SNR | Full DATA | Tiny ACK |
| :--- | ---: | ---: | ---: |
| AWGN | -23 dB | 30/30 | 17/30 |
| AWGN | -22 dB | 30/30 | 30/30 |
| disturbed mid | -19 dB | 30/30 | 15/30 |
| disturbed NVIS | -17 dB | 30/30 | 19/30 |
| disturbed high | -20 dB | 30/30 | 7/30 |

Let `pD` and `pA` be independent DATA and ACK checked-delivery probabilities
and `q = pD*pA`. One clean exchange takes
`19.540 + 3.652 + 2*0.300 = 23.792 s`, so its clean projection is only
`432/23.792 = 18.157 bit/s`.

Two retry models reach the same decision. Even perfect delivery (`q = 1`)
tops out at 18.157 bit/s, so neither model can meet the fixed 20 bit/s Level 0
target:

1. An optimistic model charges every attempt only one complete clean exchange:
   `R = 432*q/23.792`.
2. A model using current production HF timeout arithmetic charges 48.332 s
   for a failed attempt: the 19.540 s DATA transmission followed by a 28.792 s
   timeout (`DATA + ACK + two turnarounds + 5 s slack`). Its unlimited-retry
   expectation is
   `R = 432 / (23.792 + ((1-q)/q)*48.332)`.

Using the empirical point estimates, the optimistic/current-timeout rates are
10.29/7.11 bit/s at AWGN -23 dB, 9.08/5.99 in disturbed mid at -19 dB,
11.50/8.34 in disturbed NVIS at -17 dB, and 4.24/2.37 in disturbed high at
-20 dB. AWGN -22 dB projects 18.157 bit/s only because both small samples are
30/30; that leaves no measured retry margin, and 30 trials cannot demonstrate
the required 99.57% exchange reliability. The tiny ACK/session gate fails.

## Integrity, deterministic work, and host timing

Across all 1,050 retained full/tiny scout and key-point trials, acquisition
was above threshold in 1,050/1,050. There were 713 checked deliveries, 337
checked-body failures, zero errors, and zero wrong payloads. Acquisition at
these signal-present points is therefore not the limiting stage. The low
provisional threshold still has no held-out absent-window campaign.

The receiver stayed inside its declared bounds: 54,054 coarse cells per
trial, at most 16 candidates, at most 3,120 refinement cells, and at most
12,713,984 accumulated GF(16) branch metrics. On the Apple M1/8 GiB
development host with four concurrent workers, successful full decodes had
0.524 s p95 wall / 0.472 s p95 process CPU; failed full decodes had 2.196 s
p95 wall / 2.092 s p95 CPU. Tiny success/failure wall p95 was 0.377/1.389 s.
The worst observed full wall time was 2.377 s. These are comfortably below
frame duration on this host, but they are not idle-search, 30-minute-session,
or Raspberry Pi measurements.

Fifty-seven representative failed captures were saved. Replay of full AWGN
artifact record 0 and disturbed-high tiny artifact record 0 reproduced their
`payload_failed` outcomes and exact seed identities. Every other trial also
contains a one-command deterministic replay.

## Production timing and buffer check

HR1-B remains non-integrable without a deliberate production redesign:

- `whale.transport.RX_BUFFER_SECONDS` is 10.0 s (120,000 samples at 12 kHz),
  while the largest received full capture here was 234,580 samples, 19.548 s.
  A production snapshot would discard roughly the first 9.55 s before the
  frame completed.
- `HF_SSB.max_useful_frame_seconds` is 8.0 s. HR1-B's 19.540 s full keying
  exceeds it by 11.540 s, although HR1-B's experiment-local decoder permits a
  bounded 24 s capture.
- The production decode loop polls every 0.15 s and tries all candidate modes.
  This screen measured one candidate on completed captures, not the idle and
  partial-buffer cost of adding it to that loop.

Blindly increasing the shared buffer and policy cap would change production
latency, pruning, memory, and retransmission behavior. The next revision
should instead treat frame duration and ACK protection as joint design
constraints, then rerun this screen before any integration.

## Handoff

The full PHY result is worth retaining, and the target should not be weakened.
The next point is a bounded **framing/ARQ redesign**: give ACKs enough
protection or use an explicit repeated/incremental ACK policy with measured
airtime, and make the full transfer streamable or split it into frames that
fit an explicitly revised production buffer/timing contract. Recompute
retry-weighted session rate before accepting any candidate. Only then repeat
the small AWGN/Watterson screen.

Final campaign decision: **REDESIGN**. Do not run the full all-preset,
fixed-N0, continuous-fade, absent-window, interference, session, minimum-host,
or radio campaign on HR1-B revision B.
