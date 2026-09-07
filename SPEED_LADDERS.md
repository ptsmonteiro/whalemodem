# HF and VHF FM speed-ladder targets

This document defines the five target rungs for Whalemodem's HF SSB and VHF
FM speed ladders. A rung is a service objective: it combines minimum net
application throughput per data frame with a required channel envelope.
Modulation, coding, frame geometry, and implementation are deliberately
outside this document.

These are end-state targets, not descriptions of the modes currently present
in the repository. The evidence and pass/fail rules for claiming a rung are
defined in [MODE_QUALIFICATION.md](MODE_QUALIFICATION.md).

## Shared interpretation

- Level 0 is the control and last-resort fallback rung. HF minimizes short
  control airtime subject to a 3 dB reliability advantage over Level 1;
  FM prioritizes maximum coverage.
- Levels 1 through 3 exchange channel margin for increasing net application
  throughput per data frame.
- Level 4 is the maximum-speed rung. Its deliberately narrow channel envelope
  must not be widened by sacrificing its throughput target.
- A target is satisfied only when both its throughput and every channel point
  in its required envelope pass the qualification rules.
- Speeds are minimum net application throughput per full-capacity DATA frame:
  application DATA-chunk bits divided by that encoded frame's complete
  airtime. The numerator excludes the air header and all other link/physical
  overhead; the denominator includes the waveform's acquisition, framing,
  coding, guards, and intentional lead/tail samples. ACKs, retries, radio
  turnaround, connection traffic, adaptation, and other session traffic are
  excluded. Frame delivery reliability is enforced separately by the channel
  envelope gates in `MODE_QUALIFICATION.md` and is not multiplied into this
  rate.
- Channel thresholds are inclusive: a target stated at `10 dB and above`
  includes the 10 dB boundary point.

## HF SSB ladder

All HF targets occupy the 300-2,700 Hz audio channel, inclusive: no active
carrier or intentional signal energy is placed outside it. The measurable
gate is a 99%-power occupied bandwidth of no more than 2,500 Hz. That
allowance is deliberate and is not slack for a wider carrier plan: a
waveform that fills the band edge to edge measures about 2,445 Hz, because
the 99%-power interval of a 2,400 Hz-wide signal includes its transform
skirts. A mode whose carriers sit inside 300-2,700 Hz but which measures
past 2,500 Hz is radiating splatter and fails the gate. SNR uses the
standard 3 kHz passband convention in [CHANNELS.md](CHANNELS.md): mean signal
power over an explicitly recorded half-open reference interval divided by
white-noise power in 3,000 Hz. Target
qualification uses the interval from the first transmitted acquisition sample
through the last intentional frame or tail sample, including internal guards.

The standard fading classes use the repository's Watterson definitions:

| Class | Differential delay spread | Doppler spread |
| --- | ---: | ---: |
| Quiet | 0.5 ms | 0.1 Hz |
| Moderate | 1.0 ms | 0.5 Hz |
| Disturbed | 2.0 ms | 1.0 Hz |

The benign/static class is a measured or reproducibly simulated SSB path with
no more than 0.1 ms differential delay spread and 0.005 Hz Doppler spread.
Its qualification channel must retain its complete filter, frequency-offset,
drift, level, and nonlinearity description; identity-channel or AWGN-only
evidence is insufficient.

| Level | Objective | Minimum net application throughput per DATA frame | Required operating envelope |
| ---: | --- | ---: | --- |
| 0 | Short control and last-resort fallback | 20 bit/s | Quiet and moderate at +6 dB SNR/3 kHz and above; disturbed at +11 dB and above |
| 1 | Robust data | 100 bit/s | Quiet and moderate at +9 dB and above; disturbed at +14 dB and above |
| 2 | General-purpose data | 500 bit/s | Quiet at +14 dB and above; moderate at +19 dB and above |
| 3 | Fast data | 2,000 bit/s | Benign/static at +17 dB and above; quiet at +19 dB and above |
| 4 | Maximum speed | 4,000 bit/s | Benign/static at +22 dB and above |

Levels 1–4 preserve the former physical noise levels after conversion from
the retired 0–24 kHz convention (exact offset: +9.03 dB).

**2026-09-06 HF Level-0 revision:** the owner replaced the former +4 dB
all-class requirement with a relative control-margin objective: the same
packet-delivery reliability at 3 dB less SNR than Level 1 data, approximately
half the received signal power at fixed noise. This is not half the packet
loss rate and does not imply half the airtime. The resulting target-rung
boundaries are +6 dB for quiet/moderate and +11 dB for disturbed. The 20 bit/s
full-DATA floor remains in force, now against the 300-2,700 Hz channel
definition above rather than the retired 2,300 Hz width ceiling. FM is
unchanged.

