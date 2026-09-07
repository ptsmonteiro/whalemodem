# HR1-C: framing/ARQ redesign for a two-depth Level 0 MFSK mode

This is the point-6 design handoff requested by [`SCREEN.md`](SCREEN.md).
Revision B's guarded 16-MFSK physical layer screened well (real-receiver AWGN
transition between -24 and -23 dB waveform SNR, no impairment floor in the
three disturbed Watterson presets), but its single 19.54 s frame class and
weak tiny ACK failed the fixed 20 bit/s Level 0 session gate and did not fit
the production receive buffer. This revision keeps B's physical layer and
redesigns everything above it.

## Requirement being designed to

Two separate requirements, deliberately not merged:

1. **HF Level 0** ([`SPEED_LADDERS.md`](../../SPEED_LADDERS.md)): at least
   20 bit/s minimum sustained useful application throughput with quiet,
   moderate, and disturbed mid-latitude Watterson fading at -5 dB waveform SNR
   **and above**, inside 2,300 Hz.
2. **Deep coverage**: decode at -15 dB SNR referenced to a 3 kHz noise
   bandwidth. In this repository's waveform SNR convention (mean signal power
   over the complete half-open keying interval divided by real AWGN power over
   the 0--24 kHz Nyquist band at 48 kHz) that is **-24 dB waveform SNR**,
   because `10 log10(24000/3000) = 9.03 dB`. This is the target
   [`PLAN.md`](PLAN.md) already froze.

These cannot be met by one fixed rate. The throughput floor is stated at
-5 dB; the coverage requirement is 19 dB below it. A code rate that reaches
-24 dB delivers roughly 11 bit/s of session throughput at any SNR, and a code
rate that delivers 32 bit/s cannot reach -24 dB. Level 0 does not require
20 bit/s at -24 dB -- it requires 20 bit/s across its stated envelope, and
the deep requirement is *decode*, not rate.

HR1-C therefore ships **one waveform family with two negotiable depth
settings**, which is exactly how `whale/afsk.py` already ships one CPFSK
waveform as three negotiable modes:

| Depth | Role | Clean session rate | Design intent |
| :-- | :-- | ---: | :-- |
| HR1-N (normal) | Fills HF Level 0; HF control mode | 32.53 bit/s | Carry the rung with margin across the -5 dB envelope |
| HR1-D (deep) | Supplemental coverage below Level 0 | 10.83 bit/s | Reach -24 dB waveform SNR (-15 dB in 3 kHz) |

One receiver decodes every class of both depths in a single bounded pass, so
a peer that steps down is understood without prior notification, exactly as
the existing ladder requires.

## Physical layer: unchanged from revision B

Frozen, and deliberately not re-litigated -- `SCREEN.md` located no fault in
it:

- 16 tones on a 125 Hz grid, 375 Hz to 2,250 Hz; one real tone at a time.
- One coded symbol is 24 ms: an 8 ms silent guard covering the repository's
  7 ms maximum differential delay, then two 8 ms observations carrying the
  tone through 0.5 ms raised-cosine-squared edges.
- The receiver discards the guard observation and combines the two active
  observations coherently, then forms the exact phase-marginal symbol
  likelihood `log I0(2 sqrt(Es/N0) |z|)` over all 16 tones.
- Peak amplitude `0.13*sqrt(2)`; measured complete-keying 99% power width was
  1,969.86 Hz in revision B, inside the 2,300 Hz gate. Re-measured here.

The 2/3 active duty cycle remains a deliberate peak-limited trade: under the
waveform SNR convention silence is free, but a real peak-limited SSB
transmitter delivers 1.76 dB less average power than a constant-envelope
waveform would. That cost is unmeasurable without radios and is recorded, not
claimed away.

## What changes

### 1. Shared acquisition, separate format word

Revision B gave every frame class its own 32-symbol class word, so coarse
acquisition cost scaled with the number of classes. HR1-C has four classes
and room for more, so the preamble splits:

- **Acquisition word**: one 32-symbol balanced permutation block, identical
  for every class and depth. Coarse timing/CFO search runs once over one
  pattern -- half revision B's cost, one quarter of what four class words
  would have cost, and constant as classes are added.
- **Format word**: 8 symbols immediately after it, drawn from eight
  fixed sequences; four are assigned and four are reserved and rejected.
  It is read after timing/CFO refinement. The receiver tries the best two
  format hypotheses per surviving timing candidate, so a damaged format word
  degrades to bounded extra work rather than a lost frame.

Preamble is therefore 40 symbols (0.96 s); with the 0.128 s lead and the
0.02 s tail, fixed overhead is 1.108 s per frame.

