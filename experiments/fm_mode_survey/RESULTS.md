# Every declared FM mode over two analog FM paths

## Question

Ten FM waveforms exist in the tree -- three CPFSK profiles, four registered
OFDM rungs, and three unregistered experiments. Which of them actually deliver
frames over a real analog FM link?

Answer: it depends on the handheld, and more sharply than expected. The same
IC-705, the same harnesses and the same afternoon produce two different
answers when the handheld at the other end is swapped. The ladder's *order*
holds on both -- no mode passes where a faster one also passes -- but where it
breaks moves by more than a factor of two in rate.

Neither path carries `vf4`, `vf6` or `vf10` at all.

## Bench

| | Station A | Station B, run 1 | Station B, run 2 |
| --- | --- | --- | --- |
| Radio | Icom IC-705, FM | **Baofeng UV-B5** | **Wouxun KG-UV9D Plus** |
| Audio | IC-705 USB Audio CODEC | digirig USB Audio Device | digirig USB Audio Device |
| PTT | CI-V, address 164 | serial RTS, COM5 | serial RTS, COM5 |

Station A and the audio/PTT wiring are identical across both runs; only the
handheld changed. Frequency, power and deviation were not recorded.

**The handhelds' volume knobs are never at exactly the same setting.** They are
analog controls that get turned between sessions and cannot be returned to a
known position, so receive audio level is not a controlled variable here and is
not comparable between the two runs, or between two runs on the same radio.
This is not a footnote: on this bench it decided whether a shipped mode worked
at all. See *Receive audio level*.

Every measurement is physical-layer only: one `encode` -> transmit -> capture
-> `decode` per trial, byte-compared against the transmitted payload. No Link,
no ARQ, no retries, no sockets. A trial that fails its CRC is a failure.

Both directions always, and the worse one is the answer.

## Method

Three harnesses, because the modes are not all reachable the same way:

| Harness | Modes | Trials per direction | Payload |
| --- | --- | ---: | --- |
| `scripts/sweep_modes.py --channel fm --mode-level experimental` | 300baud, 600baud, 1200baud, vf3, vf12, vf4, vf6 | 10 | `AIR_HEADER_BYTES + chunk_size` |
| `scripts/hw_vf9_frames.py --mode both` | vf9, vf10 | 10 | `chunk_size` |
| `scripts/hw_vf11_bitload.py --margins 2,0,-2` | vf11 | 5 | `chunk_size` |

`vf9`, `vf10` and `vf11` are absent from `whale/mode_qualification.py`'s
manifest, so `sweep_modes.py` cannot reach them; they are imported directly by
their own scripts. `vf11` additionally needs the path sounded before it has a
mode at all -- its per-carrier bit map is a parameter both ends are handed, not
something the receiver recovers.

The first two harnesses abandon a direction after 3 consecutive failures, so a
mode that is not going to clear the bar costs three keyings rather than ten.
Counts below are therefore out of what was actually run: `0/3` means the
direction was stopped, not that it was only asked for three.

The two payload definitions differ by the 10-byte air header. The sweep sends
the full link packet, which is 10 B more than the `chunk_size` figure
`docs/MODES.md` publishes its rates against; the net bit/s column below uses
the documented `chunk_size` definition so it stays comparable to that table.

Pass is >= 80% exact-payload frames in **both** directions.

Raw output and saved failure captures are in
`logs/mode_qualification/fm/2026-09-13-uvb5/` and
`logs/mode_qualification/fm/2026-09-13-kguv9d-level-fixed/`. The superseded
clipped run is kept alongside as `2026-09-13-kguv9d/`.

## Result

Measured 2026-09-13 at commit `f26d3b0` (working tree dirty). A is the IC-705,
B is the handheld.

