# Testing and qualification

whalemodem uses progressively more realistic checks: deterministic unit
tests, full-stack paired audio, simulated channels, recorded radio captures,
and finally bidirectional hardware trials. A mode is not promoted solely
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
python -m pytest tests/test_hc0_mode.py tests/test_hc1_mode.py -q
python -m pytest tests/test_vf3_mode.py -q
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
python scripts/benchmark_simulated_channels.py --model fm --policy vhf-fm \
  --points 5 10 15 20 25 30 --trials 100
```

[CHANNELS.md](../CHANNELS.md) defines the channel contract, presets, SNR
conventions, and trial-result schema. Keep reported results tied to their
seed, channel parameters, mode, payload, and trial count.

## Capture replay

Committed recordings preserve behavior that clean synthesis cannot exercise:

```console
python -m pytest tests/test_hc0_capture_replay.py -q
python -m pytest tests/test_hc1_capture_replay.py -q
python -m pytest tests/test_vf3_capture_replay.py -q
```

Capture replay establishes reproducibility against a recorded path. It does
not substitute for trials on both live directions: one recording represents
one radio pair, configuration, propagation state, and point in time.

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

## Characterization and promotion

Characterization scripts should answer a reproducible question rather than
serve as an implicit product default. The common direct-frame hardware method
lives in `scripts/bench.py`; receiver CPU measurement lives in
`scripts/benchmark_rx.py`; session benchmarking lives in
`scripts/benchmark_sessions.py`.

The complete promotion gates—including malformed input, channel regression,
Monte Carlo sweeps, full-stack recovery, bidirectional hardware, adjacent-rung
overlap, throughput, CPU, memory, and required artifacts—are specified in
[MODE_QUALIFICATION.md](../MODE_QUALIFICATION.md).

Experiment directories retain implementation notes and raw result links for
waveform candidates. Their `RESULTS.md` files are evidence records, not a
replacement for the current qualification manifest.
