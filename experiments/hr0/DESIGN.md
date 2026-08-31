# HR0 point 2: architecture screen and first-pass design

## Decision and claim boundary

**Go to an oracle-aligned screen with HR0-A**, a guarded, sequential,
constant-envelope 16-MFSK waveform with a rate-1/3 convolutional code,
whole-frame interleaving, time-varying tone labeling, periodic known tones,
and three fixed frame classes. It is not a 32-carrier waveform and it is not
32-FSK.

This is an architecture decision, not a robustness result. No HR0 waveform,
receiver, Monte Carlo boundary, radio result, or VARA comparison exists yet.
`PLAN.md` remains the qualification protocol. In particular, the published
VARA row gives only 23 symbols/s, 32 carriers, FSK, and 18 bit/s net; it does
not disclose enough to reconstruct its modulation, code, framing, or SNR
threshold.

The first screen should stop or pivot unless HR0-A is simultaneously capable
of:

- the `-24 dB` canonical-AWGN target with a real 54-byte DATA body;
- the `-20 dB` fixed-N0 disturbed-mid-latitude target;
- an explicit result on the 7 ms/30 Hz high-latitude case; and
- at least 18 bit/s measured long-session useful rate after ACKs, retries,
  turnaround, and acquisition overhead.

The current arithmetic leaves margin over 18 bit/s only in a clean
stop-and-wait exchange. It therefore establishes feasibility, not the session
rate gate.

## Repository facts that constrain the choice

The design was cross-checked against `GOALS.md`, `PLAN.md`, the current HC0
mode/adapter, `whale.dsp.mfsk`, `whale.dsp.fec`, `whale.dsp.framing`,
`whale.dsp.interleave`, `whale.modes.hf_lead`, `whale.policy`, and the exact
`WATTERSON_PRESETS` in `whale.channel`.

- HC0 is one-of-16 noncoherent MFSK: exactly one tone is transmitted at a
  time. Its 93.75 symbols/s and 93.75 Hz tone spacing are tied to a 512-sample
  FFT interval. It is constant envelope, uses soft rate-1/2 K=7 Viterbi,
  CRC32, whitening, and a whole-frame multiplicative interleaver.
- HC0's full physical payload is 64 bytes. The link consumes 10 of those as
  its checked air header, leaving the documented 54-byte DATA body.
- The current common HF lead floor is 128 ms. It is an advisory label;
  successful checked body decoding remains authoritative.
- HF policy inserts 0.3 seconds of turnaround in each direction.
- The repository Watterson registry spans differential delays from 0.5 to
  7 ms and 2-sigma frequency spreads from 0.1 to 30 Hz. The hardest joint
  point is `high_latitude_disturbed` at 7 ms and 30 Hz, not one of the three
  mid-latitude aliases.
- `PacketCodec` and `ConvolutionalCode` currently assume rate 1/2. A
  rate-1/3 experiment can reuse their framing and vectorized Viterbi ideas,
  but cannot be represented by the production classes unchanged.
- The MFSK `ToneBank` currently makes tone dwell, FFT interval, symbol rate,
  and tone spacing the same number. HR0-A deliberately separates a 24 ms
  tone dwell from 8 ms observations and therefore needs a small experimental
  kernel rather than a misleading `ToneBank` configuration.

The retained HC0 notes place its clean-AWGN edge near -16 dB, but point 1 did
not turn that note into a new HR0 result. The HC0 boundary campaign required
by `PLAN.md` remains the baseline for any later relative claim.

## “32 carriers” is not “32-FSK”

These descriptions imply different signals:

| Description | Signal in one symbol | Uncoded bits/symbol | Envelope consequence |
| --- | --- | ---: | --- |
| 32 parallel BFSK carriers | 32 simultaneous subchannels, each selecting mark or space | 32 | multitone sum; high crest factor unless constrained |
| one-of-32 MFSK (“32-FSK”) | exactly one of 32 tones | 5 | one tone; constant envelope is straightforward |

