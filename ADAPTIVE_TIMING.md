# Adaptive head timing

## Purpose and scope

Whalemodem measures leading loss independently in each radio direction and
uses a protocol-fixed head PN sequence to protect sync acquisition. The
measurement includes transmitter startup, audio buffering, receiver recovery,
squelch, and AGC settling because audio starts immediately after PTT assertion.

There is no tail guard, tail symbol pattern, tail measurement, or carrier-only
tail delay in the protocol. Hardware acceptance runs showed that trailing loss
was caused by the former audio-output truncation bug, not the radio path. A
keying ends at the final CRC and PTT is released when those samples finish.

## Head sequence and measurement

The head is an order-15 maximal-length PN sequence defined in `FRAMING.md`.
Calibration uses one second; ordinary frames use a suffix of that sequence at
the current connection duration. All durations therefore have the same PN
phase adjacent to sync. A receiver can locate sync and walk backward through
the head length it expected even when feedback changed the transmitted length.
A 16-symbol sliding window tolerates two hard-decision errors. The complete
first window with three errors is excluded, making the observation
conservative.

The decoder reports:

```text
head_seconds_received
```

Seconds, because the head measurement crosses layers that do not share a
symbol. The CPFSK profiles count matched pad symbols and divide by their baud;
mode 3 (VF3) and mode 4 (HC1) count 12 kHz receive-rate sync cores and divide
by 12 kHz; mode 5 (HC0) does the same with four-symbol receive-rate reference
blocks. These durations are identical to dividing the corresponding on-air
counts by 48 kHz. Each also
reports its native count as a diagnostic -- `head_symbols_received` for
CPFSK, `head_cores_observed` for VF3/HC1, and `head_blocks_observed` for HC0 --
but nothing in the link reads those.

Only a frame whose checked header, optional body, and CRC validate can produce
a timing observation. Near misses and CRC failures cannot affect timing.

### Weak-signal ambiguity

A short head observation does not by itself prove that the transmitter or
receiver clipped the beginning of the audio. The head measurement is made
only after waveform acquisition and a checked payload decode, but it uses a
separate detector from both of them. A frame can therefore acquire and pass
FEC/CRC while noise or distortion makes the adjacent head detector stop
early.

This matters especially for HC0. HC0 acquires from its known 24-symbol tone
pattern and decodes its coded payload from non-coherent tone energies. Its
head measurement instead correlates repeated four-symbol reference waveforms
backward from the acquired preamble. Counting stops at the first block that
falls below the correlation threshold, the relative-energy gate, or the
allowed phase continuity. At low SNR that can yield a short or zero count even
when the head audio was physically present. HC1 can fail acquisition on the
same weak direction because its acquisition threshold is much higher; that
failure does not cause the HC0 head result, although both can have the same
weak-signal cause.

The 2026-08-28 HF end-to-end radio run showed this unresolved ambiguity on the
STA2-to-STA1 direction: validated HC0 frames reported head observations from
zero to 128 ms, feedback drove the transmitted head to the one-second maximum,
and HC1 attempts did not decode. The logs alone cannot distinguish actual
leading loss from a present but weak head. A capture must be inspected or
replayed to make that distinction.

Until measurement quality is represented separately from duration, the
implementation deliberately treats zero as a lower bound and remains safe by
increasing the head. This may waste up to one second per keying on a weak path
without improving reception. A future resolution should make the estimator or
feedback confidence-aware and prevent repeated inconclusive observations from
being interpreted indefinitely as additional physical clipping. It must still
preserve conservative timing when the audio really is clipped.

## Connection calibration

Connection format version 4 uses a three-frame timing handshake:

1. The ISS sends CONNECT with a one-second head.
2. The IRS sends CONNECT_ACK with a one-second head. The ISS measures it.
3. The ISS sends TIMING_ACK containing its CONNECT_ACK observation and using a
   one-second head. The IRS measures it and derives the ISS transmit value.
4. The IRS sends TIMING_CONFIRM containing its TIMING_ACK observation, using
   its derived transmit head. The ISS derives its transmit value.

TIMING_ACK and TIMING_CONFIRM each contain exactly two bytes:

```text
session_id head_time_received
```

`head_time_received` is `ceil(head_seconds_received * 255 /
calibration_seconds)`. Values 1 through 255 represent the observed fraction of
the protocol-fixed one-second calibration head; zero is invalid. An
observation above the calibration head is clamped to 255 rather than rejected,
because a mode whose head is quantized -- HC1 rounds its head up to whole sync
cores -- can legitimately measure a little more than was asked for. The sender
derives:

```text
head_loss = 1 second * (255 - head_time_received) / 255
tx_head = min(1 second, head_loss + 10 ms minimum guard)
```

Repeated handshake packets are bound to the session identifier and are
idempotent. A connection fails if it cannot obtain a valid head measurement.

## In-session feedback

Every version-4 DATA frame carries the head duration it actually sent, rounded
upward in 10 ms units. Only after that DATA frame validates, the IRS compares
the observed adjacent head duration with the 10 ms residual target. DATA_ACK
piggybacks an absolute requested duration in the same units.

No increase is requested when the apparent deficit is no larger than the
mode's own measurement resolution: one 16-symbol matcher window at the DATA
baud for CPFSK, one 512-sample sync core (10.67 ms) for HC1, whose head is
deliberately half a core longer than a whole number of them. A zero
observation is a lower bound and requests a bounded 100 ms increase. Requests
are capped at the documented one-second maximum.

The request is absolute, not incremental. The ISS accepts it only in a
sequence- and mode-valid ACK for the outstanding DATA frame, then applies
`max(current, requested)`. Consequently retries and duplicates are idempotent,
stale or smaller feedback cannot decrease padding, and floor or mode changes
do not reset it. Durations are stored in seconds and converted upward to the
active mode's own head granularity -- whole symbols at the active baud for
CPFSK, whole sync cores for HC1 -- so protection remains constant across mode
changes.
Each endpoint owns only its transmit direction's value.

## Packet summary

- DATA byte 0: EOF flag and seven-bit sequence number.
- DATA byte 1: transmitted head duration in unsigned 10 ms units.
- DATA_ACK bytes 0..2: answered sequence, next expected sequence, received
  mode.
- DATA_ACK byte 3: absolute requested head duration in unsigned 10 ms units.

Ordinary control frames still use the connection's current transmit-head
duration, because leading loss belongs to the physical direction rather than
one packet type. Only DATA generates new in-session feedback. The exact raw
ordinary-frame symbol count is logged locally; the ACK sends the derived
absolute request.

## Compatibility and lifetime

Connection format version 4 and checked air-header version 2 are explicit
wire-format changes. Version 3 peers expect a third tail timing byte and a
physical tail sequence, so they are rejected rather than partially accepted.
Timing state is cleared on disconnect and is not persisted between sessions.

Logs expose observed head duration, reported/requested adjustment, old and new
transmit-head duration, and reasons observations or feedback were ignored.