### 2. A rate-compatible GF(16) mother code

Revision B used a two-memory rate-1/4 GF(16) convolutional inner code whose
trellis branch consumes the complete 16-tone likelihood, avoiding the BICM
loss diagnosed in [`REDESIGN.md`](REDESIGN.md). HR1-C keeps that structure
and makes the rates a **prefix family** of one mother code: outputs
`u + a_j*s1 + b_j*s2` for `j = 1..10`, where the first 2 outputs are the
rate-1/2 code, the first 4 the rate-1/4 code, the first 6 the rate-1/6 code,
and all 10 the rate-1/10 code.

The coefficient pairs are chosen by a design-time search that maximizes the
free symbol distance of every prefix, reported in priority order
`(d2, d4, d6, d10)`. Free distance is computed exactly as the minimum-weight
nonzero path from the zero state back to the zero state over the 256-state
GF(16) state diagram. The search, the chosen coefficients, and the achieved
distances are retained in the implementation handoff. Rate compatibility is
not required by any class here; it is chosen because it makes a later
incremental-redundancy ARQ possible without a second code.

### 3. A time interleaver over coded symbols

Revision B had PN tone masking, which randomizes tone indices but does not
reorder time, so a fade of a few hundred milliseconds hit consecutive trellis
steps. HR1-C interleaves the coded symbols across the whole body before tone
mapping, with a fixed multiplicative permutation
`air[i] = coded[(i*stride) mod B]`, `stride` coprime with the body length `B`
and near `0.618*B`. The deinterleaver runs before the trellis, so a fade
becomes scattered symbol erosion the code is built for. This is expected to
close part of revision B's ~4 dB AWGN-to-disturbed gap and is the main reason
HR1-C's fading boundaries are not assumed to equal B's.

### 4. Block-adaptive metric scaling

Revision B estimated one likelihood scale for the whole frame from the class
word. That is AWGN-aligned and wrong over a 29 s frame in 1 Hz fading. HR1-C
estimates noise and signal power per block of 48 air-order symbols (1.152 s)
from the median bin power and the median per-symbol maximum, falling back to
the preamble estimate for short or degenerate blocks. Estimation is in air
order, before deinterleaving, because that is the order the channel acted in.

### 5. Frame classes sized from the session budget, not the other way round

The class geometry below is forced, not chosen. `HF_SSB.max_useful_frame_seconds`
is 8.0 s. At rate 1/3 the largest payload that fits 8 s yields 19.5 bit/s of
session throughput and **fails** the Level 0 floor; at rate 1/2 the full
64-byte physical payload fits in 7.924 s and clears it with 63% headroom.
That is the entire justification for HR1-N's rate.

| Class | Physical payload | Checked packet | Outer code | Inner rate | Inputs | Body tones | Keying |
| :-- | ---: | ---: | :-- | ---: | ---: | ---: | ---: |
| `CTL_N` | 12 B | 18 B | none | 1/4 | 38 | 152 | 4.756 s |
| `DATA_N` | 64 B | 70 B | none | 1/2 | 142 | 284 | 7.924 s |
| `CTL_D` | 12 B | 18 B | none | 1/10 | 38 | 380 | 10.228 s |
| `DATA_D` | 64 B | 70 B | RS(96,70) | 1/6 | 194 | 1164 | 29.044 s |

Checked packet is `[uint16 length][payload][CRC32(payload)]`, whitened with
the repository order-17 PN sequence seeded `0x0C4B1`, then GF(16)-encoded with
two zero termination symbols. A decode is accepted only after termination,
RS (where present), length, CRC32, and zero-fill all check.

`CTL_*` carries 12 bytes, which is the 10-byte link air header plus its two
inline bytes: DATA_ACK, DISC, DISC_ACK, FLOOR_REQ, FLOOR_GRANT, TIMING_ACK
and TIMING_CONFIRM all fit. CONNECT and CONNECT_ACK are ~32 bytes and use the
DATA class of the same depth, as they do on HC0 today.

**The ACK class is deliberately over-protected relative to the DATA class it
serves.** That is the specific defect `SCREEN.md` found: revision B's tiny
class transitioned about 1 dB *above* its full class, so ACK loss, not DATA
loss, set the session rate. Per information symbol, `CTL_N` spends 4 tones
against `DATA_N`'s 2, and `CTL_D` spends 10 against `DATA_D`'s 6. The screen
must confirm empirically that each ACK class is at least as reliable as its
DATA class at every tested point; a miss is a redesign of the ACK class, not
a renegotiated target.

### 6. Session budget

