# HF3 retained-direction hardware campaign -- 2026-09-02

## Scope and result

HF3 decoded 40/40 full-capacity physical frames byte-for-byte from the
IC-7300 (station A, transmitter) to the IC-705 (station B, receiver). All 40
captures were retained. The 95% Wilson interval for the observed decode rate
is 91.2--100%, and decoder confidence ranged from 0.976 to 0.984. There were
no error outcomes.

This first campaign numerically clears the 40-frame retained-direction
hardware gate, but it started from a dirty tree. A clean-state repeat was run
after committing the workspace and fixing the runner to capture Git state
before writing its own artifacts. That repeat delivered 36/40 frames and does
not clear the reliability bound, so the retained-direction hardware gate is
currently **failed**, not passed. The earlier 40/40 remains useful
characterization evidence but must not be selected over the predeclared clean
repeat.

## Station setup

The operator reported the following after the run:

- IC-7300 transmitted at 30 W into a small antenna.
- IC-705 received with a dummy load connected.
- Operating frequency was 10.147 MHz.
- Both radios were in USB-D mode.
- Both used the default USB-D filter, 0--3,000 Hz.
- Operator audio levels were approximately 70% on both radios.
- ALC and AGC used their default settings; the attenuator and preamp were off.
- Direction: IC-7300 to IC-705 only; the IC-705 was never keyed.

Per the operator, firmware versions and cabling are intentionally not retained
for this campaign and are not treated as required qualification metadata.

## Command and artifacts

```powershell
python scripts/sweep_modes.py --channel hf-ssb --mode-level experimental --modes hf3 --direction ab --a ic7300 --b ic705 --trials 40 --capture all --required-rate 0.9 --output-dir logs/mode_qualification/hf-ssb/hf3/2026-09-02-hardware/retained-40-ic7300-to-ic705
```

The result and per-trial decoder metrics are in
`retained-40-ic7300-to-ic705/result.json`; its `captures/` directory contains
all 40 compressed audio/payload captures. The recorded Git commit is
`8c42004f3fdb954cef6a006431a41214fcdbaeb`, and the tree was dirty.

The reported 1,907 bit/s is production DATA-chunk bits divided by keyed time.
It is a direct-frame diagnostic, not application/session throughput: this
campaign bypassed ACKs, retries, turnaround, connection, and disconnect.

## Clean-state repeat

The repeat ran from commit
`320d9c1c596b6a54a3248d8b73d8aaec1484f50f`; the corrected provenance field
records `git_dirty: false`. It used the same declared direction, settings,
seed, 40 deterministic payloads, and capture policy as the first campaign.

HF3 decoded 36/40 frames. All 40 frames acquired with high confidence, but
trials 4, 11, 20, and 38 failed CRC validation. Those failures reproduce when
their retained captures are decoded offline. The delivery Wilson interval is
76.9--96.0%, equivalent to a 95% FER upper bound of 23.1%, above the 10%
ceiling. Direct-frame useful throughput was 1,717 bit/s.

Artifacts are under `clean-retained-40-ic7300-to-ic705/`. This campaign is the
current qualification result; another run must not replace it merely to seek
a favorable sample.
