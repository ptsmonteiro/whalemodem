# Adaptive preamble and postamble timing

## Purpose

Every transmitted frame currently uses the same conservative keying and audio
allowances. They work across the radios tested so far, but stations that need
less protection pay the same cost on every frame.

This feature measures the complete end-to-end loss at the beginning and end of
a transmission during connection establishment. Calibration audio starts when
the transmitter is keyed, rather than after a fixed PTT lead delay, so the
measurement includes PTT response, audio-stream startup, buffering, receiver
squelch and AGC settling, and framing loss. Each direction is measured
independently. The two stations then use smaller, connection-specific timing
for the rest of that connection.

This is connection-time calibration. It is not continuous adaptation based on
DATA retransmissions.

## Scope

The feature adapts the complete keyed transmission surrounding a frame:

```text
calibration preamble | sync | length | payload | CRC | calibration postamble
```

For ordinary frames, the calibrated values replace the combined protection
currently supplied by `transport.PTT_LEAD`, `transport.PTT_TAIL`,
`HEAD_PAD_SECONDS`, and `TAIL_PAD_SECONDS`. Calibration therefore crosses the
transport/framing boundary: the transport starts calibration audio
concurrently with keying and does not wait for a fixed lead interval first.

The first version does not change turnaround timing, the sync word, or the
negotiated data mode. Audio API behavior that cannot be removed remains part of
the measured chain rather than an independently tuned timing allowance.

## Terminology

- **ISS**: the station that initiates the connection and initially owns the
  sending floor.
- **IRS**: the station that answers the connection.
- **Calibration sequence**: a known deterministic symbol sequence placed
  before or after a calibration frame.
- **Received head symbols**: the number of aligned calibration symbols
  successfully recovered immediately before the sync word.
- **Received tail symbols**: the number of aligned calibration symbols
  successfully recovered immediately after the CRC.
- **Guard**: extra protection retained beyond the measured loss, consisting of
  a 20% proportional margin subject to a minimum duration.

All calibration frames use the control mode. Counts therefore refer to control-
mode symbols and have the same meaning at both stations.

## Calibration sequences

The calibration preamble and postamble are fixed, known sequences. They must:

- be different from the frame sync word;
- have unambiguous alignment, unlike a repeating `0,1,0,1,...` pattern;
- exercise both FSK tones approximately equally;
- have good autocorrelation so noise is unlikely to look like part of them;
- have a length fixed by the protocol and known without reading the frame.

The head and tail use different sequences so that one cannot be mistaken for
the other. A PN sequence is suitable. The exact sequences and their lengths
must be fixed before implementation and documented in `FRAMING.md`.

The sequences are deliberately generous and are used only by the three
calibration frames. Normal control and data frames use the calibrated pads.
The calibration length is allowed to exceed the normal padding values and, if
necessary, the normal DATA keying budget, but it has its own documented finite
keying limit.

For calibration and ordinary transmissions, the head sequence begins entering
the audio output path as soon as the PTT operation is issued; there is no fixed
sleep before audio starts. The tail sequence is the last generated audio and
PTT is released as soon as the output path reports completion; there is no
fixed carrier-only tail sleep. Consequently, symbols lost while either radio
or audio path changes state are visible in the receiver's counts.

## Measurement semantics

The sync word provides the alignment for the head measurement. Once sync is
located, the receiver walks backward through the expected head sequence and
counts the correctly recovered, aligned symbols adjacent to sync.

The decoded frame length provides the alignment for the tail measurement. Once
the CRC position is known, the receiver walks forward through the expected tail
sequence and counts the correctly recovered, aligned symbols adjacent to the
CRC.

The receiver reports the two direct observations:

```text
head_symbols_received
tail_symbols_received
```

Both values are bounded by the known calibration sequence length. A value
outside that range makes the measurement invalid.

The initial implementation may stop a count at the first mismatching symbol.
If real-radio tests show that isolated bit errors make this too conservative,
the measurement may later use a documented error-tolerant matching rule. It
must never count unaligned noise merely because individual hard-decoded symbols
happen to match.

The decoder must retain enough audio before sync to inspect the entire
calibration preamble. It must also wait for the advertised calibration
postamble before returning a completed calibration measurement. Ordinary frame
decoding must not acquire this extra delay.

## Deriving operational padding

Because the number of calibration symbols transmitted is known, the sender can
infer the loss:

```text
head_symbols_lost = calibration_head_symbols - head_symbols_received
tail_symbols_lost = calibration_tail_symbols - tail_symbols_received
```

Radio impairments are durations, not symbol counts. The inferred losses are
therefore converted to seconds using the control-mode baud:

