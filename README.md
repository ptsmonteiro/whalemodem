# whalemodem

A from-scratch amateur radio data modem exposing a **VARA-API-shaped** TCP
interface (command port + data port), for exchanging bytes between two
stations over an FM/analog radio link.

This is a correctness-first v1: it does not drive real VARA software, and
it is not tuned for throughput. It implements its own physical layer
(CPFSK audio modem) and link layer (stop-and-wait ARQ) underneath an
interface shaped like VARA's, because that connect/data-stream/disconnect
shape is a well-understood target for other software to talk to.

## Acceptance criteria

Connect a session between station A and station B, send 1KB from A to B,
switch roles, send 1KB back from B to A, then disconnect at either
station's request. Verified byte-for-byte on real hardware (see
`acceptance_test.py`).

## Layout

```
whale/
  framing.py     bit-level framing: sync word, length+CRC16, bit packing
  afsk.py         CPFSK modulate/demodulate (300 baud, 1200/1800 Hz tones)
  waveform.py     physical-layer mode contract and negotiable mode registry
  transport.py    one radio: continuous RX capture + keyed TX
  link.py         stop-and-wait ARQ: connect / send / recv / disconnect
  vara_server.py  VARA-API-shaped TCP front end (StationServer, CLI)
  hw/             sound card lookup + PTT keying (audio_io, ptt, radios)
tests/
  link_harness.py         two Links against each other, in one process
  test_afsk_loopback.py   pure-software self-test, no hardware/radios
  test_link_recovery.py   what a *lost control frame* does to a session
scripts/
  bench.py                   the rig the sweeps share: radio pair, trial loop, walk
  hw_smoke_single_frame.py   one AFSK frame each direction, no ARQ/sockets
  hw_smoke_link.py           full connect/send/disconnect via Link, no sockets
  hw_half_open_recovery.py   kill one station mid-session, time the other
  measure_peer_gap.py        worst legitimate peer silence, from the logs
  sweep_ptt_timing.py        the four dead-time knobs inside one keying
  sweep_turnaround.py        the dead time *between* two stations' keyings
acceptance_test.py            drives the full acceptance scenario over TCP
```

Every characterisation script in `scripts/` answers the same shape of
question -- "does a frame with these parameters survive the real link?" --
the same way: bypass `whale.link`'s ARQ and do one direct modulate -> TX ->
capture -> demodulate per trial, both directions, worst direction decides.
`scripts/bench.py` is that method, written once. It also holds the
frame-span diagnostic printed on a near miss, which was wrong in its first
form (it compared an absolute `end_index` against a bare frame duration, so
every cleanly-read frame reported as ~1.2x and looked like a false sync
lock) and had to be fixed separately in each copy.

## Documentation

- [`LINK.md`](LINK.md) describes the link protocol, ARQ, negotiation,
  and local TCP interface.
- [`FRAMING.md`](FRAMING.md) describes modulation profiles, coding,
  framing, synchronization, error detection, and on-air timing.
- [`GOALS.md`](GOALS.md) describes the project's goals and architectural
  direction.

## Where the time goes

Throughput on a half-duplex link is set by turnaround, not by baud. Timing
the acceptance run frame by frame (both stations' logs, PTT-on recovered as
`logged_time - keyed_seconds`) put a steady-state 100-byte exchange at 1200
baud at 3.91s: 2.00s of it fixed turnaround sleeps, 0.85s of PTT lead and
sound-card startup, and 0.67s -- 17% -- actually spent on payload bits.

That was measured when the chunk was 100 bytes. Chunks are now sized by the
keying budget instead ("How long one keying may be" below), which is the
direct lever on that 17%: the fixed cost per keying does not change, so
filling the keying is how it gets amortised.

