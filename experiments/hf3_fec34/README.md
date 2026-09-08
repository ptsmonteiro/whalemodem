# HF3 rate-3/4 candidate

HF3 reuses HF4's 16-QAM OFDM carrier geometry, synchronization, equalizer,
interleaver, and packet framing. It uses a shorter 36-data-symbol frame with
pilots every 6 symbols, reducing Doppler exposure to about 3.52 seconds. It
also duplicates coded bits across two separated carrier groups and combines
their soft metrics at decode time. Its inner K=7 convolutional code is
punctured to exactly 3/4:

```text
FEC_K = 3
FEC_N = 4
```

The implementation is `whale/phy/hf3.py` (developed here as
`experiments/hf3_fec34.py`); `whale/modes/hf3_mode.py`
adapts it to the link ABI using the existing mode ID 9. The physical payload
capacity is 992 bytes before the link air header. The previous OFDM
implementation in `experiments/hf3/` is retained as historical experiment
material and is no longer the negotiated HF3 adapter.

HF3 also enables a smoothed per-carrier complex channel estimate from the
dense pilots. Frequency diversity recovers static and quiet two-path cases
through +36 dB, while moderate two-path fading still fails. The production
candidate remains rate 3/4.

Quick clean-path check:

```bash
python -m pytest tests/test_mode_conformance.py -q
```

The SNR/channel screen should be run after installing the project test
dependencies, for example with the HF4 benchmark harness adapted to import
`whale.phy.hf3` and its mode. Existing HF3 qualification numbers do
not transfer to this replacement; they remain historical evidence only.

Controlled-channel result: single-path quiet Doppler passes at 20 dB;
single-path moderate Doppler passes at 28 dB; static and quiet two-path
fading pass through +36 dB with diversity; moderate two-path fading remains
unresolved.
