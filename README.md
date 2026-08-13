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
  afsk.py         CPFSK modulate/demodulate (300 baud, 700/1300 Hz tones)
  transport.py    one radio: continuous RX capture + keyed TX
  link.py         stop-and-wait ARQ: connect / send / recv / disconnect
  vara_server.py  VARA-API-shaped TCP front end (StationServer, CLI)
  hw/             sound card lookup + PTT keying (audio_io, ptt, radios)
tests/
  test_afsk_loopback.py   pure-software self-test, no hardware/radios
scripts/
  hw_smoke_single_frame.py   one AFSK frame each direction, no ARQ/sockets
  hw_smoke_link.py           full connect/send/disconnect via Link, no sockets
acceptance_test.py            drives the full acceptance scenario over TCP
```

## Why CPFSK, why these numbers

300 baud, continuous-phase binary FSK at 700/1300 Hz: FSK carries
information in frequency, not amplitude, so it survives the AGC/limiting
this hardware chain applies on receive far better than an amplitude-coded
scheme would. Frames carry a 63-bit PN sync word, an 8-bit length, the
payload, a CRC16, and a short block of on-air padding after the CRC --
padding because this hardware reliably corrupts the last symbol or two of
a transmission (radio audio tail / squelch release), and the padding gives
the real CRC bits a cushion instead of eating that corruption directly.
See the docstrings in `whale/afsk.py` and `whale/framing.py` for the
details, and `whale/link.py` / `whale/transport.py` for the half-duplex,
self-echo, and WASAPI quirks this radio pair required working around.

## Dependencies

```
pip install -r requirements.txt
```

Radio control (sound card lookup, PTT keying) lives in `whale/hw/`
(`audio_io.py`, `ptt.py`, `radios.py`) -- copied in from a sibling
`radiomodem` checkout's `shark/hw/` package (hardware layer only;
whalemodem's DSP and protocol are independent). Which radios exist (device
name matching, PTT wiring, COM ports) is configured in `whale/hw/radios.py`.

## Running

Software-only self-test (no radios needed):

```
python tests/test_afsk_loopback.py
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
- Fixed small chunk size (40 bytes/frame) and fixed timeouts, not adaptive.
- Throughput is low (300 baud); this was optimized for correctness on
  noisy real hardware, not speed.