Two further things address it, neither of which changes how many frames are
in flight -- the link is still stop-and-wait, one chunk acked before the
next goes out:

  - The turnaround wait is anchored on when the peer stopped transmitting,
    worked out from where in the RX buffer its last frame ended, rather
    than started when the link layer gets round to replying -- so the
    decode is absorbed by the wait instead of added to it
    (`link.TX_TURNAROUND_DELAY`, `sweep_turnaround.py`). The constant is
    reasoned, not measured; the sweep has not been run on the bench yet.
  - The decode loop discards audio it has already searched, bounding what
    a poll costs instead of letting it grow with the preceding idle. This
    feeds back into the turnaround: the reply cannot go out until the poll
    that decoded the frame returns.

### The burst attempt, and why it was rolled back

The largest item -- several DATA frames per keying under one cumulative
ACK, making the link go-back-N -- was built and then reverted. It never
worked on the bench. Two hardware runs each went backwards for a reason
the loopback tests could not see:

  - Bursts were built by concatenating separately-modulated frames, so
    every join carried a 10ms fade to silence. Fixed by modulating a
    keying as one continuous waveform.
  - Sync confidence was `peak / median(|correlation|)`, which measures the
    buffer's composition rather than the frame -- it false-synced on all
    30 off-air captures containing no sync word.
  - An ACK naming the sender's own base -- "none of that keying arrived"
    -- was classified as a stale ACK and waited out in full: 11 stalls of
    ~6.1s in a 423s transfer. That signal only exists when a keying carries
    several frames, so it did not survive the rollback; with one frame per
    keying, a chunk the peer never decoded draws no ACK at all and the
    timeout is the only signal there is.
  - Replies were keying up on top of the peer, 24 of 51 within 150ms of
    its PTT going off, because the anchor stopped at the peer's last CRC
    bit and ignored the pad and carrier still to come.

What remained after all four was still not right: the ic705->ht leg
recovered exactly one frame from 32 of its 34 two-frame bursts, the second
frame syncing at 0.97 and then failing its CRC every time, while the
reverse leg carried the same bursts fine. That is the same "sync locks,
frame does not verify" signature as the per-frame size ceilings the payload
sweeps hit on this hardware, and it is not understood. Bursting is parked
until it is.

The payload sweeps now run 100% both directions to 255 bytes, so the
per-frame ceiling is not currently reproducible -- but bursting has not been
retried, and the four bugs above were all real and separate from it. Anyone
picking it back up starts from the rollback, not from the burst branch.

Three pieces of that work were kept, because they are fixes in their own
right rather than burst machinery:

  - The normalised sync correlation (`afsk._normalised_correlation`) and
    the earliest-frame-wins sync search, which stop a loud self-echo in
    the RX buffer from masking the peer's real reply.
  - The turnaround anchoring above, including
    `link.PEER_TRAILING_TRANSMISSION`.
  - Session-scoped sequence numbers. The alternating-bit toggle they
    replaced restarted at each message boundary, so a retransmitted final
    chunk was indistinguishable from the first chunk of the next message.

A DATA_ACK now carries two sequence numbers -- the frame it answers, and
the one the receiver wants next -- rather than either alone. Only the
second was carried during the burst work, and it is ambiguous: "send me S
next" is equally "your chunk S-1 landed" and "I still want S". Since the
receiver acks every frame it decodes, duplicates included, a single lost
ACK leaves a spare copy queued at the sender, which reads as "the frame you
just sent did not arrive". The pointless retransmit that follows is itself
a duplicate, draws another spare ACK, and the link settles into two keyings
per chunk for the rest of the session. One extra byte -- ~27ms of airtime
at 300 baud -- removes the ambiguity entirely.

`WHALE_CAPTURE_DIR` is left in place: set it and the link saves the audio
behind every near-miss decode, which is the input to whatever finally
explains the ceiling.

## Losing a control frame

ARQ covers a lost DATA frame or a lost ACK, because both ends go on
agreeing about what they are doing while the retransmits happen. Control
frames are the ones that *change* what each end is doing, so losing one
leaves the two ends disagreeing -- and a retransmit repeats a
disagreement, it does not repair it. Two of those used to be
unrecoverable, and neither could be reached by the test suite as it stood.

