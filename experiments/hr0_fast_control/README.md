# Faster HF control experiment

This experiment retains three short/full MFSK candidates and the historical
128-FSK HR0 baseline. **On 2026-09-06 the owner explicitly selected MARGIN32
for production HR0, overriding the unresolved promotion gates.** Production
HR0 now uses its 32-FSK geometry, 1.812 s ACK, and 3.860 s full frame. The
experiment objects remain offline constructions. The measured 3 dB margin
is still unproven; the earlier evidence-based hold remains recorded below.
The objective is minimum short-ACK airtime at the same delivery probability
as Level-1 DATA at 3 dB less SNR. See [SPEED_LADDERS.md](../../SPEED_LADDERS.md)
for the requirement and [RESULTS.md](RESULTS.md) for retained measurements.

The derived target-rung boundaries are +6 dB SNR/3 kHz for quiet/moderate
and +11 dB for disturbed. These differ from the measured boundary of HC0,
the current next installed mode above HR0. Success at those relatively easy
positive-SNR points does not demonstrate the required measured 3 dB margin.

## Waveforms

| Candidate | Tones | Symbol duration | Sync symbols | Complete 12B ACK | Complete 42B frame |
| --- | ---: | ---: | ---: | ---: | ---: |
| `FAST16` | 16 | 10.667 ms | 24 | 1.215 s | 2.495 s |
| `FAST32` | 32 | 16 ms | 16 | 1.396 s | 2.932 s |
| `MARGIN32` | 32 | 21.333 ms | 16 | 1.812 s | 3.860 s |
| Historical HR0 | 128 | 56 ms | 16 | 3.508 s | 7.316 s |

All candidates use the existing terminated K=9 rate-1/2 convolutional code,
CRC32, two-byte length, whitening, and a multiplicative interleaver with a
coprime stride near the golden-ratio fraction of the coded block. Separate
short/full coded bodies accommodate up to 12/42 physical payload bytes.
Both lengths share acquisition; bounded short-first CRC decoding identifies
the successful body. The decoder receives no true frame-start index. It
searches timing and coarse frequency hypotheses, then estimates residual CFO.

The initial 1–1.5 s design budget yielded FAST16 and FAST32. MARGIN32 is one
bounded extension after those candidates did not establish the measured
margin. Its 1.812 s ACK reduces historical HR0 ACK airtime by 48.3%, with
additional symbol energy and narrower tone spacing than FAST32. The owner
subsequently selected it despite the open evidence gates; this selection
does not turn the experimental budget into a reliability result.

The common 128 ms lead and 20 ms tail are included above. Adaptive extra
lead, radio turnaround, and retransmissions add latency. Experiment objects
retain placeholder mode ID -1. The production replacement uses the existing
HR0 name, ID 10, and common-lead signature, with no legacy 128-FSK transmitter,
receiver, or negotiation option. **Both endpoints must update together.**
The old HR0 is frozen in `legacy_hr0.py` and `legacy_hr0_mode.py` only to
reproduce historical comparisons; `screen.py` and `session_smoke.py` import
that baseline explicitly. A synthetic mode ID in an isolated session test is not an assigned protocol identifier.

## Reproduce

Run from the repository root with its Python dependencies installed:

```sh
python -m pytest -q experiments/hr0_fast_control/test_candidate.py
python experiments/hr0_fast_control/session_smoke.py
python experiments/hr0_fast_control/screen.py --trials 6 --seed 260906 --snrs -12 -9 -6 -3 0 3 6 9 11 14 --output /tmp/hr0-fast-scout.json
python experiments/hr0_fast_control/screen.py --trials 32 --seed 910600 --snrs -9 -6 -3 --output /tmp/hr0-fast-held-out.json
python experiments/hr0_fast_control/screen.py --trials 12 --seed 920600 --snrs 6 11 --presets mid_latitude_quiet mid_latitude_moderate mid_latitude_disturbed --full-candidates --output /tmp/hr0-fast-full.json
python experiments/hr0_fast_control/margin_screen.py --trials 32 --seed 930600 --output /tmp/hr0-longer32.json
python experiments/hr0_fast_control/margin_screen.py --trials 300 --seed 1930600 --output /tmp/hr0-longer32-confirmation.json
python experiments/hr0_fast_control/margin_screen.py --trials 300 --seed 2930600 --presets mid_latitude_quiet mid_latitude_moderate mid_latitude_disturbed --watterson-control-snr -5 --output /tmp/hr0-longer32-upper-bracket.json
```

`session_smoke.py` opens localhost sockets and uses the paired synthetic audio
harness for connect, bidirectional transfer, and disconnect. It writes its
result to `results/session_smoke.json`; it does not use sound cards or radios.

`screen.py` compares actual link-framed 12-byte DATA_ACK packets against
64-byte HC0 DATA packets. `--full-candidates` switches HR0/candidates to
42-byte DATA packets while HC0 stays full at 64 bytes. Sequence numbers use
the low seven bits. Identical seeds pair payload and channel realizations;
different frame durations necessarily sample different lengths of the fading
process. `margin_screen.py` compares MARGIN32 ACKs at -9 dB AWGN/-6 dB
Watterson against HC0 DATA exactly 3 dB above these points by default.
`--presets`, `--watterson-control-snr`, and `--awgn-control-snr` select
additional matched points; the DATA side remains 3 dB above the control.

Both scripts use `whale.qualification.channel_factory`: 48 kHz audio, the
canonical SNR/3 kHz convention, and repository Watterson presets with AWGN
after fading. They drain the channel, pad the downsampler delay, and invoke
the ordinary decoder. The SNR reference is the complete supplied waveform
at the AWGN stage; per-trial channel descriptions and measurements retain
that convention. This is not a calibrated fixed-transmitter-power outage
comparison. No radio filtering, frequency drift, clipping, or clock error is
added by these campaigns.

Results retain per-trial success, acquisition confidence/status, failure,
decode time, payload length, airtime, seeds, and channel descriptions. Decode
time is host timing, not a low-end-device real-time claim. Occupied bandwidth
uses equal 0.5%/99.5% cumulative-power edges of a periodogram of each complete
transmitted keying, including its lead and tail; five payloads are measured.
It is a finite waveform measurement, not exhaustive spectral-mask testing.

`screen.py` records the repository revision and experiment source hashes;
`margin_screen.py` embeds complete experiment Python source snapshots.
The former's historical hashes precede later adapter feedback/property
additions and the explicit legacy-baseline imports. Neither changes the
historical transmitted geometry or FEC. The frozen legacy modules preserve
the pre-replacement implementation with adjusted imports. For an
exact historical margin replay, restore the embedded sources into an isolated
checkout of the recorded revision. Host timings will vary. The full-frame
artifact's generic `method` text still mentions ACKs: its `full_candidates`
flag and per-trial `payload_bytes` are the authoritative workload fields.

Scripts write after each completed row and abort on unexpected exceptions.
An `errors: 0` field does not make a partially written file complete. Check
expected row counts in RESULTS.md; retain stderr for a failed invocation.

## Remaining qualification gates

Locate common-reliability boundaries with adequate confidence and independent
seeds across the required fading/impairment envelope. A few points and small
sample counts do not establish a 3 dB advantage or noninferiority. Validate
full-size management/fallback delivery separately, including MARGIN32, and
measure retry-inclusive session latency, false acquisition, CFO/drift,
clipping, filtering, clock mismatch, and representative radios. The owner override changes availability, not these evidence requirements.
Further waveform revisions must make their compatibility decision explicit;
the present replacement requires both peers to upgrade.
