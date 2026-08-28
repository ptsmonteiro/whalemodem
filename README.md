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
  link.py         stateful stop-and-wait ARQ: connect / send / recv / disconnect
  link_protocol.py  pure link packet constants, validation, and serialization
  vara_server.py  VARA-API-shaped TCP front end (StationServer, CLI)
  hw/             sound card lookup + PTT keying (audio_io, ptt, radios)
  policy.py       one channel's worth of assumptions, incl. its mode ladder
  modes/          physical-layer modes that are not CPFSK
    vf3.py          58-carrier differential-QPSK OFDM, 1436 B in 5.2s
    vf3_mode.py     VF3 as a negotiable WaveformMode (mode 3)
    hc1.py          19-carrier DQPSK OFDM for HF SSB, offset-corrected
    hc1_mode.py     HC1 as a WaveformMode, the fast HF rung (mode 4)
    hc0.py          16-FSK for HF SSB: non-coherent, works 19 dB lower
    hc0_mode.py     HC0 as a WaveformMode, and the HF ladder (mode 5)
tests/
  link_harness.py         two Links against each other, in one process
  test_afsk_loopback.py   pure-software self-test, no hardware/radios
  test_link_recovery.py   what a *lost control frame* does to a session
  test_vf3_mode.py        the mode contract VF3 has to satisfy to be negotiable
  test_hc1_mode.py        the same contract for HC1, plus the HF properties
  test_hc0_mode.py        the same for HC0, plus the margin it exists for
  test_hc1_capture_replay.py  HC1 against audio recorded off a real HF path
  test_hc0_capture_replay.py  HC0 against both legs of the same HF bench
  test_audio_e2e.py       the full TCP stack over paired audio, CPFSK and VF3
scripts/
  bench.py                   the rig the sweeps share: radio pair, trial loop, walk
  hw_smoke_single_frame.py   one AFSK frame each direction, no ARQ/sockets
  hw_hf_frames.py            HC0/HC1 frames over the HF pair, HF diagnostics
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

Protocol version 4 also carries the decoded DATA mode and an absolute
head-duration request in that ACK. The request is piggybacked feedback, not an
extra exchange.

`WHALE_CAPTURE_DIR` is left in place: set it and the link saves the audio
behind every near-miss decode, which is the input to whatever finally
explains the ceiling.

## Going faster than CPFSK: VF3

`experiments/` holds several attempts at more throughput per keying, all
measured on the same two radios and all summarised in their own RESULTS.md.
The ranking is not close:

| mode | net user bit/s | on-air evidence |
| --- | ---: | --- |
| shipped `PROFILE_1200` | 947 | acceptance runs |
| `experiments/mfsk` `4fsk_650bd` | 1,011 | 45/45 each direction |
| `experiments/ofdm` BPSK | 915 | 28/28 |
| `experiments/vf3` 58-carrier DQPSK | ~2,200 | 6/6 each direction |
| `experiments/vf4` star-8-QAM + RS | ~2,945 | 6/6 each direction |
| `experiments/vf5` 16-QAM + pilots + RS | ~3,663 | 6/6 each direction |

VF3 is the first of those to be reachable from the modem rather than only
from a bench script. `whale/modes/vf3_mode.py` presents it as a
`WaveformMode`, which is the interface `whale/waveform.py` defines and
`whale/link.py` already negotiates, steps up and down, and confirms through
DATA_ACK. Nothing in the link, the ARQ or the negotiation changed to admit
it -- which is the claim `GOALS.md` makes for that boundary, tested rather
than asserted.

The DSP moved into the package unchanged; `experiments/vf3/vf3.py` is now a
shim over it, so that experiment's own tests and its stored captures still
run against the same code the modem would transmit.
`tests/test_vf3_mode.py` asserts the default head still produces a
bit-identical waveform, which is what keeps that true.