If the VARA row means 32 parallel one-bit FSK subchannels, the geometry has a
nominal 736 coded bit/s before its undisclosed redundancy and overhead. If it
meant one-of-32 MFSK at 23 symbols/s, it would have only 115 coded bit/s. The
vendor row does not authorize choosing either interpretation as fact. HR0's
32-carrier comparison below is therefore an explicit hypothetical geometry,
not a claim about VARA's wire format.

## Family screen

The energy comparison assumes the same complete-keying waveform-SNR
definition from `PLAN.md`. “Constant envelope” refers to the generated audio;
an SSB transmitter can still add compression, filtering, and nonlinearities.

| Family | Energy/bit and bandwidth | Delay/Doppler and interference | Acquisition, crest factor, complexity | Decision |
| --- | --- | --- | --- | --- |
| Hypothetical 32 parallel BFSK carriers on a 23 Hz orthogonal grid | Plenty of raw rate to spend on code/repetition; roughly 32 simultaneous tones over a sub-kHz-to-2 kHz grid | A 30 Hz 2-sigma spread is 1.30 times a 23 Hz grid, so high-latitude ICI is a first-order risk; a CP/guard can cover 7 ms; carriers give frequency diversity but CW/notches can erase persistent subchannels | FFT receiver is cheap, but multitone PAPR costs average power in a peak-limited SSB chain; CFO/clock tracking resembles OFDM | Reject as baseline. Retain only if measured transmitter backoff is small and a wider-spaced realization survives 30 Hz. |
| One-of-M orthogonal MFSK | Excellent noncoherent energy efficiency as M grows and one-tone constant envelope; bandwidth grows as M times spacing | Sequential hopping plus interleaving spreads notches and fades; ordinary 16/32-FSK ties dwell to spacing and cannot optimize 7 ms delay and 30 Hz spread independently | Known-tone acquisition and energy detection reuse HC0 ideas; bounded FFT/filter-bank work | **Selected, with separate guard/observation timing.** |
| HC0 geometry plus much stronger FEC/repetition | Direct reuse; every extra factor of two in airtime gives nominal 3 dB more useful-bit energy | Known HC0 geometry has 7 ms equal to 66% of a symbol in the worst preset; frequency hopping helps CW but there is no deliberate delay guard | Lowest implementation risk and constant envelope | Ranked alternate 1 if the new guarded geometry loses more to discarded guard energy than it saves in fading. |
| Coherent/differential PSK, single carrier or OFDM | BPSK has an attractive AWGN limit and good spectral efficiency, allowing very low-rate codes | The 30 Hz case changes channel phase over tens of milliseconds; 7 ms multipath requires pilots/equalization or RAKE processing. Differential detection avoids absolute phase but pays noise enhancement and still needs a usable adjacent-symbol channel | Lower transmitter crest factor for single-carrier PSK than OFDM, but acquisition/channel tracking become the likely low-SNR floor; more failure modes than energy detection | Not first. Retain a fast-symbol single-carrier BPSK/CPFSK oracle only if MFSK fails the AWGN energy target. |
| DSSS with BPSK/CPFSK chips | Processing gain can trade bandwidth for low information rate; a constant-envelope chip waveform is possible | A RAKE can combine the 7 ms spread, but the 30 Hz channel changes phase during long spreading symbols; CW immunity depends on processing and front-end headroom | Code/timing search, multipath hypotheses, and coherent/noncoherent combining are substantially new to this repository | Ranked alternate 4; no clear finite-frame advantage over coded hopping MFSK yet. |
| Chirp/CSS | Constant envelope, whole-band energy, good acquisition candidates | Delay and Doppler move/smear dechirped bins; at long symbols 30 Hz spans several bins, while shorter symbols reduce processing gain. Strong CW can survive dechirping as structured interference | New chirp generation, ambiguity search, and multipath peak handling; CPU is bounded with FFTs but not yet shared | Ranked alternate 3 if MFSK suffers frequency-selective erasures that excision/FEC cannot absorb. |
| Repetition only | Predictable 3 dB per doubled duration in AWGN | Provides time diversity only if copies see different fades; literal repetition can correlate failures and is inefficient against stationary CW/notches | Simplest combiner and bounded work | Use only as a diagnostic or incremental-redundancy branch, not as the primary code. |
| Frequency diversity / hopping hybrid | No AWGN gain by itself; duplicate branches cost rate, while label hopping is free | Time-varying tone labels randomize error values; persistent-tone excision turns one/two CW interferers into about 1/16 or 2/16 erasures. Explicit duplicate branches add notch diversity | Small extension to MFSK metrics; stays one tone at a time | Label hopping and excision are in HR0-A. Dual-copy diversity is ranked alternate 2. |