Optimize actual short-control airtime subject to this requirement. A
1–1.5-second complete ACK is the initial experimental design budget, not a
measured capability or a substitute for the reliability gates. Report
minimum waveform airtime separately from adaptive lead, radio turnaround,
and retry-inclusive latency.

For the relative-margin comparison, use actual short ACK packets against
full-capacity Level-1 DATA packets through matching channel presets and
impairments, with the same SNR reference convention. Locate each mode's
boundary at a common delivery probability and report uncertainty. The target
numbers above are derived from the Level-1 contract; they do not establish a
3 dB advantage over the mode currently installed on that rung. A replacement
must separately demonstrate the margin over that measured data-mode boundary;
saturated 100% delivery at easy points cannot establish the margin. Retain
the data-mode identity and revision with the comparison. Management and
fallback DATA sizes also need their own envelope checks.

**2026-09-06 owner product override:** after reviewing the candidate evidence,
the owner explicitly directed replacement of HR0 with the MARGIN32 geometry:
32-FSK, 1.812 s short ACK, and 3.860 s full frame. It remains production HR0,
mode ID 10, with the same common-lead signature. Both endpoints must update;
legacy 128-FSK transmission/reception is not negotiated or supported by this
replacement. The prior 128-FSK HR0 is retained as a historical experiment
baseline. This product selection overrides the outstanding promotion gates;
it does not change the 3 dB requirement or mark it qualified. The measured
relative margin, full packet-size envelope, hardware, and remaining evidence
gates remain open (see `MODE_QUALIFICATION.md`).

**2026-09-01 revision:** Level 4's floor was lowered from 7,050 bit/s to 4,000
bit/s. The original figure was carried over from the VHF FM ladder's shape and
was never grounded in an HF SSB measurement; the accumulated real-hardware
record across `experiments/hf5_8psk_4k` through `experiments/hf13_fast_sync_v1`
(single-carrier 8PSK ceiling ~4,050 bit/s uncoded; 49-carrier OFDM with 16-QAM
and LDPC 3/4 reaching ~4,332 bit/s qualified and ~4,969 bit/s provisional) shows
this channel and hardware pair topping out around 4-5 kbit/s, not 7. The new
floor is a deliberate, evidence-based target fixed going forward -- it must not
be adjusted again merely because a future candidate's own result falls short of
it; per the shared interpretation above, a target is not narrowed after seeing
a candidate's numbers. Any future increase requires new measured ceiling
evidence exceeding today's, not a redesign of a specific mode's shortfall.

## VHF FM ladder

VHF thresholds are RF carrier-to-noise ratios over the full complex-IQ
Nyquist bandwidth. They are not baseband audio SNR or receiver-estimated
per-carrier SNR.

Every VHF target must be qualified through a named, retained, measured radio
preset. The Level 0 through Level 2 preset represents the conservative end of
the supported radio population, including its measured passband, ripple,
pre/de-emphasis, limiting, deviation, discriminator response, and squelch.
Level 3 uses a measured clean data-capable path. Level 4 uses a measured
excellent path with the bandwidth and linearity needed for the target rate.
Synthetic profiles may locate boundaries but cannot by themselves satisfy a
target rung.

| Level | Objective | Minimum net application throughput per DATA frame | Required operating envelope |
| ---: | --- | ---: | --- |
| 0 | Control and fallback | 500 bit/s | Conservative measured path at 5 dB RF C/N and above |
| 1 | Robust data | 1,000 bit/s | Conservative measured path at 10 dB RF C/N and above |
| 2 | General-purpose data | 2,500 bit/s | Conservative measured path at 15 dB RF C/N and above |
| 3 | Fast data | 6,000 bit/s | Measured clean path at 25 dB RF C/N and above |
| 4 | Maximum speed | 12,750 bit/s | Measured excellent path at 37 dB RF C/N and above |

The measured presets are qualification fixtures, not generic claims about a
radio model. Their source measurements, direction, equipment, settings, and
derived channel parameters must be retained so another run can reproduce the
claimed envelope.

**2026-09-07 HF channel revision:** the owner replaced the former 2,300 Hz
occupied-bandwidth ceiling with the 300-2,700 Hz channel definition above.
The retired ceiling was narrower than the SSB passband the project's own
radios actually pass, and it was costing measured throughput on the
maximum-speed rung for no operational benefit on this channel plan. Every
rung's throughput floor and operating envelope are unchanged; only the
bandwidth constraint moved. Modes previously recorded as failing the
bandwidth gate are not thereby re-qualified -- HF2, whose 99%-power occupied
interval runs roughly 480-4,100 Hz, remains far outside the 300-2,700 Hz
channel and still fails.
