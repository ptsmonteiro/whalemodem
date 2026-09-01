# HF3 fixed-mode useful-transfer campaign

This lifecycle-free campaign measures HF3 DATA with the existing normal HC0
`DATA_ACK`, stop-and-wait retries, the HF policy's 300 ms turnaround at each
direction change, and waveform-embedded PTT lead/tail. It excludes connection,
capability negotiation, adaptation/fallback, and disconnect. Each independent
trial offers and byte-verifies 10,000 application bytes (13 HF3 DATA frames).

Commands:

```text
python scripts/benchmark_fixed_mode_transfer.py --model benign-static --point 8 --trials 6 --bytes 10000 --seed 20260901 --out logs/mode_qualification/hf-ssb/hf3/2026-09-01-fixed-transfer/hf3_benign_static_8db.json
python scripts/benchmark_fixed_mode_transfer.py --model watterson --watterson-preset mid_latitude_quiet --point 10 --trials 6 --bytes 10000 --seed 20260901 --out logs/mode_qualification/hf-ssb/hf3/2026-09-01-fixed-transfer/hf3_quiet_watterson_10db.json
```

The six-trial interval is the exact distribution-free 95% confidence interval
for the population median: with six observations its endpoints are the sample
minimum and maximum (96.875% coverage). Six trials are enough to decide this
gate because even the upper endpoint is far below the 2,000 bit/s floor.

| Required boundary | Exact transfers | Retries | Median useful bit/s | Median 95% CI (bit/s) | Gate |
| --- | ---: | ---: | ---: | ---: | --- |
| Benign/static +8 dB | 6/6 (60,000 B) | 0 | 855.3 | 855.3--855.3 | Fail |
| Quiet Watterson +10 dB | 6/6 (60,000 B) | 1 | 855.3 | 756.7--855.3 | Fail |

This is not an HF3 decoder failure: 12/12 transfers completed exactly and only
one of 156 DATA frames needed retransmission. The clean-channel ceiling is set
by the current link exchange. Every fixed 3.17-second HF3 DATA frame is followed
by a fixed roughly 3.42-second HC0 ACK plus 600 ms total turnaround. Thus HF3's
2,025 bit/s frame payload rate cannot become 2,000 bit/s stop-and-wait
application throughput under the present protocol. Qualifying this waveform as
Level 3 requires changing the useful-transfer contract or introducing a more
efficient normal acknowledgement strategy and then rerunning this campaign.

The artifacts record the Git commit and dirty-tree state. The working tree was
already dirty with the active qualification changes, so these results are a
reproducible failing diagnosis, not promotion evidence.