**A lost MODE_ACK was session-fatal.** The responder moves its rx_profile
when it sends the ack; the requester moves its tx_profile only when it
receives one. Lose that frame and the peer transmits at a profile the
responder has stopped listening for -- and the only way to notice is to
decode a frame, which is exactly what has become impossible. Every DATA
frame then failed and the session died. `rx_profile` is now a hint rather
than an assertion: the pre-step profile stays a decode candidate until a
data frame settles it, and a decoded DATA/DATA_ACK is treated as ground
truth about what the peer is really sending. Recovery takes one frame,
whichever end lost the ack. Note which cases were fatal -- 600<->1200 and
any step down to the control profile. Stepping *up* from 300 always
limped on, because 300 is the control profile and every station always
tries it, and that is why the bug survived so long.

**A lost CONNECT_ACK left the session half open.** The caller retried into
a listener that had already returned from `listen_once`, where nothing
handled a CONNECT at all. The caller gave up and went IDLE while the
listener stayed CONNECTED with no keepalive, no timeout, and -- wedged in
`accept()` on its data port -- no thread that could ever notice. The
handshake is now idempotent: a retry of the session already in progress is
re-answered with the same CONNECT_ACK, byte for byte. A caller that gives
up anyway sends one DISC on its way out, so the two ends converge in
seconds rather than waiting out the backstop.

That idempotency needs to tell "a retry of the call you answered" from "I
restarted, calling again", which are otherwise the same bytes; guessing the
second resets the sequence state under a transfer with chunks in flight.
So **CONNECT and CONNECT_ACK each gained one byte**, a session identifier
the caller picks and the listener echoes. Both stations must run the same
build across that change.

`link.INACTIVITY_TIMEOUT` is the backstop for a peer that simply vanished.
It is measured, not guessed: the worst silence a healthy session produces
is a full MAX_RETRIES cycle, which timed at **44.4s** on the bench against
the 34.8s the ACK-timeout arithmetic predicts -- the five retransmissions'
own airtime and turnaround land inside the same silence. Plus an
unanswered mode step (4.3s) that puts the worst legitimate quiet at ~49s,
and the constant a little over 3x that. See `scripts/measure_peer_gap.py`.
It deliberately does not keep an *idle* session alive; that would need a
keepalive probe, and every keepalive is a keying.

### Reproducing frame loss on the bench

A real channel cannot be told to lose a chosen frame. But from the peer's
side a MODE_ACK that was never sent is indistinguishable from one that was
sent and lost, so loss is reproduced by suppressing the transmission.
Three environment hooks in `whale/link.py` do this, all off by default and
all invisible on air; the software tests drive the same code, so the bench
and the suite exercise one mechanism rather than two:

```
WHALE_DROP_PTYPE=MODE_ACK,CONNECT_ACK   packet types not to transmit
WHALE_DROP_NTH=1                        which occurrences (or "all")
WHALE_FORCE_MODE=1                      start a session at a chosen profile
WHALE_MODE_STEP_SCRIPT=1:up             step at a chosen chunk, not by luck
```

`scripts/run_acceptance_test.py` takes `--a-env`/`--b-env` to set them per
station, which is what the scenarios need -- "the responder loses its
MODE_ACK" is a different run from "both ends lose one":

```
python scripts/run_acceptance_test.py --log-dir logs/b1 --size 512 \
    --a-env WHALE_FORCE_MODE=1 --a-env WHALE_MODE_STEP_SCRIPT=1:up \
    --a-env WHALE_DROP_PTYPE=MODE_ACK \
    --b-env WHALE_FORCE_MODE=1 --b-env WHALE_MODE_STEP_SCRIPT=1:up \
    --b-env WHALE_DROP_PTYPE=MODE_ACK
```

## Why CPFSK, why these numbers

