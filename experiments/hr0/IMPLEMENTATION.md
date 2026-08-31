# HR0-A point-3 implementation handoff

> **Point-4 update (2026-08-31):** the capacity diagnosis requested below is
> complete in [`REDESIGN.md`](REDESIGN.md).  HR0-A remains frozen in `hr0.py`
> as the control.  The experiment-local, wire-incompatible HR0-B candidate is
> implemented in `hr0b.py`; its retained bounded screen delivered 19/20
> oracle-aligned frames at -24 dB and projects 18.157 bit/s for a clean full
> DATA/tiny-ACK exchange.  The current handoff is go only to a small
> real-receiver boundary screen, not the full campaign or production.

## Status and decision

The standalone HR0-A full-frame experiment is implemented in [`hr0.py`](hr0.py)
and is loadable by the matched benchmark as
`experiments.hr0.hr0:HR0`. It is deliberately absent from `whale/`, the HF
registry, negotiation, and the production mode-ID space.

**Redesign before the full boundary campaign.** The bounded receiver, checked
wire format, offset acquisition, deterministic work limits, and occupied
bandwidth gate are working. However, a deliberately tiny oracle-aligned AWGN
smoke probe did not approach the frozen -24 dB target: with five seeds per
point, the last-two-observation receiver delivered 0/5 at -22, -21, -20, and
-19 dB and 5/5 at -18 dB. The all-three clean-AWGN oracle delivered 0/5 at
-22 through -20 dB, 4/5 at -19 dB, and 5/5 at -18 dB. These samples are far
too small to declare an SNR boundary, but they are enough to trigger
`DESIGN.md`'s oracle stop rather than spend a full Monte Carlo campaign on
this exact revision.

The point-4 design work measured soft-information capacity and tested a
different energy allocation at the same session-rate budget; it confirmed
that merely replacing the binary convolutional code cannot close the gap. At
-24 dB
the five last-two oracle samples had 38.7--41.6% hard coded-bit error and only
3--5 correct pilots of 13 (chance is 0.8125). That does not yet satisfy
`DESIGN.md`'s condition that uncoded tone metrics support a stronger decoder.
The bounded screen rejected A's target rate and produced the HR0-B revision
described in `REDESIGN.md`; the full campaign remains deferred. The guarded
geometry remains useful as the control: clean acquisition works with
timing offsets, +/-143 Hz tested CFO, and +/-100 ppm tested sample-clock error,
and its measured complete-keying 99%-power width is about 1,923 Hz. Do not
claim the -24 dB target, an operating envelope, HC0 superiority, or VARA
parity from this implementation.

## Exact full-class wire format

All multibyte integers are big-endian. The implemented class is fixed at a
64-byte maximum physical payload (10 checked air-header bytes plus a 54-byte
DATA body at the eventual link boundary).

1. Construct a 70-byte checked packet:
   `[uint16 length][0..64 payload bytes][CRC32(payload)][zero fill]`.
2. Convert the complete 70 bytes to MSB-first bits and XOR all 560 bits with
   the repository order-17 PN sequence seeded by `0x0C4B1`.
3. Append eight zero inputs: six terminate the K=7 encoder and two are
   state-zero alignment padding. A checked decode requires all eight to be
   zero.
4. Encode the resulting 568 bits with the non-systematic rate-1/3 K=7 code,
   octal generators `(171, 133, 165)`. For each input, the register update and
   oldest/newest-bit convention are the same as `whale.dsp.fec`: shift the
   previous six-bit state left and place the new bit in bit zero. This yields
   1,704 coded bits.
5. Apply the multiplicative interleaver
   `air_bit[i] = coded_bit[(i * 851) mod 1704]`.
6. Group the result MSB-first into 426 four-bit labels. Label `i` is XORed
   with PN nibble `i` from seed `0x0A6D1 XOR 2`; the result is Gray-mapped to
   one of 16 tone indices.
7. Insert a known pilot after each complete run of 31 data symbols, except
   after the terminal run. The 13 pilots use tone indices 0 through 12. The
   body is therefore 439 symbols.

The checked decoder rejects an impossible length, CRC mismatch, non-zero
packet fill, or non-zero termination/pad. CRC32 covers only the decoded
payload, matching the repository `PacketCodec` convention. The zero-fill and
eight-bit termination checks are stricter than production `PacketCodec` and
are intentional false-frame defenses for this experiment.

## Audio frame and signaling

The default complete keying is exactly 604,992 samples at 48 kHz, or 12.604
seconds:

| Segment | Samples / symbols | Time |
| :--- | ---: | ---: |
| Experimental lead | 6,144 samples | 0.128 s |
| Full-class word | 80 guarded symbols | 1.920 s |
| Checked body | 439 guarded symbols | 10.536 s |
| Zero tail | 960 samples | 0.020 s |

The experiment uses one real tone at a time at 375, 500, ..., 2,250 Hz with
amplitude `0.13 * sqrt(2)`. An 8 ms observation is 384 TX samples; one 24 ms
coded symbol repeats the same tone over three observations. The real receiver
discards the first observation as the delay guard and noncoherently sums the
last two. The aligned diagnostic can additionally sum all three to expose the
clean-AWGN guard cost.

Each class word is five permutations of all 16 tones. For every block, 17-bit
sort keys come from one continuing order-17 PN sequence; tone number breaks
ties. Seeds are `0x15201`, `0x15202`, and `0x15203` for tiny, short, and full.
Only the full body is implemented, but all three words are recognized so a
wrong class is rejected without trying a full decoder.

## Real acquisition and bounded work