The parallel geometry's high-latitude warning is a screening ratio, not proof
of failure: a detailed receiver could use wider spacing, ICI equalization, or
joint detection. Those changes would no longer be the literal 23 Hz geometry
being used as the reference.

## Frozen first-pass candidate: HR0-A

### Signaling geometry

| Parameter | First-pass value |
| --- | ---: |
| Audio sample rate | 48,000 sample/s |
| Intended receive-analysis rate | 6,000 sample/s after existing decimation |
| Signaling | one-of-16 noncoherent MFSK, one real tone at a time |
| Tone centers | 375 through 2,250 Hz in 125 Hz steps |
| Nominal tone-bank width | 2,000 Hz (`16 * 125 Hz`); 99%-power bandwidth must be measured and must not exceed 2,300 Hz |
| Micro-observation | 8 ms; 384 TX samples / 48 RX samples; 125 Hz orthogonal-bin spacing |
| Tone dwell / coded symbol | 24 ms; three consecutive micro-observations on the same tone |
| Symbol rate | 41.6666667 symbols/s |
| Coded bits/symbol | 4, Gray labeled after the time-varying label mask |
| Raw coded bit rate | 166.6666667 bit/s |
| Maximum-delay receiver | treat the first 8 ms as guard; noncoherently combine the final two observations |
| Clean-AWGN oracle | report both all-three-observation combining and the frozen last-two receiver, so guard loss is visible rather than hidden |
| Envelope | single real sinusoid, continuous phase across the three micro-observations; target audio RMS equal to HC0 |
| Expected ideal-audio crest factor | sqrt(2), or 3.01 dB peak/RMS; measure after all ramps/filters |

The 8 ms guard exceeds the registry's 7 ms maximum differential delay by
1 ms. It is not silence: the new tone starts immediately, but the receiver
does not trust the interval in which the delayed path can still carry the
previous tone. Keeping it energized preserves a single-tone envelope and
avoids a 33% on/off duty cycle with a worse spectral skirt. The whole keyed
interval still counts in waveform SNR and energy/bit, so discarded receive
energy is honestly charged to HR0-A.

The outer centers leave approximately 375 Hz below and 150 Hz above the last
tone inside a 0--2,400 Hz view. Hard tone changes have sidelobes, so nominal
tone-bank width is not an occupied-bandwidth result. Failure of the measured
99%-power 2,300 Hz gate requires pulse shaping or inward movement of the edge
tones before channel screening.

### Checked payload and FEC

The first oracle uses a terminated, non-systematic, rate-1/3 K=7
convolutional code with octal generators `(171, 133, 165)`, soft Viterbi, and
64 states. This needs an experiment-local generalization of the current
two-output class. It retains the repository's 2-byte length, CRC32, PN
whitening, and checked-payload semantics.

Each class appends the usual six zero tail inputs and then two more zero
inputs while already in state zero. The resulting eight-bit termination/pad
makes every three-output codeword divide exactly into four-bit MFSK labels.
Padding is part of the frozen codeword and never counted as payload.

| Class | Maximum physical payload | Intended workload | FEC input | Coded bits | Data symbols | Pilots | Body symbols |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| tiny | 12 B | 10-byte header plus DATA_ACK remainder; empty controls fit | 152 | 456 | 114 | 3 | 117 |
| short | 26 B | 10-byte header plus 16-byte short DATA; 12-byte control body also fits | 264 | 792 | 198 | 6 | 204 |
| full | 64 B | 10-byte header plus 54-byte DATA body | 568 | 1,704 | 426 | 13 | 439 |

