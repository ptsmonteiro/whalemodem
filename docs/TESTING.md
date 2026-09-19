# Testing and qualification

Whale uses progressively more realistic checks: deterministic unit
tests, full-stack paired audio, simulated channels, recorded radio captures,
and finally retained-direction hardware frame trials plus bidirectional
hardware sessions. A mode is not promoted solely
because it passes a clean software loopback.

Install the test extra in the project's virtual environment before running
these commands:

```console
python -m pip install -e ".[test]"
```

## Automated suite

Run all ordinary tests:

```console
python -m pytest -q
```

Important focused groups include:

```console
python -m pytest tests/test_audio_e2e.py -q
python -m pytest tests/test_mode_qualification.py -q
python -m pytest tests/test_hc0_mode.py tests/test_hc1w_mode.py -q
python -m pytest tests/test_ptt_backends.py tests/test_ptt_safety.py -q
```

`tests/test_audio_e2e.py` replaces only the sound cards. It exercises two
station servers, modem services, the TCP command/data interface, ARQ,
negotiation, adaptation, and real modulation/demodulation in both directions.

## Simulated channels

The bounded fixed-seed channel matrix is marked separately:

```console
python -m pytest -m channel_regression -q
```

For explicit Monte Carlo measurements:

```console
python scripts/benchmark_simulated_channels.py --model fm --policy fm \
  --points 5 10 15 20 25 30 --trials 100
```

[MODES.md](MODES.md) is the maintained summary of shipped-mode results.

For a new or changed FM mode, the two baseline checks are a bench radio test
(`scripts/hw_smoke_single_frame.py` or `scripts/hw_smoke_link.py` over a real
FM pair) and a simulated flat FM C/N sweep with
`scripts/benchmark_simulated_channels.py --model fm --policy fm
--fm-profile flat_nbfm`. Record the resulting C/N floor as a comment in the
mode's module docstring, alongside the MODES.md entry.

## Capture replay

Committed recordings preserve behavior that clean synthesis cannot exercise:

```console
python -m pytest tests/test_hc0_capture_replay.py -q
```

Capture replay establishes reproducibility against a recorded path. It does
not substitute for a promotion-sized live retained-direction campaign: one
recording represents one radio pair, configuration, propagation state, and
point in time.

## Hardware progression

After the automated suite passes, use this order:

1. One frame in each direction with `scripts/hw_smoke_single_frame.py`.
2. A complete link without TCP using `scripts/hw_smoke_link.py`.
3. Every offered rung with `scripts/sweep_modes.py --channel CHANNEL`.
4. The full acceptance scenario using `scripts/run_acceptance_test.py`.

Hardware setup and safety notes are in [HARDWARE.md](HARDWARE.md).

## Acceptance scenario

The acceptance test starts from two already-running station servers. It
connects A to B, sends the requested payload A → B, reverses direction, then
disconnects. Both payloads are checked byte-for-byte. It reports elapsed time
and net useful-application throughput; protocol overhead and retransmitted
bytes are not counted as useful throughput.

```console
python acceptance_test.py \
  --a-cmd 8300 --a-data 8301 --b-cmd 8310 --b-data 8311 \
  --a-call STA1 --b-call STA2 --size 1024
```

`whale-test` runs the same scenario between two configured stations with no
server to start and no ports to choose: the answering station runs
`whale-test`, the calling station runs `whale-test CALLSIGN`. It takes
`--channel` and `--config` and nothing else. It starts `whale-server` itself,
as a separate process on loopback ports it picks, and drives it over TCP as
any client application would; the report's keying, SNR and mode counts are
the status lines that modem sent on its command port. Before transmitting --
and before that process is started -- it checks that the configuration loads,
a callsign and a radio for the channel are configured, and the radio's audio
devices exist and accept whale's format; a problem there exits 2. It asks for
one confirmation, then always writes
`whale-report-<callsign>-<timestamp>.txt` and prints its path. Exit status
is 0 for a pass and 1 for an on-air failure. Ctrl-C stops the run at any
point: it ends a session still up with a parting DISC, closes the client and
asks the modem process to stop, which unkeys the radio, and still writes the
report for the part that ran. A second Ctrl-C, or a goodbye unanswered within
the grace period, skips the DISC and takes the radio down immediately.

`whale-test --sweep` measures the same path one waveform mode at a time
instead of running a session: the listening station runs `whale-test
--sweep`, the driving station `whale-test --sweep CALLSIGN`, and both need
the flag because the listener needs the raw receive stream. For each mode,
walking up from the control mode and stopping after two modes in a row
decode nothing, the driver announces the mode at the control mode, keys once
for a burst of sequence-numbered frames, and collects the far end's tally:
frames decoded, frames that synchronised and failed, frames never seen, and
the receive SNR of each decoded frame. The roles then swap, so both
directions are measured. It uses the same preflight, the same confirmation
and the same report file, with a per-mode table added. A mode that decodes
nothing is a result, not a failure: the run exits 0 when it measured the
path, 1 when the far end never answered at the control mode or the operator
stopped a sweep part way, and 2 when the station could not be brought up.
The SNR figures are whale's own estimate -- useful for ranking modes on one
path, not a lab measurement and not comparable between differently
configured stations.

## Characterization and promotion

Characterization scripts should answer a reproducible question rather than
serve as an implicit product default. The common direct-frame hardware method
lives in `scripts/bench.py`; receiver CPU measurement lives in
`scripts/benchmark_rx.py`; session benchmarking lives in
`scripts/benchmark_sessions.py`.

For an individual mode, the documented throughput is the net application
DATA chunk carried by one full-capacity frame divided by that encoded frame's
airtime. Session throughput reported by acceptance and transfer benchmarks
includes protocol timing effects and is useful system evidence, but it is not
the mode throughput gate.

Experiment directories retain implementation notes and raw result links for
waveform candidates. Their `RESULTS.md` files are evidence records, not a
replacement for the current qualification manifest.