`HR0.decode()` accepts only finite one-dimensional 12 kHz audio. It considers
at most the first 16 seconds and searches class-word starts only in the first
2 seconds. For a full-length capture, the fixed coarse grid is:

- 1,001 start cells at 2 ms spacing;
- 27 CFO cells at 15.625 Hz spacing from -203.125 to +203.125 Hz; and
- three class words.

That is exactly 81,081 coarse `(start, CFO, class)` cells. A 768-point
zero-padded FFT supplies the CFO grid for each 96-sample observation. At most
24 local maxima per class/CFO plane enter the raw ranking. Adjacent timing/CFO
cells are deduplicated and at most 16 candidates survive. Only full-class
candidates are refined or body-decoded.

Fine timing uses 0.25 ms cells across +/-8 ms, and CFO checks the coarse value
and +/-7.8125 Hz. This is at most 195 refinement cells per tried candidate.
Each complete candidate body performs 14,048 tone correlations and at most
72,704 Viterbi branch comparisons. With 16 candidates, input length, coarse
search, refinement, tone-bank, and Viterbi work all have hard finite bounds.

The result and benchmark artifact expose `candidate_limit`, `candidate_count`,
`candidates_tried`, `candidate_rank`, `search_cells_evaluated`,
`refinement_cells_evaluated`, `body_tone_correlations`, and
`total_viterbi_branch_metrics`, along with CFO, confidence, tone SNR, pilot
hits, CRC, termination, and fill checks. The acquisition threshold is the
point-3 provisional constant `0.012`; it has not passed the 100,000-window
held-out false-acquisition freeze required by `PLAN.md`.

## Documentation/code cross-check and revisions

The implementation was checked against `GOALS.md`, `PLAN.md`, `DESIGN.md`,
`BASELINE.md`, `benchmark.py`, `whale.waveform`, `whale.rx_audio`, and the
repository bits, interleaver, framing, FEC, MFSK, HC0, and common-HF-lead
implementations. These point-3 resolutions are frozen in the code:

- `DESIGN.md` says an intended 6 kHz analysis rate, while the actual
  `WaveformMode` contract and shared receive front end supply 12 kHz. HR0-A
  therefore uses 96-sample 8 ms observations at 12 kHz. Tone centers and
  orthogonality are unchanged; no production decimator was modified.
- The design specifies packet PN whitening but not its seed. This revision
  reuses HC0's `0x0C4B1`, preserving the repository convention and making the
  wire deterministic.
- There is no production HR0 common-lead label and point 3 forbids assigning
  one. The budgeted 128 ms lead is therefore an experiment-only cycling HR0
  tone sequence with a 5 ms ramp. It is not interpreted as a production mode
  hint. Class-word acquisition remains authoritative.
- The 2 ms coarse and 0.25 ms fine timing resolutions are unchanged. Fine
  refinement spans a complete +/-8 ms observation because scoring only the
  trusted observations creates an 8 ms timing plateau. It scores all three
  known preamble observations to locate the transition, while body decoding
  still discards the frozen guard.
- The design says pilots cycle over the full bank but does not state the
  first tone. This revision starts at tone zero; the full class has 13 pilots
  and therefore uses tones 0 through 12 once.
- `mode_id=240` is only an experiment object field required by the benchmark.
  It is not a stable or negotiable on-air assignment.

## Known limitations

- Tiny and short checked bodies are not implemented, so the 4.876 s ACK and
  clean 23.894 bit/s stop-and-wait projection are not yet realizable by this
  object. Every payload currently spends the 12.604 s full-class keying.
- The pilot lattice is measured but not yet used for timing/clock tracking or
  a distributed acquisition fallback. `clock_offset_ppm` is reported as zero,
  not estimated.
- There is no persistent-tone excision, noise-history estimator, erasure
  marking, CW/notch mitigation, pulse shaping, or streaming state.
- The receiver's low provisional threshold can produce bounded false
  acquisition on noise. CRC, termination, fill, and class checks prevented a
  false payload in focused tests, but the required 100,000 absent-window gate
  has not run.
- Only small independent-frame AWGN smoke probes were run. There is no
  Watterson boundary, continuous fading, combined stress, long session,
  Raspberry Pi, radio, or comparative VARA evidence.

## Reproduction

From the repository root, using the required environment:

```sh
/Users/pedro/miniconda3/envs/gnuradio/bin/python -m py_compile \
  experiments/hr0/hr0.py experiments/hr0/benchmark.py \
  experiments/hr0/test_hr0.py experiments/hr0/test_benchmark.py

/Users/pedro/miniconda3/envs/gnuradio/bin/pytest -q \
  experiments/hr0/test_hr0.py experiments/hr0/test_benchmark.py

/Users/pedro/miniconda3/envs/gnuradio/bin/python \
  experiments/hr0/benchmark.py sweep \
  --modes experiments.hr0.hr0:HR0 --model awgn --points 20 \
  --trials 1 --workers 1 --save-failures 0 \
  --out /tmp/hr0_point3_smoke.json
```

At the point-3 handoff, the focused suite reported `30 passed`; the relevant
production DSP, HC0, mode-conformance, and channel-contract regressions
reported `170 passed` (with six pre-existing non-finite-input warnings from
production decoders). The one-frame +20 dB benchmark smoke decoded and
recorded 81,081 coarse cells, 195 refinement cells, 14,048 body-tone
correlations, and 72,704 Viterbi branch comparisons. Separate one-frame +20
dB canonical Watterson smokes decoded in `mid_latitude_disturbed`,
`mid_latitude_disturbed_nvis`, and `high_latitude_disturbed`; one frame per
preset is only a wiring check and says nothing statistical about robustness.