Use existing multiplicative interleaving with class-specific first-pass
strides `227`, `395`, and `851` respectively. Each is coprime with its coded
length and places adjacent coded bits nearly half a codeword apart. The oracle
must still report per-tone and per-time error bursts; a valid permutation is
not evidence that it is the best interleaver.

Insert one known pilot tone after every 31 data symbols, omitting a terminal
pilot. Pilot tones cycle over the full bank. They support clock/CFO diagnostics
and a distributed acquisition fallback; they are not payload or FEC bits.

Before Gray tone mapping, XOR each four-bit transmitted label with successive
nibbles from the repository order-17 PN generator, seed `0x0A6D1` XOR the
class number (`0`, `1`, `2`). This does not duplicate energy. It ensures that
a persistent wrong winning tone does not create one repeated Viterbi error
pattern and that transmitted tones remain spread across the band.

### Lead, acquisition, and class indication

- Retain 128 ms as the rate-budget lead floor. The current common-HF lead can
  eventually advertise HR0, but it stays advisory and no production label or
  mode ID is assigned in point 2.
- Follow it with 80 guarded tone symbols (1.920 seconds). The three frame
  classes use distinct balanced class words. Construct each word as five
  consecutive permutations of all 16 tones. For permutation block `j`, sort
  tone numbers by a 17-bit key drawn from `dsp.bits.pn_bits`, tie-breaking by
  tone number, with seeds `0x15201`, `0x15202`, and `0x15203` for tiny, short,
  and full. This freezes a reproducible first codebook while making every
  16-symbol span frequency balanced. Point 3 may replace it only as a new
  candidate revision selected on training seeds.
- The preamble transmits 1.920 seconds and contributes 1.280 seconds of
  trusted post-guard observations. The trusted observation time is exactly
  five times HC0's 256 ms sync observation time. The
  analytical 6.99 dB energy increase makes `-24 dB` acquisition plausible
  if HC0 acquisition remains usable near `-17 dB`; it does not demonstrate
  it under fading.
- Initial real acquisition in point 3 searches CFO hypotheses
  `k * 15.625 Hz` for integer `k` from `-13` through `+13` (a symmetric
  +/-203.125 Hz screen), coarse timing every 2 ms, then refines timing to
  0.25 ms. Repeated 8 ms observations inside each tone dwell provide a fine
  CFO estimate unambiguous over +/-62.5 Hz after coarse correction.
- Rank at most 16 distinct `(boundary, CFO, class)` body candidates after
  deduplicating adjacent timing/CFO cells. CRC32 and the terminated state
  validate a payload; neither the common lead nor a class-word score does.
  Point 3 must freeze score thresholds on training seeds and demonstrate the
  bounded work on held-out and signal-absent inputs.
- If the leading class word falls in a slow fade, a bounded fallback may use
  the known pilot lattice distributed across the body. The pilot fallback is
  part of the acquisition problem, not permission to try unbounded starts or
  call a CRC search “synchronization.”

Static CFO beyond one half-tone spacing is why the receiver needs a coarse
bank rather than HC0's fine repeated-tone estimate alone. At 100 ppm, a
12.6-second full frame drifts by about 1.26 ms, below one 8 ms observation;
the pilot lattice must show whether non-data-aided refinement is still needed.

### Airtime and energy budget

The keyed time includes the 128 ms lead floor, 1.920-second class word, body,
and 20 ms tail. It does not hide fixed padding as useful data.

| Class | Keyed time | Useful workload rate |
| --- | ---: | ---: |
| tiny | 4.876 s | workload-dependent; 12 physical bytes maximum |
| short | 6.964 s | 18.38 bit/s for the 16-byte DATA body; this workload is a latency probe, not the long-transfer target |
| full | 12.604 s | 34.275 bit/s for the 54-byte DATA body |