300 baud, continuous-phase binary FSK at 1200/1800 Hz: FSK carries
information in frequency, not amplitude, so it survives the AGC/limiting
this hardware chain applies on receive far better than an amplitude-coded
scheme would.

300 and 600 baud are both centred on 1500 Hz, the middle of the
~600-2300 Hz band `scripts/measure_band_edges.py` measured, rather than
wherever each profile's history left them; both share the pair 1200/1800
Hz, which is safe because the sync correlator discriminates on symbol
timing rather than tone frequency. 600 baud's separation was narrowed from
800 Hz to 600 on the bench evidence: A/B'd against 1100/1900 it decoded
100% either way but scored 2.2 dB better through a fixed measurement band
and held steadier sync confidence on large frames. Both separations now sit
on an exact multiple of their baud rate (2.0 and 1.0), which is the
orthogonality condition for the non-coherent detector this modem
uses. 1200 baud is the exception and stays at 1200/2200 Hz -- centring it
was tried at three separations on the radios and every centred variant
failed outright, while 1200/2200 passes first try. The runs and a candidate
explanation (the detector integrates one symbol, so a tone below the baud
rate gives it less than a full cycle) are in `PROFILE_1200`'s comment in
`whale/afsk.py`.

Frames carry a 63-bit PN sync word, a 16-bit length, the
payload, a CRC16, and short blocks of on-air padding before the sync word
and after the CRC, so that settling artifacts at either end of a
transmission eat padding rather than real bits. See the docstrings in
`whale/afsk.py` and `whale/framing.py` for the details, and
`whale/link.py` / `whale/transport.py` for the half-duplex, self-echo, and
WASAPI quirks this radio pair required working around.

## Air time

Everything held around a frame -- PTT lead-in, the two padding blocks, PTT
tail -- is dead air, and all of it is measured rather than guessed:
`scripts/sweep_ptt_timing.py` sweeps each allowance over the real radios in
both directions and lets the worst direction decide. The constants it
produced live in `whale/transport.py` (`PTT_LEAD`/`PTT_TAIL`) and
`whale/framing.py` (`HEAD_PAD_SECONDS`/`TAIL_PAD_SECONDS`), each with the
measurement behind it in a comment. Re-run the sweep after any change to
the radios, the audio chain, or the PTT wiring.

Re-run it in earnest, too. Swapping the bench HT -- a Wouxun KG-UV9D Plus,
briefly replaced by a Baofeng UV-B5 -- broke `PROFILE_1200` in one
direction while leaving 300 and 600 baud apparently working, and the
frame *bodies* were arriving with one bit error in 868 the whole time --
what was being lost was the sync word, and a frame nobody can sync on is
indistinguishable from one that never arrived. The new HT blacks out for
~110ms after its squelch opens (not attenuates -- in-band and out-of-band
energy are equal to within 3 dB for that whole stretch, so what arrives is
transient with no tone in it), and the 80ms head pad no longer covered it.

Only the fast profile died, and that asymmetry was the real defect. The
sync word was a fixed 63 *bits*, so it lasted 210ms at 300 baud and 52ms at
1200: a fixed-duration impairment cost the fastest profile the largest
fraction of the one thing it cannot lose. Both pads had been scaled to a
duration for exactly this reason, and the sync word between them had not.

It is now scaled too (`framing.SYNC_SECONDS`, 0.21s at every profile, using
one m-sequence order per baud), so a blackout costs every profile the same
fraction of its sync word. A lock survives losing roughly the first 40% of
it -- measured identical at all three profiles -- which gives a total
tolerance of `HEAD_PAD_SECONDS + 0.4 * SYNC_SECONDS`. On the weak leg at
1200 baud that took the head pad needed for a clean 8/8 from 280ms down to
50ms; 150ms ships, for about twice the margin that HT needs. The Baofeng is
no longer on the bench -- its battery was failing -- so these allowances are
now sized against a radio the bench cannot reproduce, which is deliberate:
the point of the exercise was to stop the fastest profile being the one with
the least margin, not to tune for whatever is plugged in today.