```text
head_loss_seconds = head_symbols_lost / control_baud
tail_loss_seconds = tail_symbols_lost / control_baud
```

The operational protection durations are:

```text
head_guard_seconds = max(HEAD_MIN_GUARD_SECONDS, head_loss_seconds * 0.20)
tail_guard_seconds = max(TAIL_MIN_GUARD_SECONDS, tail_loss_seconds * 0.20)

tx_head_seconds = head_loss_seconds + head_guard_seconds
tx_tail_seconds = tail_loss_seconds + tail_guard_seconds
```

Each duration is clamped to documented minimum and maximum values. When a frame
is sent in another mode, its pad symbol count is derived from time:

```text
head_symbols = ceil(tx_head_seconds * frame_baud)
tail_symbols = ceil(tx_tail_seconds * frame_baud)
```

Using seconds preserves the same protection when the data mode changes. All
duration-to-symbol conversions round upward; ordinary rounding must not shorten
the selected protection.

The 20% proportional margin is the default calibration policy. The minimum
guard and clamp constants are implementation parameters selected by software
fault-injection tests and repeated real-radio timing sweeps. A minimum guard is
required because a proportional margin alone supplies little or no protection
when the measured loss is small. These parameters are not transmitted on air.

If a calibration frame is retransmitted during the same connection attempt,
the endpoint retains the most conservative valid observation: the smallest
received head count and the smallest received tail count. Retries therefore
provide additional samples without adding protocol exchanges. A successful
connection may still be calibrated from a single observation when no retry is
needed.

## Connection handshake

A new `TIMING_ACK` carries the reverse-direction measurement. A final
`TIMING_CONFIRM` acknowledges its delivery and gives the initiator a definite
connection-complete event:

```text
ISS                                                   IRS

CONNECT with large calibration sequences
----------------------------------------------------->
                                      measures ISS -> IRS

                       CONNECT_ACK carrying that measurement,
                       with large calibration sequences
<-----------------------------------------------------
measures IRS -> ISS

TIMING_ACK carrying that measurement,
with large calibration sequences
----------------------------------------------------->

                                      TIMING_CONFIRM
<-----------------------------------------------------

both sides activate their connection-specific transmit padding
```

More precisely:

1. The ISS sends `CONNECT` with the full calibration preamble and postamble.
2. The IRS validates `CONNECT`, measures its head and tail sequences, and sends
   those two received-symbol counts in `CONNECT_ACK`.
3. The ISS validates `CONNECT_ACK`. The counts in its body describe the ISS's
   own transmissions, so the ISS derives and stores its operational transmit
   padding. The ISS also measures the calibration sequences around
   `CONNECT_ACK`.
4. The ISS sends those two received-symbol counts in `TIMING_ACK`. This frame
   also uses the full calibration sequences; optimized timing is not used yet.
5. The IRS validates `TIMING_ACK`. Its body describes the IRS's transmissions,
   so the IRS derives and stores its operational transmit padding.
6. The IRS sends `TIMING_CONFIRM`, using its newly derived operational padding,
   and accepts the connection as calibrated.
7. The ISS validates `TIMING_CONFIRM` and accepts the connection as calibrated.
   Receipt of this frame also exercises the IRS's derived padding once before
   application data is sent.

Thus each measurement controls only the transmitter whose frame was measured:

| Measured frame | Receiver | Measurement configures |
| --- | --- | --- |
| `CONNECT` | IRS | ISS transmit padding |
| `CONNECT_ACK` | ISS | IRS transmit padding |

The calibration sequences around `TIMING_ACK` make the final handshake frame
as robust as the first two. They are not used to calculate a third set of
values.

## Packet fields