| Mode | ID | Level | Frequency span | Geometry | Order | FEC | Net bit/s | UV-B5 A->B | UV-B5 B->A | UV-B5 | KG-UV9D A->B | KG-UV9D B->A | KG-UV9D |
| --- | ---: | --- | --- | --- | ---: | --- | ---: | ---: | ---: | --- | ---: | ---: | --- |
| 300baud | 0 | default | 1,200-1,800 Hz | 2-tone CPFSK | 2 | none | 189 | 10/10 | 10/10 | pass | 10/10 | 10/10 | pass |
| 600baud | 1 | default | 1,200-1,800 Hz | 2-tone CPFSK | 2 | none | 399 | 10/10 | 10/10 | pass | 10/10 | 10/10 | pass |
| 1200baud | 2 | default | 1,200-2,200 Hz | 2-tone CPFSK | 2 | none | 818 | 10/10 | 0/10 | fail | 10/10 | 9/10 | pass |
| vf3 | 3 | default | 468.75-3,140.625 Hz | 58-carrier differential-QPSK OFDM | 4 | 1/2 K=7 convolutional | 1,853 | 10/10 | 1/10 | fail | 10/10 | 9/10 | pass |
| vf9 | 9 | unregistered | 500-2,900 Hz | 49-carrier QPSK OFDM | 4 | 3/4 LDPC | 2,751 | 8/10 | 5/10 | fail | 10/10 | 6/10 | fail |
| vf11 | 12 | unregistered | 500-2,900 Hz | 49-carrier bit-loaded OFDM | 0-4 bits/carrier | 3/4 LDPC | map-dependent | 5/5 | 5/5 | pass, sounded | 4/5 | 4/5 | pass, sounded |
| vf10 | 11 | unregistered | 500-2,900 Hz | 49-carrier 8PSK OFDM | 8 | 3/4 LDPC | 4,131 | 4/10 | 3/10 | fail | 7/10 | 0/10 | fail |
| vf12 | 18 | default | 500-3,000 Hz | 51-carrier 16-QAM OFDM, comb pilots | 16 | 3/4 LDPC | 4,690 | 10/10 | 9/10 | pass | 10/10 | 0/10 | fail |
| vf4 | 8 | experimental | 468.75-3,140.625 Hz | 58-carrier 16-QAM OFDM | 16 | RS(254,238) | 6,475 | 0/10 | 0/10 | fail | 0/10 | 0/10 | fail |
| vf6 | 6 | experimental | 468.75-3,140.625 Hz | 58-carrier 256-QAM OFDM | 256 | RS(254,238) | 13,281 | 0/10 | 0/10 | fail | 0/10 | 0/10 | fail |

Rows are in rate order. `vf11`'s counts are its best-scoring map per direction.

Fastest mode that passes:

| Handheld | Fastest passing | Rate |
| --- | --- | ---: |
| Baofeng UV-B5 | `vf12` | 4,690 bit/s |
| Wouxun KG-UV9D Plus | `vf11` at margin -2, sounded | 2,261 bit/s |
| Wouxun KG-UV9D Plus, registered modes only | `vf3` | 1,853 bit/s |

### vf11 by margin

Sounded per-carrier SNR (min/median/max) and delivery at each threshold shift.
Negative margin is more aggressive.

| Handheld | Direction | Sounded SNR | +2 dB | 0 dB | -2 dB |
| --- | --- | --- | --- | --- | --- |
| UV-B5 | A->B | 8.7/10.9/13.2 dB | 5/5 at 1,841 | 5/5 at 2,429 | 5/5 at 2,597 |
| UV-B5 | B->A | 7.9/10.8/13.1 dB | 5/5 at 1,673 | 4/5 at 2,429 | 5/5 at 2,765 |
| KG-UV9D | A->B | 11.2/13.0/14.7 dB | 5/5 at 2,513 | 5/5 at 2,682 | 4/5 at 3,691 |
| KG-UV9D | B->A | 4.1/8.6/10.3 dB | 5/5 at 747 | 5/5 at 1,337 | 4/5 at 2,261 |

Rates are bit/s. The Wouxun's two legs sound 4 dB apart, and bit loading tracks
that: on B->A at +2 dB the planner switches carriers off entirely, where on
A->B at the same margin it keeps all 49.

## Why each failure fails

The captures were re-decoded offline with each mode's own diagnostics.

**The two paths fail in different ways.** The UV-B5 path is *asymmetric*:
audio it transmits arrives at the IC-705 5-6 dB worse than the reverse, which
is what breaks `1200baud` and `vf3` in one direction only. The Wouxun path is
asymmetric the other way and more steeply -- 13.0 dB median sounded on A->B
against 8.6 dB on B->A. So the QPSK modes the UV-B5 was breaking now pass,
while `vf12` and `vf10`, which need the margin, fail on the Wouxun's weak leg.

**1200baud on the UV-B5** -- acquisition is perfect (zero bit errors in the
255-bit sync word, confidence 0.91-0.92). The 2,200 Hz space tone arrives
**6-10 dB below** the 1,200 Hz mark tone on B->A, a repeatable rolloff rather
than noise. The resulting 0.17-0.60% raw BER is small, but the mode has no FEC
at all, so a 402 B frame fails its CRC every time. `300baud` and `600baud`
survive because both their tones sit below 2 kHz. On the Wouxun this rolloff
is absent and the mode recovers to 9/10.