Two lessons for a new radio, both of which cost time here: a profile can
fail with a clean channel underneath it, and a timing allowance that
nothing on the bench needs is not thereby margin.

The binding constraint is the transmitter: the IC-705 takes 129-310ms
(variable, 28 samples) between the CI-V key command and being usably on
air, which is most of the ~430ms of lead-in. The trailing side costs
almost nothing by comparison.

Note that `link.TX_TURNAROUND_DELAY` -- a 1s pause *before* keying -- is
not air time, but it is now the largest single delay per frame, and it has
not had the same measurement treatment.

### How long one keying may be

Everything above is fixed cost per keying, so the way to spend less of it
per byte is to put more bytes in a keying. Nothing on this bench stops that
outright, but three things make an unbounded keying a bad trade: a frame is
all or nothing under stop-and-wait ARQ, so a keying is the unit one bit
error destroys; the link is half duplex, so the peer cannot interject while
we hold the carrier; and the decoder has no timing recovery, so its
tolerance to sample-clock offset falls as `0.5/n_bits` and long frames are
the first thing a clock difference takes away.

So a keying is capped in *duration* -- `afsk.MAX_KEYING_SECONDS`, 3.0s,
measured PTT key-down to PTT release -- and every profile's `chunk_size` is
whatever fits inside that cap at its baud rather than a number picked per
profile (`afsk.max_chunk_for_keying`):

| profile | chunk | AFSK payload | frame | keying | payload bits/s |
|---------|-------|--------------|-------|--------|----------------|
| 300 baud  |  75 |  77 | 2.56s | 2.99s | 201 |
| 600 baud  | 157 | 159 | 2.56s | 3.00s | 419 |
| 1200 baud | 320 | 322 | 2.57s | 3.00s | 855 |

Three things worth knowing about that table:

  - **3.0s is chosen, not measured.** None of the three reasons above has a
    cliff in it, so the figure is round rather than derived, and it is the
    knob to turn if the trade changes. If a radio ever does impose a hard
    limit -- some receivers mute themselves periodically to scan, on a timer
    set at the *far* station where this modem can neither see nor change it
    -- that would be a measurement, and it would replace this.
  - **1200 baud used to be capped by the format, not the clock.** 3.0s of
    air there carries 322 bytes, but the length field was 8 bits, so the
    chunk stopped at 253 and most of a second of every keying went unspent.
    The field is 16 bits now (`framing.LENGTH_FIELD_BITS`) and the budget
    binds at all three profiles. That was an on-air format change: stations
    built either side of it do not interoperate. It also means a declared
    length can no longer be taken on trust -- see
    `afsk.MAX_CREDIBLE_FRAME_SECONDS`. Scaling the sync word to a duration
    (`framing.SYNC_SECONDS`) was a second such break, for the same reason:
    the sync word is what a receiver locks on, so stations either side of it
    do not interoperate at 600 or 1200 baud at all. 300 baud is unchanged,
    and since that carries the control plane, a mismatched pair fails at
    CONNECT rather than half-opening a session it cannot carry data on.
  - **The clock-offset budget shrinks as the cap grows.** Sizing by airtime
    means a faster profile spends the budget on more bits, and more bits is
    what the decoder cannot keep timing across: the production frames sit at
    ~680 ppm at 300 baud, ~340 at 600 and ~169 at 1200. The two sound cards
    measure 3.4 ppm apart, so this costs nothing here, but it is the first
    thing to check on hardware whose clocks are not that close.

The budget is spent on `transport.PTT_LEAD` + the output stream's
`STREAM_FILL` + `transport.PTT_TAIL` (0.43s in total) before any frame bits
are paid for, and `whale/afsk.py` cannot import those without dragging the
sound-card stack into the DSP module. `whale/transport.py` therefore
asserts at import that its own constants still sum to
`afsk.KEYING_OVERHEAD_SECONDS`; if that fires, the chunk sizes derived from
it are over budget and need re-deriving, not silencing.

