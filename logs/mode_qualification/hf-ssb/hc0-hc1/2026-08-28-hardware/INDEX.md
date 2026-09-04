# HC0/HC1 bidirectional hardware captures, 2026-08-28

IC-705 <-> IC-7300 bench pair. Files were originally written straight into
`logs/` and were gitignored scratch until the 2026-08-30 log reorganization
promoted them here as retained evidence and gave them this INDEX.md; the
capture files themselves are unchanged. Each `.json` summary pairs with a
`.bin`/`.npy` capture directory of the same trials (file naming:
`<direction>_<NN>.bin`/`.npy`).

## HC0

- `hc0_both.json` + `hc0_both/`: 5 trials each direction (ic7300->ic705,
  ic705->ic7300). All 10 decoded exactly.
- `hc0_weakleg.json` + `hc0_weakleg/`: 6 trials, ic705->ic7300 only (the
  weaker-measured leg). All 6 decoded exactly.

Together: 11/11 ic705->ic7300, 5/5 ic7300->ic705. Both directions have exact
saved-capture replay, but the largest single batch (6) is below the 100-trial
promotion minimum in `MODE_QUALIFICATION.md` -- this is provisional evidence,
not a qualifying frame Monte Carlo or hardware gate result.

## HC1

- `hc1_first.json` (no capture directory -- summary only): 3/3 decoded
  ic7300->ic705; 3/3 failed ic705->ic7300, all `"header not found"`
  (acquisition failure, not payload/CRC).
- `hc1_confirm_ab.json` + `hc1_captures/` (10 files): 10/10 decoded
  ic7300->ic705. No ic705->ic7300 trials in this batch.
- `hc1_confirm_ack.json` + `hc1_captures_ack/` (4 files): 4/4 decoded
  ic7300->ic705. No ic705->ic7300 trials in this batch.

Together: 17/17 ic7300->ic705, 0/3 ic705->ic7300. HC1 has exact saved-capture
replay in only one direction; the other direction failed every attempted
trial at acquisition. Under the current retained-direction criterion,
ic7300->ic705 is HC1's better usable retained leg. Its 17/17 result is
provisional because it is below the 40-frame minimum.

## 2026-08-30 operator note on the weak ic705->ic7300 leg

The operator recalls that during this session the IC-705 was connected to a
dummy load rather than an antenna, so it radiated very little power into the
ic705->ic7300 path. This is recollection, not a recorded configuration
field -- no antenna/power setting was retained with the run (see below) --
but the captured metrics are consistent with it and with nothing else
obvious:

- HC1 ic705->ic7300: `present_carriers` 0 on all 3 trials, confidence
  0.20-0.29, failure `header not found`. No signal detected at all, rather
  than a marginal or partially-acquired one.
- HC0 ic705->ic7300: decoded 11/11, but with confidence 0.38-0.42 and
  nonzero raw BER (0.0018-0.0027) in `hc0_both.json`, against confidence
  0.49-0.50 and zero raw BER on ic7300->ic705 in the same batch.

A one-directional level deficit that HC0's redundancy margin absorbs while
HC1 does not acquire is the expected ladder behaviour under a large path
loss, and matches the simulated finding that HC0 covers conditions HC1's
envelope excludes.

The consequence for qualification is that this session does not measure
HC1's declared envelope at all: the weak leg was not a channel condition
under test but a bench misconfiguration. The 0/3 result is therefore treated
as an invalid characterization run rather than a failed retained direction.
It remains documented but does not enter the retained ic7300->ic705 leg's
Wilson interval or verdict. HC1's retained-direction hardware frame gate is
`provisional` pending at least 40 frames with the transmit configuration
recorded. HC0's 11/11 on the weak leg remains valid historical evidence.

## Missing configuration metadata

Neither mode has a retained artifact recording the transmit/receive
configuration (radio settings, audio levels, antenna/path) beyond what each
JSON's per-trial fields capture, so this evidence cannot yet support a
complete promotion record on its own; see `LOGS.md` for what a full
qualification artifact requires going forward. This session is the concrete
case for that requirement: had the antenna/dummy-load state been recorded,
the weak leg would have been identifiable from the artifact instead of from
recollection two days later.