Three things the adapter had to reconcile, all recorded in its docstring:

  - **VF3 does the framing CPFSK asks `whale/framing.py` for.** Its payload
    grid already carries a length, a CRC32 and a rate-1/2 convolutional code,
    and it acquires on an OFDM header rather than a PN correlation. So the
    link's air header is simply the first ten bytes of a VF3 payload.
  - **The head is adaptive, and had to stay core-periodic.** The link
    negotiates a leading guard per direction against the receiver's squelch
    blackout. VF3 already had one in miniature -- 45 ms of the sync symbol's
    1024-sample *core*, repeated -- so the adaptive head is that same
    construction made longer. Building it from repeated *symbols* instead
    looks equivalent and is not: acquisition locks by correlating the signal
    against itself one symbol apart, so a symbol-periodic head extends that
    correlation's plateau across the whole head and leaves the ranking step a
    single arbitrary offset inside it to score.
  - **Head feedback is expressed in seconds, not symbols.** VF3 counts the
    surviving head cores and converts that observation to seconds for the
    link. Its `head_match_allowance_seconds` is one 21.3 ms core, matching the
    measurement's actual resolution. CPFSK supplies its own allowance in the
    same unit, so `_head_feedback_request` is waveform-independent and VF3 can
    lengthen its ordinary-frame head when the receiver observes leading loss.

### It carries a session

`tests/test_audio_e2e.py` runs the whole TCP stack with both the CPFSK-only
ladder and the default VHF ladder, whose top rung is VF3: connect, 4 KB each
way, disconnect, through both StationServers, both ModemServices, ARQ and the
real modulate/demodulate, with only the sound cards replaced. Both directions
climb 300 -> 600 -> 1200 -> VF3 on their own -- nothing pins the mode -- and
each station's `rx_profile` agrees with its peer's `tx_profile`, so the
mode-confirmation path in DATA_ACK is exercised rather than assumed.

The test measures **airtime**, the seconds each station was keyed, because
the transports hand audio over instantly and wall-clock time here means
nothing:

| bytes each way | CPFSK ladder | with VF3 | | net application rate |
| ---: | ---: | ---: | ---: | --- |
| 4,000 | 93.9s | 66.6s | 1.41x | 681 -> 960 bit/s |
| 16,000 | 315.7s | 161.0s | 1.96x | 811 -> 1,590 bit/s |

That is below the 2.3x the frame arithmetic suggests, for two reasons the
measurement makes visible and the frame arithmetic hides:

  - **The climb is a fixed cost.** Reaching VF3 takes one clean chunk at each
    of 300, 600 and 1200 baud, about 15s of air, whatever the transfer is
    worth. It is most of the difference between the two rows above, and it is
    why mode history (`whale/mode_history.py`) matters more once a fast mode
    exists than it did when the top rung was 1200 baud.
  - **The ACK is now 12% of a chunk.** A 5.2s VF3 keying is answered by a
    0.70s DATA_ACK on the 300-baud control plane. That was a rounding error
    against a 3.0s chunk carrying 402 bytes and is not one against a 5.2s
    chunk carrying 1,426. The control plane has to stay robust, but it does
    not have to stay 300 baud for an ACK the peer just proved it can decode a
    faster mode from.

**VF3 is on the default VHF ladder.** `afsk.default_registry()` intentionally
remains CPFSK-only for callers and tests concerned with that waveform family;
`whale.modes.default_registry()` appends VF3 and is what the `vhf-fm` channel
actually uses. VF3 has carried full acceptance-test sessions over the radios
in both directions without ARQ retries. Mode negotiation keeps mixed-capability
peers compatible: a peer that does not advertise mode 3 stays on the mutually
supported CPFSK rungs.

One consequence worth stating plainly: a VF3 keying is 5.2 s, against the
3.0 s of useful audio the CPFSK profiles are sized to. That cap is CPFSK's
own -- see "How long one keying may be" -- and the reasons behind it are
answered differently by a waveform with a cyclic prefix and per-carrier
equalisation. Keyings of around five seconds are acceptable here.