**vf3 on the UV-B5** -- not a band-edge problem. Its six carriers above
2,900 Hz were the *cleanest* in the frame; only one of 58 carriers
(468.75 Hz) sits outside the 500-2,900 Hz window measured usable in
`whale/modes/vf9.py`, and it is the worst one. The cause is plain SNR: 7.8 dB
mean per-carrier against a rate-1/2 convolutional code working from a single
header-fit channel estimate, giving 2.5-5.8% raw BER. On the Wouxun it is
19/20.

**vf12 on the Wouxun** -- it acquires perfectly every time. Failures report
`synced=True` at confidence 0.935-0.968 with the frame start in the same
sample window as on the UV-B5, so this is not an acquisition, lead-in or
timing failure. It is corrupt payload: 0/48 codewords on the weak leg, where
median effective SNR falls to 10.8-13.4 dB from the ~15 dB the mode is
calibrated at. The UV-B5's single failure sat at 15.3 dB with 47/48 codewords
-- one bit from passing. **vf12 has almost no margin at its operating point**,
which is why it is the mode that moves most between handhelds.

**vf4, vf6 and vf10** -- a constellation-order failure on both paths, not a
band or acquisition one. All 58 carriers were present in every `vf4`/`vf6`
capture; every failure reports `RS decode failed`. RS(254,238) is
hard-decision and corrects at most ~3.1% symbol errors with no interleaving.
`vf4` sits at 2.7-3.1% raw BER on the UV-B5's good direction and 18-30% on the
bad one; `vf6`'s 256-QAM yields ~20% BER even at 16 dB.

**vf9** -- failures are all-or-nothing (0/30 or 18-26/30 codewords) despite
healthy *median* SNR, because the weakest carriers sink whole codewords. This
is exactly the loss `vf11` was built to remove, and on both paths it does:
`vf11` passes both directions where uniform QPSK at a comparable rate does not.

### Receive audio level

The Wouxun's first run was taken with its volume knob below half, and that was
already far too hot: a plain 1,500 Hz tone came back at **peak 1.317, RMS
0.679**, against 0.572 peak on the IC-705 leg. Roughly 1.1% of samples were
over unity. With the knob near minimum the same tone returns peak 0.116, RMS
0.066 in steady state, a 4.9 dB crest factor on a sine.

That one knob position changed the answers:

| Mode / direction | Clipped | Corrected |
| --- | ---: | ---: |
| vf12 A->B | 0/10 | **10/10** |
| vf10 A->B | 0/10 | 7/10 |
| vf9 A->B per-carrier SNR | 12.2 dB | **17.2 dB** |
| vf3 B->A | 10/10 | 9/10 |

Every figure in the tables above is from the corrected run. It also broke two
measurement tools outright -- see below. The lesson is not "turn it down" but
that **receive level has to be checked before a session's numbers mean
anything**, because nothing in the mode results themselves reveals it: vf12
failed at 0/20 with perfect acquisition and plausible SNR both times.

### Bench characterisation refreshed

The measured radio presets in `whale/fm_channel.py` were re-measured against
this handheld at the corrected level, with
`scripts/measure_fm_audio_band.py` and `scripts/measure_clock_offset.py`:

| | `ic705_to_kg_uv9d` | `kg_uv9d_to_ic705` |
| --- | --- | --- |
| -6 dB band | 445.0-1,929.7 Hz | 487.8-1,631.7 Hz |
| -10 dB band | 399.2-2,490.0 Hz | 436.4-2,228.2 Hz |
| Sample clock error | -5.2 ppm | +1.2 ppm |
| Delay spread | 0.446 ms | 0.717 ms |

`leading_mute_seconds` is the one field left untouched: what a capture shows at
its start is the PTT lead and the squelch blackout summed, and nothing here
separates them.

## What this changes

- Mode results are a property of the path *and the levels*, not of the mode
  alone. `vf12` and `vf3` swap places on a handheld change, and `vf12` swaps
  again on a volume knob. Any single-radio result in `docs/MODES.md` should be
  read that way.
- `vf12` is the fastest mode measured working on any FM path here, but it is
  also the most fragile, and it does not hold both directions on the Wouxun.
  It is the shipped DEFAULT top rung.
- `vf3` and `1200baud` are DEFAULT rungs that deliver nothing B->A on the
  UV-B5 path. A link that climbs to either there stalls until it falls back.
- Bit loading earns its keep on both paths and degrades gracefully where fixed
  constellations fall off a cliff: on the Wouxun's weak leg, where `vf10` and
  `vf12` deliver nothing, `vf11` still passes at 2,261 bit/s. It is not yet a
  self-contained mode -- the receiver cannot learn the map -- so this is a
  measurement, not an available rung.
- `vf4` and `vf6` have now failed 0/40 across two paths and two level settings.
  Nothing here suggests either is worth further bench time without a change to
  their FEC.