That 0.43s is measured, and measuring it mattered: it was first reasoned at
0.40 from `audio_io`'s requested stream latency, which made every derived
chunk ~20ms too big for the cap. Every transmission logs `Ns audio, Ms
keyed`, so an acceptance run reads the real figure straight off -- 0.42-0.43s
over 88 keyings across two runs, both radios, all three profiles, every
frame type, with no value outside that range. Re-read it after any change to
the audio chain, the same as `sweep_ptt_timing.py`'s constants.

Both runs passed 1 KB each way byte-for-byte with **no retransmit, no
near-miss decode and no rx-profile correction** on either leg. Those runs
predate the move to a 3.0s cap and the 16-bit length field, so their chunk
sizes were smaller than the table above; the acceptance run should be
repeated at the current sizes.

One cost this buys rather than saves: the decoder lays symbol sample points
on a rigid grid from the sync peak, with no timing recovery, so its clock
tolerance is 0.5/n_bits -- and sizing frames by airtime means a faster
profile spends its budget on more bits. Tolerance therefore *falls* as the
link speeds up: ~745 ppm at 300 baud, ~370 at 600, ~235 at 1200. The two
sound cards on this bench measure 3.4 ppm apart, ~70x inside even the
1200-baud figure, so it costs nothing here -- but it is the first thing to
check on hardware whose clocks are not this close, and
`tests/test_afsk_loopback.py` keeps the arithmetic on file.

## Dependencies

```
pip install -e .
```

Dependencies are declared in `pyproject.toml`; `requirements.txt` is kept as
a plain list of the same four. The editable install is what puts `whale` and
`acceptance_test` on the import path, so nothing under `tests/` or
`scripts/` has to reach back to the repo root by hand.

Radio control (sound card lookup, PTT keying) lives in `whale/hw/`
(`audio_io.py`, `ptt.py`, `radios.py`) -- copied in from a sibling
`radiomodem` checkout's `shark/hw/` package (hardware layer only;
whalemodem's DSP and protocol are independent). Which radios exist (device
name matching, PTT wiring, COM ports) is configured in `whale/hw/radios.py`.

## Running

Software-only self-tests (no radios needed):

```
python tests/test_afsk_loopback.py
python tests/test_link_recovery.py
```

Hardware smoke tests (need both radios connected and on the same
frequency):

```
python scripts/hw_smoke_single_frame.py   # one frame each direction
python scripts/hw_smoke_link.py           # full connect/send/disconnect
```

Full acceptance test, two station servers + a driving client:

```
python -m whale.vara_server --radio ic705 --mycall STA1 --cmd-port 8300 --data-port 8301
python -m whale.vara_server --radio ht    --mycall STA2 --cmd-port 8310 --data-port 8311
python acceptance_test.py --a-cmd 8300 --a-data 8301 --b-cmd 8310 --b-data 8311 --a-call STA1 --b-call STA2
```

## VARA-API surface implemented

Command port (line-oriented, `\r`-terminated):

```
MYCALL <call>
LISTEN ON | OFF
CONNECT <mycall> <dstcall>
DISCONNECT | ABORT
```

Status lines pushed back: `PTT ON`/`PTT OFF`, `CONNECTED <peer> <mycall>`,
`CONNECT FAILED`, `DISCONNECTED`.

Data port: once connected, raw bytes written are sent over the air; raw
bytes received are written back out. Not implemented: compression modes,
bandwidth selection, WINLINK-specific extensions.

## Known limitations (by design, for v1)

- One frame in flight at a time (stop-and-wait, not sliding-window).
- Chunk size is fixed per profile -- the largest that fits the keying
  budget, see "How long one keying may be" -- and timeouts derive from it,
  but neither adapts to how the channel is actually behaving. A lost frame
  costs a whole chunk to resend, and at 1200 baud that is now 325 bytes.
- Throughput is low; this was optimized for correctness on noisy real
  hardware, not speed.
