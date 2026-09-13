"""How much per-carrier SNR can this FM path actually deliver?

VF9 measures 13-16 dB median per-carrier SNR over the real ic705/ht bench,
which caps it at QPSK -- 8PSK already fails, and 256-QAM would want 25-30 dB.
But a clean FM link should manage far more than 16 dB, so something is holding
this one down. This finds out what, and how high the ceiling really is.

The method separates the two candidates. If the path were NOISE-limited, a
carrier's SNR would depend only on the power in that carrier, and spreading
the same drive across more carriers would cost exactly the power each one
loses -- nothing more. If instead it is INTERMODULATION-limited, carrier count
costs extra: more carriers means a higher crest factor for the same average
power, the transmitter's nonlinearity sees bigger peaks, and on a 50 Hz grid
every third-order product lands exactly on another carrier. So the experiment
sweeps carrier count and drive together and asks what the best achievable
per-carrier SNR is for each count.

One carrier is the reference point that matters: a single OFDM carrier is a
constant-envelope tone (crest factor ~3 dB against ~12-17 dB for 49 carriers),
so it is the closest thing to an undistorted probe of what the radios can do.
If a single carrier reaches 30 dB while 49 carriers plateau at 15, the ceiling
belongs to our waveform's crest factor and not to the radios, and reducing it
is worth real effort. If a single carrier also stops near 15 dB, the limit is
in the audio chain itself and no amount of PAPR work will buy high-order QAM.

Carriers are spread evenly across VF9's 500-2900 Hz band whatever the count,
so every run samples the same band rather than a favourable corner of it.

Run: python scripts/measure_fm_snr_ceiling.py
     python scripts/measure_fm_snr_ceiling.py --carriers 1,49 --rms 0.06,0.1
"""
import argparse
import csv
import sys
import time

import numpy as np
from scipy.signal import correlate, hilbert

import bench
from whale.transport import RX_SAMPLE_RATE, SAMPLE_RATE

SPACING_HZ = 50.0
BIN_LO, BIN_HI = 10, 58          # 500..2900 Hz, VF9's band
NFFT_TX = int(SAMPLE_RATE / SPACING_HZ)       # 960
NFFT_RX = int(RX_SAMPLE_RATE / SPACING_HZ)    # 240
DECIMATION = SAMPLE_RATE // RX_SAMPLE_RATE
CP_RX = 36
CP_TX = CP_RX * DECIMATION
RX_STRIDE = NFFT_RX + CP_RX

LEADIN_SYMBOLS = 45              # ~1 s, to open squelch and settle AGC
SYNC_SYMBOLS = 4
ESTIMATE_SYMBOLS = 8             # 8, not 4: a noisy channel estimate would
                                 # itself cap the SNR we are trying to measure
PAYLOAD_SYMBOLS = 20

SEARCH_BACK, SEARCH_FORWARD = 56, 8
CAPTURE_TAIL = 0.7


def qpsk(bits):
    pairs = bits.reshape(-1, 2).astype(np.float64)
    return ((1.0 - 2.0 * pairs[:, 0])
            + 1j * (1.0 - 2.0 * pairs[:, 1])) / np.sqrt(2.0)