Adaptive timing is required by connection format version 1, using the
versioned CONNECT and CONNECT_ACK bodies specified in
[`LINK.md`](LINK.md#versioned-connection-bodies-format-version-1). Support is
deduced solely from that version. The fields below must not be appended to the
legacy connection bodies.

`CONNECT_ACK` gains these fields:

```text
connect_head_symbols_received
connect_tail_symbols_received
```

`TIMING_ACK` contains:

```text
session_id
connect_ack_head_symbols_received
connect_ack_tail_symbols_received
```

`TIMING_CONFIRM` contains:

```text
session_id
```

The session identifier binds `TIMING_ACK` to the connection attempt and makes a
delayed packet from an earlier connection harmless.

Field widths depend on the final calibration sequence length. They must express
every value from zero through the full sequence length, inclusive. Multi-byte
counts use the framing convention: unsigned, most-significant byte first.

The exact placement and widths of the measurement fields remain to be fixed
after the calibration sequence lengths are selected. They will be defined as
fields of the version-1 packet bodies, not as ambiguous suffixes inferred from
the ends of legacy CONNECT or CONNECT_ACK bodies.

## Retries and duplicate frames

Calibration packets use the existing control-plane retry policy and control
mode. Retransmissions always carry the full calibration sequences.

A duplicate `CONNECT` for the active session is answered with the same logical
`CONNECT_ACK`. Its measurement fields contain the most conservative valid
observations accumulated for that session: the smallest received head and tail
counts. The same rule applies when the ISS receives duplicate `CONNECT_ACK`
frames before completing calibration.

A duplicate `TIMING_ACK` for the active session is idempotent: the IRS reapplies
the same derived padding and sends `TIMING_CONFIRM` again. The ISS retries
`TIMING_ACK` until it receives a matching `TIMING_CONFIRM` or exhausts the
control-plane retry limit. A duplicate `TIMING_CONFIRM` is harmless.

`TIMING_CONFIRM` uses the IRS's calibrated padding. If that derived timing does
not reach the ISS, the ISS retransmits `TIMING_ACK`; the IRS then sends another
confirmation. If confirmations repeatedly fail, connection establishment
fails at the ISS. As with any finite handshake, loss of the final packet can
temporarily leave the IRS believing the session is established. Duplicate
handling plus the existing inactivity timeout must make that state bounded and
self-healing. Each endpoint exposes the connection only at its acceptance point
described above.

## Failure behavior

Calibration must fail safe:

- A missing, truncated, or out-of-range measurement is invalid.
- If `CONNECT` or `CONNECT_ACK` decodes but its calibration tail has not fully
  arrived, the decoder waits only for a bounded calibration-tail timeout.
- If no valid measurement is obtained after the retry limit, the connection
  attempt fails rather than applying an unsafe short pad.
- A measurement implying that the calibration sequence itself was too short is
  invalid. In particular, receiving zero head or tail symbols provides no safe
  estimate of the true loss.
- Derived values are clamped and can never become negative or exceed the full
  calibration duration plus the configured guard.

The current fixed PTT and framing allowances remain the implementation fallback
during development and for tests that construct a `Link` without completing a
radio handshake. They are not silently substituted for a failed over-the-air
calibration unless the protocol explicitly chooses a compatibility mode.

## Connection lifetime

Measurements apply only to the connection in which they were made. They are
cleared when the link returns to `IDLE` and are not initially persisted across
connections.

Recalibration is not performed after a mode change because the stored values
are durations and are converted to the new mode's symbol count. A later feature
may persist successful measurements by radio configuration or recalibrate a
long-running connection, but neither is part of this design.

## Implementation outline

1. Implement and test the versioned, length-delimited CONNECT and CONNECT_ACK
   bodies from `LINK.md`, while retaining an explicitly selected legacy mode
   during migration.
2. Change the transport so calibration/operational head audio starts with the
   PTT operation and PTT is released without a fixed carrier-only tail delay.
3. Allow the framing encoder to accept per-frame head and tail sequences.
4. Add fixed calibration sequences and their protocol constants.
5. Extend the decoder result for calibration frames with the two received-
   symbol counts while leaving ordinary decode behavior unchanged.
6. Preserve sufficient leading audio and defer calibration-frame completion
   until the tail can be measured.
7. Add the `TIMING_ACK` and `TIMING_CONFIRM` packet types and encode/decode
   helpers for their fields.
8. Add per-connection transmit head and tail durations to `Link`.
9. Implement the calibration state transitions, conservative aggregation, and
   idempotent retry behavior.
10. Make frame airtime, keying-budget, and timeout calculations accept the
   active timing, including the separate bounded calibration budget.
11. Add unit tests with controlled leading and trailing truncation, followed by
   bidirectional hardware tests using `scripts/sweep_ptt_timing.py`.

## Acceptance criteria

The feature is complete when:

- both directions derive their padding from their own measured calibration
  frame;
- calibration audio begins with keying and therefore observes the complete
  transmitter, audio, and receiver startup chain;
- a known amount of leading or trailing truncation produces the expected
  received-symbol count and derived duration;
- the selected guard is 20% of measured loss or the configured minimum,
  whichever is greater;
- duplicate calibration observations select the most conservative valid head
  and tail counts;
- padding remains a constant duration across mode changes;
- lost or duplicated calibration packets cannot create a permanent half-open
  connection;
- malformed measurements cannot select unsafe padding;
- ordinary frames do not wait for a calibration postamble;
- the software loopback and link recovery suites pass;
- logs expose transmitted calibration lengths, received counts, inferred
  losses, guards, clamps, and active per-direction timing;
- repeated hardware connections establish an acceptable observed spread for
  the minimum guards and complete the existing acceptance transfer without new
  retransmissions.
