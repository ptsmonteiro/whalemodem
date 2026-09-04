# HF2: a from-scratch HF SSB mode targeting Speed Ladder Level 2

## Purpose and status

HF2 is a working name for an experimental HF data waveform whose target is
Level 2 of the HF SSB ladder in [SPEED_LADDERS.md](../../SPEED_LADDERS.md):

| Level | Objective | Minimum useful application throughput | Required operating envelope |
| ---: | --- | ---: | --- |
| 2 | General-purpose data | 500 bit/s | Quiet at +5 dB waveform SNR and above; moderate at +10 dB waveform SNR and above |

"Quiet" and "moderate" are the repository's Watterson classes
(`mid_latitude_quiet` = 0.5 ms delay spread / 0.1 Hz Doppler,
`mid_latitude_moderate` = 1.0 ms delay spread / 0.5 Hz Doppler in
`whale/channel.py`'s `WATTERSON_PRESETS`). This experiment does not target
disturbed fading, low/high-latitude presets, or any envelope beyond the
Level 2 contract; those belong to other rungs.

This design is deliberately independent of HC0, HC1, VF6, HR0, or any other
mode or experiment already in this repository -- no parameter, geometry, or
measured conclusion is carried over from them. The only things reused are
the shared, mode-agnostic DSP primitives in `whale/dsp/` (OFDM symbol
build/analyze, the rate-1/2 K=7 convolutional code, frequency/timing
recovery, equalization), on the same basis every existing OFDM mode already
uses them independently, and the shared `whale.modes.hf_lead` acquisition
label block so a receiver polling for HF frames can find HF2 the same way it
finds HC0/HC1.

No radios are available for this work. All qualification evidence is
simulated, using `whale.qualification`'s Monte Carlo helpers over
`whale.channel`'s Watterson model, which `SPEED_LADDERS.md` and
`MODE_QUALIFICATION.md` treat as valid primary qualification evidence
(hardware/session evidence is a separate, later gate this experiment does
not attempt to satisfy).

## Design hypothesis

Level 2's envelope (quiet fading at +5 dB and moderate fading at +10 dB) is
substantially more forgiving than a Level 0/control design has to tolerate.
The hypothesis this experiment tests: spending that extra SNR margin on a
denser constellation (16-QAM, pilot-assisted coherent detection) rather than
on redundancy yields several times a differentially-coded, heavily-redundant
design's useful throughput, while still clearing the FER/acquisition gate
across the full required envelope.

See [DESIGN.md](DESIGN.md) for the concrete geometry, constellation, pilot
layout, and coding choice, and why each was picked.

## Falsifiable target

| Target | Required result |
| --- | --- |
| Floor | >=500 bit/s net application throughput per full-capacity DATA frame (DATA-chunk bits / complete encoded frame airtime), simultaneously with the separate FER/acquisition gate below. |
| Envelope | Passes the MODE_QUALIFICATION.md Monte Carlo gate at every point in `mid_latitude_quiet` >= +5 dB and `mid_latitude_moderate` >= +10 dB tested. |
| Stretch | Maximize useful throughput beyond the 500 bit/s floor without narrowing the required envelope; report the achieved rate honestly rather than assuming a nominal one. |

Missing the floor or the envelope at any required point means the design is
revised (pilot density, code rate, interleaving, payload size) and re-run --
not that the envelope is narrowed to fit the result, per
`MODE_QUALIFICATION.md`'s Level 2 contract.

## Statistical gates (MODE_QUALIFICATION.md, section 3)

- Screening: >=30 trials/point while iterating.
- Boundary shaping: >=100 independent trials per `(candidate, point)`.
- Confirmed boundary: >=300 trials at the two points used to claim +5 dB
  quiet and +10 dB moderate as passing.
- Inside the required envelope: 95% Wilson upper bound on FER <= 10%, 95%
  Wilson lower bound on acquisition probability >= 90%, zero `error`
  outcomes.
- Point grid brackets each envelope edge: >=2 points well inside, >=2 near
  the boundary, >=2 outside (outside points locate the boundary and are not
  required to pass).
- AWGN is used only as a diagnostic sanity baseline, not as qualifying
  evidence for an `hf-ssb` rung.

## Sequential stages

1. Freeze this plan and the design record (this document and DESIGN.md).
2. Build the waveform (`hf2.py`): geometry, mapping, FEC, framing,
   frequency/timing recovery, deterministic round-trip test.
3. Build the benchmark harness (`benchmark_hf2.py`) and run an AWGN sanity
   pass, then a coarse quiet/moderate screen.
4. Iterate design against the screen until the Level 2 gate is met at both
   required boundary points; run the >=300-trial confirmation.
5. Record results (`RESULTS.md`): Wilson intervals, useful throughput,
   explicit statement of what is not yet established (hardware, sessions).
6. Only if 4 clears: promote into `whale/modes/` as an experimental,
   unregistered-by-default `WaveformMode`, per `MODE_QUALIFICATION.md`'s
   promotion path.

Each stage's artifacts and conclusions are retained under
`experiments/hf2/results/` before the next stage begins.