def pick_carriers(count):
    """`count` bins spread evenly across the band, endpoints included."""
    if count == 1:
        return np.array([(BIN_LO + BIN_HI) // 2])
    return np.unique(np.round(np.linspace(BIN_LO, BIN_HI, count)).astype(int))


def values(seed, symbols, n_carriers):
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, symbols * n_carriers * 2, dtype=np.uint8)
    return qpsk(bits).reshape(symbols, n_carriers)


def build(rows, bins, nfft, cp):
    out = np.empty((len(rows), nfft + cp))
    for i, row in enumerate(rows):
        spectrum = np.zeros(nfft // 2 + 1, dtype=complex)
        spectrum[bins] = row
        body = np.fft.irfft(spectrum, nfft)
        if cp:
            out[i, :cp] = body[-cp:]
        out[i, cp:] = body
    return out.reshape(-1)


def make_burst(bins, rms):
    n = len(bins)
    sync_one = values(0x5104, 1, n)
    sync = np.array([1.0, 1.0, -1.0, 1.0])[:, None] * sync_one
    estimate = values(0xE57A, ESTIMATE_SYMBOLS, n)
    payload = values(0x9A17, PAYLOAD_SYMBOLS, n)
    leadin = values(0x1EAD, 1, n)

    body = build(np.vstack([sync, estimate, payload]), bins, NFFT_TX, CP_TX)
    lead = np.resize(build(leadin, bins, NFFT_TX, 0),
                     LEADIN_SYMBOLS * (NFFT_TX + CP_TX)).copy()
    lead[:240] *= np.linspace(0.0, 1.0, 240)

    audio = np.concatenate([lead, body, np.zeros(960)])
    audio *= rms / np.sqrt(np.mean(audio ** 2))
    peak = float(np.max(np.abs(audio)))
    return {"audio": np.clip(audio, -0.95, 0.95).astype(np.float32),
            "sync": sync, "estimate": estimate, "payload": payload,
            "bins": bins, "peak": peak,
            "papr_db": 20 * np.log10(peak / rms)}


def extract(audio, start, symbols, bins):
    if start < 0 or start + symbols * RX_STRIDE > len(audio):
        return None
    out = np.empty((symbols, len(bins)), dtype=complex)
    for i in range(symbols):
        base = start + i * RX_STRIDE + CP_RX
        out[i] = np.fft.rfft(audio[base:base + NFFT_RX], NFFT_RX)[bins]
    return out


def fit(observed, reference):
    channel = (np.sum(observed * np.conj(reference), axis=0)
               / np.sum(np.abs(reference) ** 2, axis=0))
    modelled = channel * reference
    signal = float(np.mean(np.abs(modelled) ** 2))
    residual = float(np.mean(np.abs(observed - modelled) ** 2))
    return channel, (residual / signal if signal > 0 else np.inf)


def analyse(captured, burst):
    bins = burst["bins"]
    reference = build(burst["sync"], bins, NFFT_RX, CP_RX)
    if len(captured) < len(reference) * 2:
        return None
    corr = np.abs(correlate(hilbert(captured.astype(np.float64)),
                            hilbert(reference), mode="valid"))
    coarse = int(np.argmax(corr))

    best = None
    for offset in range(-SEARCH_BACK, SEARCH_FORWARD + 1):
        start = coarse + offset
        observed = extract(captured, start + SYNC_SYMBOLS * RX_STRIDE,
                           ESTIMATE_SYMBOLS, bins)
        if observed is None:
            continue
        _, evm = fit(observed, burst["estimate"])
        if best is None or evm < best[1]:
            best = (start, evm)
    if best is None:
        return None

    block = extract(captured, best[0] + SYNC_SYMBOLS * RX_STRIDE,
                    ESTIMATE_SYMBOLS + PAYLOAD_SYMBOLS, bins)
    if block is None:
        return None
    channel, _ = fit(block[:ESTIMATE_SYMBOLS], burst["estimate"])
    safe = np.where(np.abs(channel) > 1e-12, channel, 1e-12)
    equalised = block[ESTIMATE_SYMBOLS:] / safe
    error = np.mean(np.abs(equalised - burst["payload"]) ** 2, axis=0)
    return 10 * np.log10(1.0 / np.maximum(error, 1e-12))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--carriers", default="1,7,25,49",
                    help="comma-separated carrier counts to sweep")
    ap.add_argument("--rms", default="0.03,0.06,0.1,0.16",
                    help="comma-separated transmit RMS levels to sweep")
    ap.add_argument("--out", default="scratch_fm_snr_ceiling.csv")
    args = ap.parse_args()

    counts = [int(x) for x in args.carriers.split(",")]
    levels = [float(x) for x in args.rms.split(",")]
    rows = []

    print(f"sweeping carriers {counts} x rms {levels}, "
          f"band {BIN_LO * SPACING_HZ:.0f}-{BIN_HI * SPACING_HZ:.0f} Hz")

    with bench.radio_pair() as (t_ic705, t_ht):
        for count in counts:
            bins = pick_carriers(count)
            for rms in levels:
                burst = make_burst(bins, rms)
                for tx, rx, tx_name, rx_name in (
                        (t_ic705, t_ht, "ic705", "ht"),
                        (t_ht, t_ic705, "ht", "ic705")):
                    stale = rx.snapshot_rx()
                    rx.consume_rx(len(stale))
                    tx.send(burst["audio"])
                    time.sleep(CAPTURE_TAIL)
                    snr = analyse(rx.snapshot_rx(), burst)
                    if snr is None:
                        print(f"  {count:2d} carriers rms={rms:.3f} "
                              f"{tx_name}->{rx_name}: NOT ACQUIRED")
                        continue
                    print(f"  {count:2d} carriers rms={rms:.3f} "
                          f"(papr {burst['papr_db']:4.1f} dB, peak "
                          f"{burst['peak']:.2f}) {tx_name}->{rx_name}: "
                          f"SNR min/med/max = {snr.min():5.1f}/"
                          f"{np.median(snr):5.1f}/{snr.max():5.1f} dB")
                    for b, s in zip(bins, snr):
                        rows.append([count, rms, f"{burst['papr_db']:.2f}",
                                     f"{tx_name}->{rx_name}",
                                     b * SPACING_HZ, f"{s:.2f}"])
                    time.sleep(0.4)

    if rows:
        with open(args.out, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["carriers", "rms", "papr_db", "direction",
                             "freq_hz", "snr_db"])
            writer.writerows(rows)
        print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
