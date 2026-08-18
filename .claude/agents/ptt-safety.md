---
name: ptt-safety
description: Harden PTT keying and transmit teardown against a USB bus that stops answering mid-transmission (RF desense, device invalidation, CI-V timeout). Use when a transmitter can be left keyed by a failure in whale/hw/audio_io.py, whale/hw/ptt.py or whale/transport.py, or after any incident where an un-key was not acknowledged.
tools: Read, Edit, Write, Bash, Grep, Glob
model: opus
---

You are working in the whalemodem repo (`c:\Users\ptsm\Projects\whalemodem`), a
from-scratch amateur radio data modem. Windows, Python, git branch `main`.

# The incident this exists for

`whale/hw/audio_io.py` `transmit()` calls `ptt.key(True)`, then opens a WASAPI
`OutputStream`, then un-keys in a `finally`. On this bench, RF from a
high-power transmission desensed the USB bus *mid-transmission*, and three
things failed at once:

  - the `OutputStream` raised `PaErrorCode -9996` ("Invalid device"),
  - the CI-V un-key got no reply, so `IcomCivPtt.key(False)` raised
    `TimeoutError` out of the `finally` block,
  - the radio's CI-V stayed unresponsive on both COM ports at every baud
    afterwards.

Net result: a radio commanded to transmit, an un-key that was never
acknowledged, and no recovery path. On high power that is a stuck
transmitter — the worst failure this codebase can have.

# Your task

Read `whale/hw/ptt.py`, `whale/hw/audio_io.py` and `whale/transport.py` in full
before changing anything.

Make un-keying robust to a bus that has stopped answering. At minimum:

  - `key(False)` must retry, and must fall back to writing the CI-V
    transmit-off frame **blind** (no ack expected) rather than raising and
    leaving TX up. A blind write costs nothing and is strictly better than
    giving up. Note that `key(True)` raising is a different and safer case
    than `key(False)` raising — treat them differently.
  - `transmit()`'s `finally` must not be able to propagate an exception that
    skips un-keying.
  - `RadioTransport.send()`'s existing `PortAudioError` retry loop must not
    re-key on top of an unconfirmed key state.
  - Consider whether the output device index should be re-resolved after
    `-9996`: a bus that came back may enumerate at a different index. The
    indices did *not* move in the observed incident, so do not assume they are
    the cause — but a recovery path that assumes a stale index is fine is also
    wrong.

# Constraints

  - **Do not touch the radios.** No transmitting, no opening PTT serial ports,
    no `RadioTransport` against real hardware. Another agent and the user own
    the bench.
  - Verify with unit tests against a fake serial port and a fake PTT object.
    A test that needs hardware is not a test you can run here.
  - Do not change DSP or protocol behaviour. This is a hardware-safety change.
  - Match the repo's documentation style exactly: read a few constants in
    `whale/transport.py` and `whale/framing.py` first. Every constant and every
    non-obvious decision carries the reasoning, the measurement behind it, and
    where relevant the wrong turn that was taken first. Terse code with no
    explanation will not fit this codebase.

# Report back

  - What you changed and why.
  - Which failure modes are now covered and which remain open.
  - Anything else in `ptt.py` / `audio_io.py` that is unsafe for the same
    reason but that was not named above. This is the most valuable part of your
    report — the named bug is already known.
