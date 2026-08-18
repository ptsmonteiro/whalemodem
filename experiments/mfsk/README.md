# MFSK frame mode — evaluation

An M-ary FSK frame mode for the IC-705 / HT bench, sized to the same 3.0s
keying cap the shipped profiles use, evaluated for maximum payload throughput
subject to decoding 100% in **both** directions.

Standalone by design. Nothing in `whale/` imports any of this, and nothing here
writes back into it — `mfsk.py` imports `whale.framing` for CRC16 and the
bit/byte helpers (pure functions), and the sweep imports `whale.transport` to
reach the radios. Integration (a `codec_id` on `Profile`, mode negotiation,
chunk sizing in `link.py`) is deliberately not attempted.

```
mfsk.py         the modem: MfskProfile, modulate/demodulate, candidate generation
test_mfsk.py    software invariants — no hardware
screen_mfsk.py  AWGN pre-screen, to avoid spending airtime on hopeless candidates
sweep_mfsk.py   the bench sweep over the real radios — this is what decides
```

## The headline, first

**Orthogonal MFSK cannot beat the shipped 1200-baud binary profile on this
band.** That is arithmetic, not a measurement, and it is worth stating before
anything else because it is the opposite of the intuition that more tones means
more throughput.

Orthogonal MFSK needs one symbol rate of tone spacing, so M tones span
`(M-1)·Rs` and the efficiency is `log2(M)/(M-1)` bits/s per Hz of span: 1.00 at
M=2, 0.67 at M=4, 0.43 at M=8. **More tones is strictly worse per Hz.** Fold in
the measured 600–2300 Hz band and the one-cycle rule (below) and the best
orthogonal placements are:

| M | symbol rate | raw | payload bits/s |
|---|-------------|-----|----------------|
| 2 | 1150 baud   | 1150 | 909 |
| 4 |  550 baud   | 1100 | 848 |
| 8 |  225 baud   |  675 | 477 |

against `PROFILE_1200`'s **947**. None of them reaches parity — and the reason
the shipped profile wins is that it is *itself* sub-orthogonal, running 1000 Hz
of separation at 1200 baud, a ratio of 0.833.

So every candidate above 947 bits/s gets there by packing tones closer than the
detector can cleanly separate, exactly as the shipped profile already does.
That is a bet on this specific hardware rather than a property of the
modulation, and the bench is the only thing that can settle it.
`test_no_orthogonal_candidate_can_beat_the_shipped_profile` keeps the arithmetic
on file so it does not get rediscovered the expensive way.

## Why MFSK is still worth having here

The bench is **not bandwidth-limited — it is symbol-rate-limited**, and the repo
already contains the evidence. `scripts/sweep_baud_600_2300.py` cleared 1200
baud at 1200/2200 Hz and failed 1400 baud **at the same two tones**. Bandwidth
did not change between those two points; only symbol duration did. Whatever
kills 1400 baud — group delay across the FM audio chain, ISI, the one-symbol
integration in the detector — is a function of symbol rate.

That is exactly the trade MFSK makes. It buys bits per symbol with bandwidth,
which this channel has spare, and spends nothing on symbol rate, which it does
not. Orthogonal 4-FSK delivers 90% of the shipped profile's throughput at
**48% of its symbol rate** — a large margin against the one thing known to
break, at a small throughput cost. It also carries roughly half the symbols per
frame, which doubles the sample-clock offset a frame tolerates (the decoder lays
symbol points on a rigid grid from the sync peak, budget `0.5/n_symbols`).

## Constraints candidates are generated under

All four come from measurements already in the repo; see `mfsk.candidates()`.

| constraint | value | source |
|---|---|---|
| band | 600–2300 Hz | `scripts/measure_band_edges.py`, worst direction |
| max symbol rate | 1200 baud | `scripts/sweep_baud_600_2300.py` — 1200 clears, 1400 fails |
| lowest tone | ≥ 1 cycle per symbol | `PROFILE_1200`'s failed re-centring attempts |
| spacing | ratio to symbol rate | 1.0 is orthogonal; `PROFILE_1200` proves 0.833 survives at M=2 |

The one-cycle rule deserves a note, because two readings of `PROFILE_1200`'s
evidence were available and they make different predictions. The README there
says performance fell off monotonically as the tone pair moved *down* from 1700
Hz, which reads as absolute placement; but `measure_band_edges.py` decoded fine
with a 600 Hz tone at 600 baud, which is a low absolute frequency working
perfectly well. Both observations fit "at least one cycle of the lowest tone per
symbol" and only one fits "tones must be high", so the cycle rule is the
better-supported model and is what `place()` enforces.

## What changed from `whale/afsk.py`, beyond M

Three things, all forced by M>2 rather than chosen:

- **The detector is a bank of M filters with an argmax**, not the sign of a
  difference.
- **The preamble doubles as training.** It is a PN sequence over all M tones, so
  after sync the receiver knows which tone was sent in every preamble slot and
  can divide out the gain each actually came back at. Costs no airtime.
