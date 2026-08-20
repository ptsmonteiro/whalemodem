# qpsk29 — a 29-carrier QPSK OFDM frame

A from-scratch implementation of a handed-down frame specification. It shares
no DSP with `experiments/ofdm/` and does not obey the shipped modem's 3.0 s
keying cap: one frame keys the transmitter for 5.200 s.

| parameter | value |
|---|---|
| Frame duration | 5.200 s (45 ms lead-in + 214 symbols + 19 ms tail) |
| Symbol period | 1152 samples @ 48 kHz = 24.000 ms, 41.667 symbol/s |
| Guard interval | 128 samples = 2.667 ms |
| Modulation interval | 512 samples = 10.667 ms, transmitted twice per symbol |
| Carrier spacing | 93.750 Hz (= 1/10.667 ms, minimum orthogonal spacing) |
| Carriers | 29, at 468.75 Hz … 3093.75 Hz inclusive |
| Occupied bandwidth | 2718.75 Hz |
| Constellation | QPSK, constant modulus, on every carrier |
| Header | symbols 0–14 (360 ms), unscrambled coherent QPSK |
| Payload | symbols 15–213 (199 symbols) |
| Gross rate | 29 × 41.667 × 2 = 2416.7 bit/s |
| Payload per frame | 199 × 29 × 2 = 11 542 bit, i.e. 2217 bit/s over the keying |

## The symbol

```
[ 128 guard ][ 512 core ][ 512 core ]   = 1152 samples = 24 ms
   2.667 ms    10.667 ms   10.667 ms
            29 carriers x 93.75 Hz, QPSK
```

One continuous 512-sample-periodic waveform, 2.25 periods long. The guard is a
true cyclic prefix and falls out of the construction rather than being
generated separately: `block[n] == core[(n - 128) % 512]` holds across all 1152
samples, which the test suite asserts rather than assumes.

Only 44% of the symbol time carries new information. The band could hold about
58 carriers at this symbol rate and the modem uses 29. What the repeat buys is
timing tolerance and a free frequency-offset estimator, against the 2.667 ms a
plain guard interval would give. On a link where the two soundcard clocks are
independent and the frame start is masked by squelch delay, that trade is
deliberate.

"128 guard + 1024 useful with every other bin nulled" and "128 guard + 512 core
sent twice" generate bit-identical samples, so a capture cannot distinguish the
two descriptions.

## The guard, the window and the 3 dB are one budget, not three

This is the part of the design most easily got backwards, and the receiver is
built around it.

With a channel impulse response of length `L`, a single 512-point FFT at offset
`d` is clean when `d >= L` and `d + 512 <= 1152`, so **`d` in `[L, 640]`**.
Delay spread eats timing tolerance one-for-one. The prefix's job is to widen
that window from 512 to 640 samples, not to act as a separate ISI guard beside
it.

Combining both cores for 3 dB additionally requires the second window to fit,
`d + 1024 <= 1152`, so **`d` in `[L, 128]`**:

| Placement `d` | Available | Timing freedom |
|---|---|---|
| `[L, 128]` | full 3 dB combining (equivalently a 1024-pt FFT with odd bins nulled) | 128 samples, 2.67 ms |
| `[L, 640]` | single core, no combining gain | 640 samples, 13 ms |

Both properties are real; they are endpoints of a slider, not additive. Small
`d` is the normal operating point and the 640-sample window is graceful
degradation for poor acquisition. The receiver places its window adaptively: it
estimates `L`, aims near `d = 64` and combines whenever `[L, 128]` is non-empty,
and slides out toward 640 on a single core when it is not. `symbol_carriers()`
raises rather than combining past `d = 128`, because silently returning garbage
there is the failure this design makes easy.

That also puts real weight on `L`. Multipath is nil on a short VHF FM path, but
the radios' audio filters are not — a steep 300–3000 Hz filter's impulse
response can run a millisecond or more (~48 samples), a third of the combining
window before any timing error at all. `probe_carriers.py` measures it.

## Exactly 17 codewords, always

`N = 648` is fixed and only `INFORMATION_BITS[rate]` changes, so the frame
carries exactly 17 LDPC codewords at every rate. Frame geometry, interleaver
and decode loop are identical across 1/2, 2/3 and 3/4 — changing rate changes
nothing structural.

