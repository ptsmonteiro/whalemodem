# HF and VHF FM speed-ladder targets

This document defines the five target rungs for Whalemodem's HF SSB and VHF
FM speed ladders. A rung is a service objective: it combines minimum useful
application throughput with a required channel envelope. Modulation, coding,
frame geometry, and implementation are deliberately outside this document.

These are end-state targets, not descriptions of the modes currently present
in the repository. The evidence and pass/fail rules for claiming a rung are
defined in [MODE_QUALIFICATION.md](MODE_QUALIFICATION.md).

## Shared interpretation

- Level 0 is the control and last-resort fallback rung. Its priority is
  maximum coverage.
- Levels 1 through 3 exchange channel margin for increasing useful
  application throughput.
- Level 4 is the maximum-speed rung. Its deliberately narrow channel envelope
  must not be widened by sacrificing its throughput target.
- A target is satisfied only when both its throughput and every channel point
  in its required envelope pass the qualification rules.
- Speeds are minimum sustained useful application throughput, not symbol,
  coded, nominal, payload-per-frame, or error-free-loopback rates.
- Channel thresholds are inclusive: a target stated at `10 dB and above`
  includes the 10 dB boundary point.

## HF SSB ladder

All HF targets use no more than 2,300 Hz occupied bandwidth. SNR uses the
waveform convention in [CHANNELS.md](CHANNELS.md): mean signal power over an
explicitly recorded half-open reference interval divided by real AWGN power
over the 0--24 kHz Nyquist band at the 48 kHz audio boundary. Target
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

| Level | Objective | Minimum useful application throughput | Required operating envelope |
| ---: | --- | ---: | --- |
| 0 | Control and last-resort fallback | 20 bit/s | Quiet, moderate, and disturbed at -5 dB waveform SNR and above |
| 1 | Robust data | 100 bit/s | Quiet and moderate at 0 dB and above; disturbed at +5 dB and above |
| 2 | General-purpose data | 500 bit/s | Quiet at +5 dB and above; moderate at +10 dB and above |
| 3 | Fast data | 2,000 bit/s | Benign/static at +8 dB and above; quiet at +10 dB and above |
| 4 | Maximum speed | 7,050 bit/s | Benign/static at +13 dB and above |

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

| Level | Objective | Minimum useful application throughput | Required operating envelope |
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
