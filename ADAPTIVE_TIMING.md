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
mode 4 (HC1) counts whole 512-sample sync cores and divides by the sample
rate; a future mode counts whatever its head is made of. Each may also report
its own raw count as a diagnostic -- `head_symbols_received` for CPFSK,
`head_cores_observed` for HC1 -- but nothing in the link reads those.

Only a frame whose checked header, optional body, and CRC validate can produce
a timing observation. Near misses and CRC failures cannot affect timing.

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