## HF: what mode 0 could not do there

Everything above this line was built, measured and accepted against one
channel: two FM handhelds on 2 m, a few metres apart. `whale/policy.py`
already separated the numbers that are facts about *that channel* from the
ones that are facts about the protocol. What it could not separate was the
waveform, because there was only ever one family of them.

There are now two more, and an HF ladder built out of them, because three
things mode 0 relies on are simply not true on sideband:

  - **Frequency is exact.** An FM receiver reproduces the transmitted audio
    frequency. Two SSB receivers reproduce it offset by the difference between
    the stations' reference oscillators plus whatever the dials disagree
    about. `afsk.demodulate` integrates each tone in a fixed bin and has no
    frequency estimate anywhere in it.
  - **A frame either arrives or it does not.** CPFSK framing has a CRC and no
    correction, so one wrong bit costs the whole keying and a retransmit.
  - **The channel has no memory.** An HF path has a millisecond or two of
    delay spread; nothing on the FM bench had any.

The first answer was **HC1** (`whale/modes/hc1.py`, mode 4): a 19-carrier
differential-QPSK OFDM frame over 656-2344 Hz, with a 2.67 ms cyclic prefix,
an interleaved rate-1/2 K=7 convolutional code, a CRC32, and -- the part that
was genuinely new -- a carrier frequency offset that is *estimated and
removed* rather than reported. 74 payload bytes, of which 64 are a DATA chunk,
in a fixed 0.695 s keying. Full geometry in
[`FRAMING.md`](FRAMING.md#mode-4-hc1-the-fast-hf-data-mode).

It is a good frame, and it is not the one a control plane wants; the next
section is why, and what replaced it there.

It is geometry and wiring: every transform is a `whale/dsp/` kernel. The two
frequency estimators in `whale/dsp/freq.py` were extracted alongside the
others and, at the time, used by nothing -- that module's docstring ends "a
coherent HF waveform would correct with them before analysis". This is that
waveform.

On the bench's strong leg HC1 does exactly what it was built to do: **10/10**
full-payload frames and **4/4** ACK-sized ones byte-for-byte, 0.00% raw BER on
every one, all 19 carriers present, and the carrier offset measured at -7.8 to
-8.8 Hz on every single frame. Two of those captures are committed under
`tests/data/hc1_captures/` and replayed by
`tests/test_hc1_capture_replay.py`.

On the weak leg it decoded nothing at all, and that is what the next section
is about.

### HC0: the rung that gets through

HC1 was the wrong shape for the job it was first given. It was the *control*
mode, and a control mode is the one thing on a link that has to work when
nothing else does -- so the number that matters for it is not how fast it is
but how far into the noise it still decodes. Measured, at equal transmitted
RMS with white noise across the whole band:

| | works down to |
| --- | ---: |
| HC1's payload, handed the true frame start | -4 dB |
| HC1 as actually decoded | **+3.5 dB** |
| what the bench's weak leg delivers | about -8 dB |

The second row is the interesting one, and it is not a tuning problem. HC1's
confidence is the normalized self-correlation of its repeated sync symbols,
whose expected value is exactly `SNR/(SNR+1)`. A 0.70 threshold is therefore a
3.7 dB SNR floor by construction, and lengthening the preamble does not move
it -- that shrinks the correlation's variance, not its mean. Everything
downstream then leans on a carrier-offset estimate that is itself unusable
down there: correcting by a bad estimate destroyed a coherent header match
that would otherwise have worked 12 dB further down.

The fix is not a better phase estimator. It is to stop needing phase.

**HC0** (`whale/modes/hc0.py`, mode 5) is non-coherent 16-ary FSK: information
is which of 16 tones is present, detected as energy, so nothing in the receive
path holds a phase reference -- not the demodulator, not the synchronizer, and
not the frequency estimator, which measures the offset but is never gated on
it. Detection is a correlation against a known tone *pattern*, whose
processing gain grows with its length in the ordinary way. Behind the tone
detector sits the same interleaver, rate-1/2 K=7 convolutional code and
length/CRC32 packet the OFDM modes use, unchanged.

    [head][24 preamble symbols][283 payload symbols][tail] = 3.380 s, 64 B

**HC0 decodes to -16 dB where HC1 fails below +3.5 dB.** About 7 dB of the
19.5 is spending five times the airtime; the rest is not paying for coherence.
And that is at equal RMS -- HC0 is constant-envelope, crest factor 1.41
against HC1's 3.9, so through the same peak-limited transmitter it puts
roughly 8 dB more average power on the air again. Full geometry in
[`FRAMING.md`](FRAMING.md#mode-5-hc0-the-hf-control-mode).

So the HF ladder is HC0 as the control mode and bottom rung, HC1 above it for
a path that can carry it -- the same shape as the FM ladder, for the same
reason.

### The HF bench, both modes

IC-7300 and IC-705, both on 10.145 MHz USB in data mode, antennas in the same
room. `scripts/hw_hf_frames.py`, ARQ bypassed, one modulate -> TX -> capture
-> demodulate per trial.

The bench is about 30 dB asymmetric -- the IC-705's antenna port radiates and
hears that much worse than the IC-7300's -- which turned out to be the most
useful thing about it, because it supplies a genuinely weak path to test
against:

| leg | tone/carrier SNR | HC1 | HC0 |
| --- | ---: | ---: | ---: |
| ic7300 -> ic705 | 37 dB | 10/10 | 5/5 |
| ic705 -> ic7300 | 14 dB | **0/10** | **5/5 + 6/6** |

Every HC0 frame byte-for-byte. The strong leg arrived with no raw bit errors
at all; the weak leg -- the one HC1 could not carry -- with 2 in 1,132, which
a rate-1/2 K=7 code does not notice. Acquisition scored 0.38-0.50 against a
0.12 threshold in both directions.

Both legs measure the carrier offset at about 8 Hz, opposite in sign: a real
difference between two crystal oscillators, and the quantity no CPFSK profile
in this repo can see. One capture from each direction is committed under
`tests/data/hc0_captures/` and replayed by `tests/test_hc0_capture_replay.py`,
so the on-air result is a regression test rather than a story.

### The acceptance test, on HF

```
python scripts/run_acceptance_test.py --channel hf-ssb \
    --a-radio ic7300 --b-radio ic705 --size 512
```

**Passes.** Connect, 512 bytes each way verified byte-for-byte, disconnect,
all over real radios on HF SSB. Every control frame on HC0; the data plane
climbed to HC1 and stayed there; every chunk acked on its first attempt.
Decode cost 15-31 ms per frame, and 19 ms for a poll of a 10 s buffer with
nothing in it.

Two things are honestly not done. The successful bench acceptance run validates
the HF waveforms and end-to-end protocol path, but not `HF_SSB`'s timeout,
retry, turnaround, or adaptation constants; those remain reasoned placeholders
without dedicated measurements. (`max_useful_frame_seconds` is accepted and
ignored by the fixed-geometry HF modes.) And `require_clear_channel` is set on
that policy and enforced by nothing, because this codebase has no busy-channel
detector: a station on `--channel hf-ssb` transmits without listening first,
which is fine on a bench pair and is not fine on a shared band.

## Losing a control frame

ARQ covers a lost DATA frame or a lost ACK, because both ends go on
agreeing about what they are doing while the retransmits happen. Control
frames are the ones that *change* what each end is doing, so losing one
leaves the two ends disagreeing -- and a retransmit repeats a
disagreement, it does not repair it. Two of those used to be
unrecoverable, and neither could be reached by the test suite as it stood.

**Mode changes no longer have a separate control exchange.** The ISS changes
its own DATA mode and the IRS searches every mutually advertised mode. The
next DATA_ACK carries the mode in which DATA decoded, confirming delivery and
speed together. After three unanswered attempts, the ISS steps down and
retries the same sequence at the lower speed; this works even when no frame at
the previous speed reached the IRS.

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
own airtime and turnaround land inside the same silence. The 150s constant
is a little over 3x that. See `scripts/measure_peer_gap.py`.
It deliberately does not keep an *idle* session alive; that would need a
keepalive probe, and every keepalive is a keying.

### Reproducing frame loss on the bench

A real channel cannot be told to lose a chosen frame. But from the peer's
side a frame that was never sent is indistinguishable from one that was sent
and lost, so loss is reproduced by suppressing the transmission. Three
environment hooks in `whale/link.py` do this, all off by default and
all invisible on air; the software tests drive the same code, so the bench
and the suite exercise one mechanism rather than two:

```
WHALE_DROP_PTYPE=DATA_ACK,CONNECT_ACK   packet types not to transmit
WHALE_DROP_NTH=1                        which occurrences (or "all")
WHALE_FORCE_MODE=1                      start a session at a chosen profile
WHALE_MODE_STEP_SCRIPT=1:up             step at a chosen chunk, not by luck
```

`scripts/run_acceptance_test.py` takes `--a-env`/`--b-env` to set them per
station. For example, suppressing the first three DATA frames forces the
silent-downgrade path:

```
python scripts/run_acceptance_test.py --log-dir logs/b1 --size 512 \
    --a-env WHALE_FORCE_MODE=1 --a-env WHALE_DROP_PTYPE=DATA \
    --a-env WHALE_DROP_NTH=1,2,3
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

Frames carry a duration-scaled PN sync word, a 16-bit length, the
payload, a CRC16, and known PN padding before the sync word, so that leading
settling artifacts eat padding rather than real bits. See the docstrings in
`whale/afsk.py` and `whale/framing.py` for the details, and
`whale/link.py` / `whale/transport.py` for the half-duplex, self-echo, and
WASAPI quirks this radio pair required working around.

## Air time

Leading clipping protection is entirely in-band. Every calibration keying
carries one second from a protocol-fixed PN sequence before its first sync;
ordinary frames use a sync-anchored suffix at the per-direction adaptive
duration. PTT is asserted immediately
before output-stream setup and released immediately after confirmed playout;
there is no configured carrier-only lead or tail sleep. This deliberately
conservative baseline is replaced at connection time by a per-direction
duration. During transfer, every valid DATA frame reports its transmitted head
duration and DATA_ACK piggybacks an absolute request derived from the observed
residual. Requests only increase the connection value, are idempotent across
retries, and are capped at 1.00 s. There is no tail guard or tail PN sequence;
audio ends at the final CRC. Connection protocol version 4 and air-header
version 2 make this explicit and are not wire-compatible with version 3.

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
fraction of the one thing it cannot lose. The head pad had been scaled to a
duration for exactly this reason, and the sync word after it had not.

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

The measured IC-705 startup variation is now absorbed by the transmitted head
symbols rather than a carrier-only PTT lead.

Note that `link.TX_TURNAROUND_DELAY` -- a nominal 0.4s pause before keying -- is
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

Useful framed audio is capped at three seconds by
`afsk.MAX_USEFUL_FRAME_SECONDS`. This includes sync, length, payload, and CRC
across the checked header and optional body, but excludes the outer head pad and
transport startup. Every profile's `chunk_size` is whatever fits that useful
budget (`afsk.max_chunk_for_useful_frame`). Total PTT occupancy is separate
because adaptive head timing makes it radio-pair dependent:

| profile | chunk | AFSK payload | useful | current keying | payload bits/s |
|---------|-------|--------------|-------|--------|----------------|
| 300 baud  |  88 |  88 | 3.04s | 4.20s | 137 |
| 600 baud  | 193 | 193 | 3.03s | 4.19s | 299 |
| 1200 baud | 402 | 402 | 3.01s | 4.17s | 623 |

Three things worth knowing about that table:

  - **3.0s of useful audio is chosen, not measured.** None of the three reasons above has a
    cliff in it, so the figure is round rather than derived, and it is the
    knob to turn if the trade changes. If a radio ever does impose a hard
    limit -- some receivers mute themselves periodically to scan, on a timer
    set at the *far* station where this modem can neither see nor change it
    -- that would be a measurement, and it would replace this.
  - **1200 baud used to be capped by the format, not the clock.** The former 3.0s of
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

The output stream contributes about 0.16s of startup/fill time to total
occupancy, but not to the useful-frame restriction. A packet contains one sync
and one continuous waveform. Its header and optional body have separate CRCs,
and the adaptive head remains outer-keying protection. No tail symbols are
appended after the final CRC.

Both runs passed 1 KB each way byte-for-byte with **no retransmit, no
near-miss decode and no rx-profile correction** on either leg. Those runs
predate the move to the duration-derived cap and the 16-bit length field, so their chunk
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
pytest tests/test_audio_e2e.py -q       # full TCP stack over paired audio, CPFSK, VF3 and HC1
pytest tests/test_vf3_mode.py -q         # the VF3 WaveformMode contract
pytest tests/test_hc1_mode.py -q         # the same for HC1, plus offset/multipath/FEC
pytest tests/test_hc0_mode.py -q         # the same for HC0, plus the low-SNR margin
pytest tests/test_hc0_capture_replay.py -q   # HC0 against recorded on-air HF audio
pytest tests/test_hc1_capture_replay.py -q   # and HC1 against its own
python tests/test_afsk_loopback.py
python tests/test_link_recovery.py
```

Hardware smoke tests (need both radios connected and on the same
frequency):

```
python scripts/hw_smoke_single_frame.py   # one frame each direction
python scripts/hw_smoke_link.py           # full connect/send/verified clean disconnect
python scripts/hw_hf_frames.py --mode hc0  # HF frames over an HF pair (ic7300/ic705)
```

On HF, start both stations on the HF channel -- which selects HF timeouts and
the HC1 ladder -- with `--channel hf-ssb`:

```
python scripts/run_acceptance_test.py --channel hf-ssb     --a-radio ic7300 --b-radio ic705 --size 1024
```

Full acceptance test, two station servers + a driving client:

```
python -m whale.vara_server --radio ic705 --mycall STA1 --cmd-port 8300 --data-port 8301
python -m whale.vara_server --radio ht    --mycall STA2 --cmd-port 8310 --data-port 8311
python acceptance_test.py --a-cmd 8300 --a-data 8301 --b-cmd 8310 --b-data 8311 --a-call STA1 --b-call STA2
```

For each direction, the acceptance test reports elapsed transfer time and net
throughput in useful application-payload bits per second. Protocol overhead
and retransmitted data are therefore not counted as useful throughput.

## Radio and PTT configuration

Radio hardware is selected from a TOML inventory rather than requiring a
source edit. See `radios.example.toml`, then run the server with
`--radio-config PATH --radio NAME` (or set `WHALE_RADIO_CONFIG`). Each entry
selects an audio-device substring, a PTT backend, backend-specific settings,
and backend-specific settings.

Built-in backends are `icom-civ`, `serial-line` (RTS or DTR), `hamlib`, and
`vox`. External packages can provide GPIO, USB-interface, CAT, or other
station-specific implementations with a `whalemodem.ptt_backends` Python
entry point. Its object implements `PttBackend` from
`whale.hw.ptt_backends`; applications may also call `register_backend()`.

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
  costs a whole chunk to resend, and at 1200 baud that is now 402 bytes.
- Throughput is low; this was optimized for correctness on noisy real
  hardware, not speed.