A clean stop-and-wait full-DATA/tiny-ACK exchange, including two 0.3-second HF
turnarounds, is `12.604 + 4.876 + 0.600 = 18.080` seconds, or 23.894 useful
bit/s. The 5.894 bit/s surplus is the entire budget for retransmissions,
timeouts, extra calibrated lead, and other session behavior. Eighteen bit/s
is therefore not established by this table; point 6 must measure it, and the
oracle should reject any revision whose predicted ARQ behavior already spends
the surplus.

Using `PLAN.md`'s 24 kHz noise bandwidth:

- full-frame useful-bit `Eb/N0 = waveform SNR + 28.452 dB`, so `-24 dB`
  waveform SNR is `+4.452 dB` useful-bit Eb/N0;
- clean-session useful-bit `Eb/N0 = waveform SNR + 30.019 dB`, so `-20 dB`
  waveform SNR is `+10.019 dB` on that no-retry session budget; and
- the corresponding `-24 dB` waveform SNR is `-14.97 dB` in a stated 3 kHz
  white-noise bandwidth.

These are conversions, not decoder limits. A single-tone waveform avoids the
parallel-carrier backoff risk: at the same allowed audio peak, its ideal
average power is roughly the PAPR difference above a multitone candidate.
Actual clipping and occupied bandwidth remain mandatory measurements.

## Exact Watterson exposure

The ratios below use the repository's 2-sigma spread convention. “Delay/dwell”
is differential delay divided by 24 ms. “Spread/spacing” is 2-sigma spread
divided by 125 Hz.

| Preset | Delay | Spread (2 sigma) | Delay/dwell | Spread/spacing |
| --- | ---: | ---: | ---: | ---: |
| low_latitude_quiet | 0.5 ms | 0.5 Hz | 0.021 | 0.004 |
| low_latitude_moderate | 2 ms | 1.5 Hz | 0.083 | 0.012 |
| low_latitude_disturbed | 6 ms | 10 Hz | 0.250 | 0.080 |
| mid_latitude_quiet | 0.5 ms | 0.1 Hz | 0.021 | 0.0008 |
| mid_latitude_moderate | 1 ms | 0.5 Hz | 0.042 | 0.004 |
| mid_latitude_disturbed | 2 ms | 1 Hz | 0.083 | 0.008 |
| mid_latitude_disturbed_nvis | 7 ms | 1 Hz | 0.292 | 0.008 |
| high_latitude_quiet | 1 ms | 0.5 Hz | 0.042 | 0.004 |
| high_latitude_moderate | 3 ms | 10 Hz | 0.125 | 0.080 |
| high_latitude_disturbed | 7 ms | 30 Hz | 0.292 | 0.240 |

At the high-latitude extreme, the spread's Gaussian sigma is 15 Hz. The
ideal Gaussian-scatter autocorrelation magnitude over one 8 ms observation is
approximately `exp(-2*pi^2*15^2*0.008^2) = 0.75`; over an undivided 24 ms
dwell it is only about 0.077. That is the reason for noncoherently combining
short observations instead of coherently integrating the whole dwell. This
calculation is an analytical warning, not a substitute for the repository
channel.

The full codeword spans 10.536 seconds of body and 12.604 seconds keyed. That
is about 1.26 times `1/spread` for the slowest 0.1 Hz preset, but it is only a
small part of the 300-second continuous-fade dwell required by `PLAN.md`.
Whole-frame interleaving cannot claim immunity to a many-second fade; session
ARQ must carry the remaining outage duration.

## Erasures, CW, notches, and impulses

The receiver should estimate a robust per-tone noise/interference floor from
the 15 nominally unused tones in every observation and its history. A tone
that remains abnormally energized is clipped or marked unreliable before
bit-max metrics are formed. With balanced hopping, excising one fixed tone
turns its collisions into approximately 1/16 symbol erasures; two fixed tones
produce approximately 2/16. A 300 Hz notch can remove roughly two or three
adjacent centers and becomes a distributed erasure pattern after
interleaving.

This mechanism is falsifiable. It fails if front-end clipping from a strong CW
raises the whole-band floor, if the detector wrongly excises a legitimate
faded tone, or if the convolutional code cannot absorb the resulting erasure
density. Those cases pivot to explicit dual-frequency time diversity or a
stronger erasure-aware outer code; they are not fixed by retuning the SNR
label.

