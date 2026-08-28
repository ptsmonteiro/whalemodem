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
calibration preamble | checked packet header | optional body | calibration postamble
```

For ordinary frames, the calibrated values replace the conservative one-second
`HEAD_PAD_SECONDS` and `TAIL_PAD_SECONDS` allowances. The transport already
starts audio-stream setup concurrently with keying and has no fixed lead or
tail sleep, so clipping is observable as lost pad symbols.

The fixed turnaround delay is zero. Its former protection is folded into the
adaptive head sequence, which measures the total clipping between one
station's transmission and the peer's reply. The first version does not change
the sync word or negotiated data mode.

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

The head and tail use different order-15 maximal-length PN sequences so that
one cannot be mistaken for the other. Their LFSR taps, seed, generation rule,
and baud-dependent lengths are fixed in `FRAMING.md`. At the current
one-second calibration duration their 32,767-symbol periods exceed every
supported sequence length.

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

The checked header announces whether a body follows and its exact decoded
length and mode. That determines the complete keying extent. Once the final CRC
position (header CRC for a header-only packet, body CRC otherwise) is known,
the receiver walks forward through the expected tail sequence and counts the
correctly recovered, aligned symbols adjacent to it.

The receiver reports the two direct observations:

```text
head_symbols_received
tail_symbols_received
```

Both values are bounded by the known calibration sequence length. A value
outside that range makes the measurement invalid.

Counting uses a 16-symbol sliding window and tolerates at most two hard-decision
errors in each window. When a window contains three errors, the receiver stops
and excludes that complete 16-symbol window from the received count. Excluding
the suspect window compensates conservatively for the look-ahead needed to
distinguish clipping from isolated errors: noise cannot earn extra received
symbols merely because some individual decisions happen to match. Audio-buffer
boundaries stop the count immediately.

The decoder must retain enough audio before sync to inspect the entire
calibration preamble. It must also wait for the advertised calibration
postamble before returning a completed calibration measurement. Ordinary frame
decoding must not acquire this extra delay.

## Deriving operational padding

The receiver first converts its raw symbol count to the one-byte reported
duration, rounding upward:

```text
head_time_byte = ceil(head_symbols_received * 255 / calibration_head_symbols)
tail_time_byte = ceil(tail_symbols_received * 255 / calibration_tail_symbols)
```

The sender infers loss in seconds from that protocol value:

```text
head_loss_seconds = calibration_seconds * (255 - head_time_byte) / 255
tail_loss_seconds = calibration_seconds * (255 - tail_time_byte) / 255
```

Rounding received duration upward can understate loss by less than one unit
(currently 3.92 ms); the mandatory minimum guard covers that quantization.

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

CONNECT with large calibration sequences (opening frame only)
----------------------------------------------------->

                       CONNECT_ACK with large calibration sequences
<-----------------------------------------------------
measures IRS -> ISS after a real TX-to-RX transition

TIMING_ACK carrying that measurement,
with large calibration sequences
----------------------------------------------------->
                                      measures ISS -> IRS after a real
                                      TX-to-RX transition

                    TIMING_CONFIRM carrying the TIMING_ACK measurement
<-----------------------------------------------------

both sides activate their connection-specific transmit padding
```

More precisely:

1. The ISS sends `CONNECT` with the full calibration preamble and postamble.
2. The IRS validates `CONNECT` and immediately sends `CONNECT_ACK` with full
   calibration sequences. `CONNECT` is not a timing probe because the IRS was
   not transmitting immediately beforehand.
3. The ISS measures the calibration sequences around `CONNECT_ACK`; this
   observation includes its transition from transmitting to receiving.
4. The ISS sends those observations as received-duration bytes in `TIMING_ACK`. This frame
   also uses the full calibration sequences; optimized timing is not used yet.
5. The IRS validates `TIMING_ACK`. Its body describes the IRS transmission, so
   the IRS derives its transmit padding. The IRS also measures `TIMING_ACK`,
   including its own transition from transmitting to receiving.
6. The IRS sends that observation in `TIMING_CONFIRM`, using its newly derived
   operational padding, and accepts the connection as calibrated.
7. The ISS validates `TIMING_CONFIRM`, derives its transmit padding from the
   reported measurement, and accepts the connection as calibrated.

Thus each measurement controls only the transmitter whose frame was measured:

| Measured frame | Receiver | Measurement configures |
| --- | --- | --- |
| `CONNECT_ACK` | ISS | IRS transmit padding |
| `TIMING_ACK` | IRS | ISS transmit padding |

Both probes occur immediately after the receiving station transmitted, so the
head loss includes peer unkeying, both radios' direction changes, transmitter
startup, receiver recovery, and audio buffering as one effective duration.

## Packet fields

Adaptive timing is required by connection format version 2, using the sole
CONNECT and CONNECT_ACK body format specified in
[`LINK.md`](LINK.md#connection-bodies). There is no legacy connection mode or
downgrade path.

`TIMING_ACK` contains:

```text
session_id
connect_ack_head_time_received
connect_ack_tail_time_received
```

`TIMING_CONFIRM` contains:

```text
session_id
timing_ack_head_time_received
timing_ack_tail_time_received
```

The session identifier binds `TIMING_ACK` to the connection attempt and makes a
delayed packet from an earlier connection harmless.

Each measurement is one unsigned byte proportional to successfully decoded
calibration-symbol time. `0` represents zero received duration and `255`
represents the complete protocol-fixed calibration duration (currently one
second). The encoder converts the received symbol count to this scale and
rounds upward. Thus one unit currently represents approximately 3.92 ms. Zero
is invalid for timing derivation because the actual loss may exceed the
calibration interval.

## Retries and duplicate frames

Calibration packets use the existing control-plane retry policy and control
mode. Retransmissions always carry the full calibration sequences.

A duplicate `CONNECT` for the active attempt is answered with the same
`CONNECT_ACK`. If retries produce multiple valid probe observations, the
receiver retains the smallest head and tail counts before reporting them.

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

The current fixed PTT and framing allowances remain an internal development
default for tests that construct a `Link` without completing a radio
handshake. They are never substituted for a failed over-the-air calibration.

## Connection lifetime

Measurements apply only to the connection in which they were made. They are
cleared when the link returns to `IDLE` and are not initially persisted across
connections.

Recalibration is not performed after a mode change because the stored values
are durations and are converted to the new mode's symbol count. A later feature
may persist successful measurements by radio configuration or recalibrate a
long-running connection, but neither is part of this design.

## Implementation status

Connection format version 2 and the adaptive-timing handshake are implemented.
CONNECT and CONNECT_ACK use the length-delimited bodies from `LINK.md`; the
former NUL-delimited format has no legacy decoder or downgrade path. Calibration
frames use the fixed PN sequences and per-frame timing support in the framing
layer. Their decoder results include head and tail observations, which the link
exchanges through `TIMING_ACK` and `TIMING_CONFIRM` and converts into
per-connection transmit padding.

The implementation also includes bounded calibration decoding, timing-aware
airtime and timeout calculations, conservative aggregation of repeated
observations, and idempotent handling of repeated handshake packets. Software
tests cover the encoding, timing derivation, clipping behavior, and link
recovery paths. Bidirectional hardware sweeps with
`scripts/sweep_ptt_timing.py` remain part of validation for particular radio
and audio configurations rather than an unimplemented protocol step.

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