This removes a known cost problem in `experiments/ofdm/` at the root. There,
`_deinterleave_llrs(llrs, n_blocks)` ends in `.reshape(ldpc.N, n_blocks).T`, so
the codeword count had to be known before de-interleaving could run — but the
count came from a length field living *inside* the first codeword.
`_fec_candidate_block_counts()` broke the deadlock by brute force, and a wrong
guess was the *most* expensive case of all, because it never cleared the
syndrome and so burned all 30 min-sum iterations before failing.

Here the count is a compile-time constant. One hypothesis, no search, and the
16-bit length field is purely informational rather than a search key. The 17
decodes are also identically shaped, so they batch: check-node degree varies
between check nodes but not between codewords, which lets the min-sum vectorise
over the codeword axis.

### 526 spare bits become scattered pilots

17 × 648 = 11 016 against 11 542 grid bits leaves 526 bits — 263 QPSK symbols,
4.6% of the payload. Rather than transmit them as zeros, the interleaver
scatters them in both time and frequency and the receiver uses them as known
phase references.

The specification deliberately has no pilots and asks the tracker to run
decision-directed across 199 symbols (4.8 s), which is the thinnest margin in
the receiver. These anchors cost nothing and change no published parameter of
the frame. Decision-directed tracking still does the work between them; 4.6%
alone is too thin to carry it.

## Capacity

| Mode | Info bits | Payload bytes | Net over 5.200 s |
|---|---|---|---|
| LDPC 1/2 | 5 508 | 684 | 1052 bit/s |
| LDPC 2/3 | 7 344 | 914 | 1406 bit/s |
| LDPC 3/4 | 8 262 | 1028 | 1582 bit/s |
| uncoded | 11 542 | 1438 | 2212 bit/s |

Uncoded is a diagnostic, not an operating mode: it is the spec-exact waveform
layer and the cleanest instrument for separating a DSP bug from a coding-margin
shortfall.

For scale, `experiments/ofdm/` confirmed 3301 bit/s on air on 2026-08-19. This
design buys timing tolerance and squelch robustness, not throughput.

## Where the frame duration comes from

45 ms lead-in plus 214 symbols is 2160 + 246 528 = 248 688 samples = 5.181 s,
short of the specified 5.195–5.205 s. A 912-sample (19 ms) ramp-down and settle
tail closes it to exactly 249 600 samples = 5.200 s.

That is our choice for what fills the gap, not something the specification
stated. It is recorded here and in the module docstring so a later reader can
see it was a decision.

## Files

| File | Role |
|---|---|
| `qpsk29.py` | the modem — constants, profile, `modulate`, `demodulate`, `demodulate_debug` |
| `ldpc.py` | IEEE 802.11n length-648 QC-LDPC, copied so this experiment stands alone |
| `test_qpsk29.py` | software invariants, no hardware |
| `screen_qpsk29.py` | software channel model, no hardware |
| `probe_carriers.py` | on-air per-carrier SNR, channel magnitude and delay spread |
| `run_qpsk29.py` | on-air frame runner |
| `diagnose_qpsk29.py` | offline post-mortem of a saved capture |
| `RESULTS.md` | dated on-air outcomes |

## Running

Software only, no radios:

    python experiments/qpsk29/test_qpsk29.py
    python experiments/qpsk29/screen_qpsk29.py

On air — these key a transmitter for 5.2 s per frame:

    python experiments/qpsk29/probe_carriers.py --direction ht-to-ic705 \
        --out experiments/qpsk29/results/measurements/probe.json

    python experiments/qpsk29/run_qpsk29.py --direction ht-to-ic705 \
        --fec 2/3 --trials 10 \
        --capture-dir experiments/qpsk29/results/captures/ldpc23 \
        --out experiments/qpsk29/results/sweeps/ldpc23.json

Post-mortem of a capture:

    python experiments/qpsk29/diagnose_qpsk29.py \
        --capture <run>.npy --payload-file <run>.bin

Note `pyproject.toml` sets `testpaths = ["tests"]`, so `test_qpsk29.py` is run
by name and is not collected by a bare `pytest`.

## Status

Contract frozen, implementation in progress. Nothing has been on air yet;
`RESULTS.md` is where that will be recorded when it has.
