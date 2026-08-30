# HC2 (experimental candidate) frame Monte Carlo sweep, 2026-08-30

Diagnostic only, not promotion evidence. HC2 is an unregistered experimental
mode (`experiments/hc2/hc2.py`, no mode ID, not in `whale.mode_qualification.MANIFEST`),
so this run used `experiments/hc2/benchmark_hc2.py` rather than
`scripts/benchmark_simulated_channels.py`, with the same underlying trial
runner (`whale.qualification.run_frame_trial`) and Wilson-95% summary. 100
trials per point, full-capacity payloads, waveform SNR -5/0/5/10/15/20 dB
(AWGN swept from 0 dB), across AWGN and all three mid-latitude Watterson
presets. Run against a dirty tree (the HC2 exploration itself) on 2026-08-30.

Result: HC2 (differential 8-PSK + K=9 conv code on HC1's carrier geometry)
beats HC1 in AWGN/quiet throughput (+54% payload capacity, 92-100% delivery)
but collapses under moderate and disturbed Watterson conditions relative to
HC1's own retained numbers (`logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-30/`),
never exceeding 61/100 moderate or 25/100 disturbed versus HC1's 96/100 and
61/100. This is a rejected design, not a qualifying result for any gate; see
`experiments/hc2/RESULTS.md` for the full point-by-point table, the failure
mechanism (higher-order modulation raises the cost of the same correlated
multi-carrier fade that already limits HC1), and recommended next designs
(outer burst coding or frequency diversity, not higher modulation order).