One clean stop-and-wait exchange is `DATA + ACK + 2 x 0.3 s` turnaround:

| Depth | DATA | ACK | Turnaround | Exchange | Clean session rate |
| :-- | ---: | ---: | ---: | ---: | ---: |
| N | 7.924 s | 4.756 s | 0.600 s | 13.280 s | **32.53 bit/s** |
| D | 29.044 s | 10.228 s | 0.600 s | 39.872 s | 10.83 bit/s |

Level 0 needs 20 bit/s. Under the pessimistic retry model `SCREEN.md` used
(a failed attempt costs the DATA transmission plus a full
`DATA + ACK + 2 turnarounds + 5 s` timeout), HR1-N still returns 25.1 bit/s
at a 10% DATA FER and 5% ACK FER, and 20 bit/s is not reached until the
combined exchange failure rate is about 22%. Inside the -5 dB envelope the
expected margin is 8 dB or more, so this is headroom, not a knife edge. The
clean projection is not the claim; the measured session number is.

## Predicted boundaries

Derived from revision B's screened results by rate accounting alone, stated
before the campaign so the campaign can falsify them:

| Quantity | Revision B (measured) | HR1-N (predicted) | HR1-D (predicted) |
| :-- | ---: | ---: | ---: |
| Frame useful rate | 22.11 bit/s | 54.52 bit/s | 14.87 bit/s |
| Eb/N0 at its AWGN edge | 6.85 dB | -- | -- |
| Canonical AWGN edge | -23.5 dB | about -19 dB | about -25 dB |
| `mid_latitude_disturbed` edge | -19.5 dB | about -15 dB | about -21 dB |

The interleaver and block-adaptive scaling are expected to improve the fading
columns; the AWGN columns should be close to the rate accounting. The Level 0
requirement is -5 dB, so HR1-N is predicted to hold the envelope with about
10 dB of margin, and the deep requirement is -24 dB, which HR1-D is predicted
to clear in AWGN by about 1 dB.

**HR1-D is not predicted to reach -24 dB in disturbed fading**, and this
design does not claim it will. `PLAN.md`'s own stretch row asks for -20 dB
fixed-N0 `mid_latitude_disturbed`, which matches the prediction above. The
honest form of the deep claim is a decode boundary per named channel, never a
single number.

## Production integration consequences

HR1-N integrates with no production constant changed: its 7.924 s keying is
inside the 8.0 s HF useful-frame budget, and the link's derived
`_rx_keep_seconds` of 8.924 s is inside `transport.RX_BUFFER_SECONDS` of
10.0 s.

HR1-D does not. Its 29.044 s keying implies a 30.044 s keep window, so the
receive buffer must be able to hold the longest frame the negotiated ladder
can carry. The fix is to derive the buffer length from that ladder rather
than raise a module constant blindly, with the present 10.0 s as the floor;
at 12 kHz a 32 s buffer is about 3 MB, well inside the 256 MiB qualification
limit. The ACK timeout and inactivity arithmetic must also be reviewed for
the deep depth: at `max_retries` 10 an unanswered deep DATA frame spends
about 445 s against a 300 s inactivity timeout. Until those are settled and
measured, HR1-D is experimental only.

## Mode identity

`experiments/hr1/` continues the local `EXPERIMENTAL_MODE_ID` sequence started
by `hr1.py`'s 240 and `hr1b.py`'s 241 and continued by `hf2`'s 242, `hf3`'s
243, and `hf4`'s 244 (see those experiments' `benchmark_*.py` bench-mode
adapters); the next free local IDs are 245 (N) and 246 (D).

This design was previously drafted as "HR0", but that name and the
production mode ID it expected (10) are already taken: `whale/modes/hr0.py`
is an unrelated, already-shipped 128-tone FSK Level 0 control mode
(`hf-ssb` mode ID 10, `DEFAULT` registry status), added independently of this
experiment line. The `hf-ssb` production IDs actually free in
`whale.mode_qualification.MANIFEST` are 6, 8, and 12 and above -- not 7,
which is HF2's. Integration would assign two of those free IDs. The mode ID
travels in the link air header, which is opaque payload to this waveform, so
the assignment does not change the wire and does not invalidate screen
artifacts.

## What would falsify this design

- HR1-N misses any point of the Level 0 envelope, or its measured median
  session throughput is below 20 bit/s at any claimed point.
- Either ACK class is measurably less reliable than the DATA class it serves.
- HR1-D does not decode at -24 dB canonical AWGN.
- Occupied 99% power bandwidth exceeds 2,300 Hz.
- Any checked false payload, unbounded search, or exception.