Impulses and time blanking are spread by the class-wide interleaver, but a
contiguous outage comparable to the codeword remains an ARQ event. The tiny
class limits ACK latency; it must not be made as long as the full DATA frame
merely to improve its isolated FER.

## Ranked alternates and reasons to pivot

1. **HC0-grid 16-MFSK plus stronger coding/incremental redundancy.** It reuses
   almost the entire receiver and has no new occupied-bandwidth question.
   Pivot here if HR0-A loses at least 2 dB to its guard/short-observation
   structure without gaining at least 3 dB over HC0 in disturbed fading.
   Its known red flag is the 7 ms delay occupying 66% of an HC0 symbol.
2. **HR0-A with a second time/frequency-diverse copy or incremental parity.**
   Keep one tone at a time and place the extra branch in a disjoint tone/time
   permutation. Pivot if CW/notch/selective-fade failures dominate while the
   measured clean session rate has enough surplus. Reject if it takes the
   long-session rate below 18 bit/s.
3. **Short-block rate-1/3 or rate-1/4 LDPC/polar outer code on the HR0-A
   geometry.** Pivot if oracle symbol metrics are healthy but the frozen
   `(171,133,165)` code misses the AWGN target by more than 1 dB or shows an
   error floor. It ranks below the convolutional baseline only because no
   such decoder/building block exists in the repository and list/iteration
   bounds become new qualification work.
4. **Constant-envelope CSS/chirp with FFT dechirping and multipath peak
   combining.** Pivot if sequential tone erasures, rather than FEC, are the
   measured limit across many Watterson families. Require an oracle win of at
   least 2 dB to justify the entirely new acquisition path.
5. **Fast-symbol single-carrier BPSK/CPFSK with low-rate code and equalizer or
   noncoherent block combining.** Pivot only if it demonstrates the same
   7 ms/30 Hz envelope and at least a 2 dB oracle energy advantage. Do not use
   a clean-AWGN-only win to accept its phase/channel-estimation risk.
6. **Wider-spaced parallel BFSK.** Reconsider only with measured crest-factor
   backoff and an ICI-aware high-latitude result. Literal 23 Hz spacing is not
   promoted past the analytical screen while the channel spread is 30 Hz.

## Falsifiable point-2 handoff

**Go** to point 3's oracle and bounded experimental receiver work for HR0-A,
with these stops:

- **Redesign the code** if the oracle-aligned full frame does not meet the
  `-24 dB` AWGN target but its uncoded tone metrics support a stronger decoder
  at the same airtime.
- **Redesign the geometry** if `high_latitude_disturbed` or disturbed NVIS
  loses at least 3 dB relative to disturbed mid-latitude because of measured
  delay/Doppler contamination, or if measured 99%-power bandwidth exceeds
  2,300 Hz after reasonable edge movement/shaping.
- **Redesign framing/ARQ** if real acquisition plus tiny ACKs cannot project
  at least 18 bit/s at the target FER. Do not call the 34.275 bit/s full-frame
  number a session result.
- **Stop HR0-A** if a bounded real receiver cannot keep acquisition at least
  as reliable as checked payload delivery, or if it cannot beat the newly
  measured HC0 disturbed boundary by 3 dB without dropping below 18 bit/s.
- **Stop the current family search** if no constant-envelope orthogonal or
  spread candidate can meet both the physical robustness and useful-session
  rate targets under the frozen whole-keying SNR definition. Report that
  limit instead of weakening the target after observing it.

Exact point-2 files and checks:

```sh
python experiments/hr0/architecture_screen.py
python -m py_compile experiments/hr0/architecture_screen.py
git diff --check -- experiments/hr0/DESIGN.md \
  experiments/hr0/architecture_screen.py
```

`architecture_screen.py` is arithmetic only. It reproduces frame sizes,
airtimes, delay/Doppler ratios, and Eb/N0 conversions; it intentionally cannot
emit FER, acquisition probability, or a robustness claim.