- **Sync correlation is multi-channel** — M envelopes against M template
  channels, summed in the numerator, normalised by total energy across all M.

### One bug worth recording

The multi-channel correlation was wrong in its first form, and the failure is
instructive. Tone envelopes are magnitudes, so every channel is non-negative and
carries a large DC component; correlating raw envelopes against a raw template
scores the DC against the DC, and the score never falls to where it should.
**Pure Gaussian noise scored 0.73 against the 0.70 lock threshold** — the
detector would have false-synced on silence, which is precisely the failure
`afsk._normalised_correlation` was rewritten to eliminate.

The fix is `mfsk._centre`: subtract the across-tone mean at each instant so the M
channels sum to zero. This is also what makes it the honest generalisation of the
binary case — `whale/afsk.py` correlates the signed difference `e1 - e0`, which
has no DC by construction, and at M=2 the centred channels here are `±(e1-e0)/2`,
so the score reduces to exactly afsk's and `CONFIDENCE_THRESHOLD` keeps the
meaning it was calibrated with.

### One piece of machinery that did *not* earn its place

The preamble training equaliser makes **no measurable difference in software**.
Tested at gain slopes of 0/6/12/18 dB across the band, at spacing ratios from
1.0 down to 0.6, with the frame alone in the buffer and with four seconds of
noise around it, trained and untrained decode identically — right up to the
point where both fail together.

Two reasons: `tone_envelopes()` already divides each tone by its own RMS over the
buffer, so the training refines something already present rather than adding a
capability; and the decision margin at these spacings is far wider than the
tilt, since the wrong tones' filters see mostly noise.

It is kept, switchable and defaulted on, only because the software model has no
group delay in it, and group delay travelling with a real frequency response is
the half of the channel that has broken every previous tone placement on this
bench. `sweep_mfsk.py --no-training` A/Bs it on air. If it cannot be shown to
matter there either, it should come out.

## Why the software screen is relative, not absolute

The obvious screen — run every candidate at the 7.5 dB `measure_snr.py` reported
for the weak leg — gives the wrong answer.

Run `PROFILE_1200` itself through the AWGN model at 7.5 dB and it decodes **0 of
10** frames. On the actual radios, at the SNR that script measured, it decodes
100%. The two 7.5 dBs are not the same quantity, and the model is the honest
one: `measure_snr.py` estimates in-band noise from side bands just outside the
tone span and scales it in. Under FM that is pessimistic by a wide margin — a
captured carrier quiets the noise *inside* the occupied band far more than
beside it, which is the whole reason FM is usable at these levels. The side
bands measure unquieted noise; the tone band is quieted.

So `screen_mfsk.py` screens on a ratio instead. It re-measures what
`PROFILE_1200` needs on the same yardstick every run (14 dB at 20 trials) and
requires a candidate to need no more than that. A mode already in service at
100% defines a link margin the bench demonstrably has.

## Results

Full detail in [RESULTS.md](RESULTS.md). In short:

**`4fsk_650bd_x0.833`** — 663/1204/1746/2287 Hz, 650 baud, 379-byte payload,
2.99s keying, **1010.7 payload bits/s = 1.07× the shipped profile**. Verified
**45/45 frames each direction** across two independent sessions, random payloads,
checked byte-for-byte, no ARQ. A symbol-level post-mortem on four further frames
found **0 errors in 6128 symbols**, so the margin is real rather than knife-edge.

The binding constraint turned out to be **tone separation, not symbol rate**. The
nearest miss failed with 8 wrong symbols in 1596, *all* of them the lowest tone
read as its neighbour, spread evenly through the frame — leakage, not drift, not
noise. The winner sits exactly on the constraint corner: 650 baud at 0.833 is the
widest spacing available at the highest symbol rate that fits the band.

Two things worth knowing beyond the headline:

- **The AWGN screen was a poor predictor.** 19 of 24 candidates cleared its bar;
  one worked on air. Noise is not the binding impairment here, so use the screen
  for ordering, not for skipping bench trials.
- **The orthogonal mode is the more interesting one.** `4fsk_550bd_x1` decoded
  4/4 both directions first try at 848 bits/s (0.90×) — with *genuinely
  orthogonal* spacing, no leakage bet at all, at 46% of `PROFILE_1200`'s symbol
  rate and half the symbols per frame. On a link whose measured wall is on symbol
  rate, that is a much larger robustness margin for a 10% throughput cost. "Max
  throughput" does not select for it.

## Running

```
python experiments/mfsk/test_mfsk.py                       # software invariants
python experiments/mfsk/screen_mfsk.py --trials 20         # AWGN shortlist
python experiments/mfsk/sweep_mfsk.py --top 12 --trials 5  # the bench, decides
python experiments/mfsk/sweep_mfsk.py --confirm 4fsk_550bd_x1 --confirm-trials 20
```

The sweep needs both radios connected, on the same frequency, squelched — the
same setup every script in `scripts/` assumes.
