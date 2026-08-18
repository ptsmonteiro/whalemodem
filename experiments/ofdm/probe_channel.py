"""What the FM audio chain actually does to 300-3000 Hz, measured directly.

This runs before the ladder and it is the highest-value airtime in the whole
experiment, for a reason specific to OFDM with no FEC: a single subcarrier
sitting in a notch produces errors in every symbol of every frame, and no
amount of margin on the other thirty-four compensates. A blind ladder cannot
tell that failure from "the mode is too fast" -- it just sees 0/5 -- and it
costs a rung of airtime to learn nothing. One probe answers it directly.

An OFDM training symbol is already a per-subcarrier channel measurement; the
receiver computes H[k] on every frame it decodes because it cannot equalise
without it. So this script sends nothing exotic: a frame that is all preamble
and training symbols, spread over a deliberately *wider* band than any
candidate uses, and reads off what the equaliser would have seen.

Three things come back, and each answers a question the repo has been guessing
at:

  - **|H[k]|, the amplitude response.** scripts/measure_band_edges.py put the
    usable band at 600-2300 Hz using a 2-byte FSK frame, which is the easiest
    thing this channel ever carries; experiments/mfsk/RESULTS.md lists
    re-measuring it as follow-up #1 because its winner's top tone sits at 2287
    Hz, essentially on that ceiling. This measures the same edges with 91
    simultaneous probes and a real noise estimate behind each.

  - **Per-subcarrier SNR**, from the scatter of the individual training
    symbols about their mean. This is what says whether a subcarrier is
    *usable*, as opposed to merely present -- and with no FEC, the worst
    subcarrier in the band is what decides whether frames decode.

  - **Delay spread**, from the impulse response the phase of H implies. This
    is the number the whole cyclic-prefix trade turns on and nothing in this
    repo has ever measured it. If the spread is 0.5 ms, the cheapest prefix in
    the candidate set is ample and the ladder should start at the top; if it
    is 4 ms, most of the candidate set is doomed and the ladder should not
    waste rungs finding that out one at a time.

The band it probes is deliberately wider than the band it recommends. Nothing
is being transmitted outside what the radio will pass anyway -- the audio
chain simply attenuates it, which is precisely the measurement.

Run: python experiments/ofdm/probe_channel.py
     python experiments/ofdm/probe_channel.py --trials 5 \
         --out experiments/ofdm/results/measurements/probe.json
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from scipy.signal import hilbert

import ofdm
from whale.transport import RadioTransport

# The probe frame: 30 Hz subcarriers from 300 to 3000 Hz, and enough repeats
# of the training symbol to make the per-subcarrier noise estimate mean
# something. 24 repeats is 0.9s of air, so the whole probe frame is about
# 1.1s -- well inside the keying cap, which this script is not trying to fill.
PROBE_N_FFT = 1600
PROBE_CP = 200
PROBE_TRAIN = 24
PROBE_BAND = (300.0, 3000.0)

# An SNR a subcarrier has to reach to be called usable. 15 dB is roughly what
# uncoded QPSK needs for the ~1e-5 bit error rate a 900-byte CRC-only frame
# implies, with a few dB in hand; it is a reporting threshold, not a decision
# the modem makes anywhere.
USABLE_SNR_DB = 15.0

CAPTURE_TAIL = 0.8
INTER_TRIAL = 0.6


def probe_profile(band=PROBE_BAND):
    return ofdm.OfdmProfile(name="probe", n_fft=PROBE_N_FFT, cp=PROBE_CP,
                            bits_per_carrier=2, band_low=band[0], band_high=band[1],
                            n_train=PROBE_TRAIN)


def measure(audio, profile):
    """Per-subcarrier channel and noise from one captured probe frame.

    Returns None if the preamble was not found -- which is itself a result
    worth printing rather than crashing on, since a probe that will not sync
    is the same news the ladder would have delivered more expensively.
    """
    audio = np.asarray(audio, dtype=np.float64)
    proposals, _ = ofdm._propose(audio, profile)
    if not proposals:
        return None
    analytic = hilbert(audio)
    best = None
    for proposal in proposals:
        start, confidence = ofdm._refine(analytic, profile, proposal)
        if best is None or confidence > best[1]:
            best = (start, confidence)
    start, confidence = best
    if confidence < profile.confidence_threshold:
        return None

    first = start + profile.n_fft + profile.cp
    if first + profile.n_train * profile.symbol_samples > len(audio):
        return None

    # The per-symbol estimates, rather than _equalise's mean, because the
    # scatter between them is the noise measurement.
    guard = int(ofdm._WINDOW_GUARD_FRACTION * profile.cp)
    estimates = []
    for i in range(profile.n_train):
        w0 = first + i * profile.symbol_samples - guard
        window = audio[w0:w0 + profile.n_fft]
        if len(window) < profile.n_fft:
            break
        estimates.append(np.fft.rfft(window)[profile.carriers]
                         * np.conj(ofdm.training_values(profile)))
    if len(estimates) < 2:
        return None
    estimates = np.vstack(estimates)
    channel = estimates.mean(axis=0)
    noise = estimates.var(axis=0, ddof=1)
    with np.errstate(divide="ignore"):
        snr_db = 10 * np.log10(np.maximum(np.abs(channel) ** 2, 1e-30)
                               / np.maximum(noise, 1e-30))
    return {"confidence": confidence, "channel": channel, "snr_db": snr_db,
            "symbols": len(estimates)}


def delay_spread_ms(channel, profile, floor_db=-20.0):
    """RMS delay spread implied by the measured channel, in milliseconds.

    H[k] is the frequency response, so its inverse transform is the impulse
    response -- band-limited to the probed span, which sets the resolution at
    about 1/bandwidth (0.37 ms over 2.7 kHz). That is coarse, and it is
    deliberately compared against prefixes of 0.8-5 ms rather than used to
    pick one to the microsecond.

    Only taps within `floor_db` of the peak are counted. Below that the
    estimate is measuring its own noise, and a noise floor spread across the
    whole window would report an impressive and entirely fictitious spread.
    """
    spectrum = np.zeros(profile.n_fft // 2 + 1, dtype=complex)
    spectrum[profile.carriers] = channel
    impulse = np.abs(np.fft.irfft(spectrum, n=profile.n_fft))
    impulse = np.roll(impulse, profile.n_fft // 2)
    power = impulse ** 2
    keep = power >= power.max() * 10 ** (floor_db / 10)
    taps = np.flatnonzero(keep)
    if len(taps) < 2:
        return 0.0
    t = taps / ofdm.SAMPLE_RATE
    w = power[taps]
    mean = np.sum(w * t) / np.sum(w)
    return float(1000 * np.sqrt(np.sum(w * (t - mean) ** 2) / np.sum(w)))


def run_direction(tx, rx, profile, trials, label):
    """`trials` probe frames one way; averages the channel over those that
    decoded. Averaging complex H rather than |H| on purpose -- a phase that
    does not repeat between trials is not a channel, and averaging it away is
    the honest outcome."""
    channels, snrs, confidences = [], [], []
    for i in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        keyed = tx.send(ofdm.modulate(b"", profile))
        time.sleep(CAPTURE_TAIL)
        result = measure(rx.snapshot_rx(), profile)
        if result is None:
            print(f"  [{label}] {i}/{trials}: keyed={keyed:.2f}s NO SYNC")
        else:
            channels.append(result["channel"])
            snrs.append(result["snr_db"])
            confidences.append(result["confidence"])
            usable = int(np.sum(result["snr_db"] >= USABLE_SNR_DB))
            print(f"  [{label}] {i}/{trials}: keyed={keyed:.2f}s "
                  f"conf={result['confidence']:.3f} "
                  f"median SNR={np.median(result['snr_db']):.1f}dB "
                  f"usable={usable}/{len(result['snr_db'])}")
        time.sleep(INTER_TRIAL)
    if not channels:
        return None
    return {"channel": np.mean(channels, axis=0), "snr_db": np.mean(snrs, axis=0),
            "confidence": float(np.mean(confidences)), "decoded": len(channels)}


def report(profile, summary, label):
    freqs = profile.carriers * profile.spacing
    mag = 20 * np.log10(np.maximum(np.abs(summary["channel"]), 1e-12))
    mag -= mag.max()
    snr = summary["snr_db"]

    print(f"\n-- {label}: response and per-subcarrier SNR "
          f"({summary['decoded']} frames averaged)")
    print("     Hz   |H| dB   SNR dB")
    for f, m, s in zip(freqs, mag, snr):
        bar = "#" * int(max(0, min(30, (s - 5) / 1.5)))
        flag = "" if s >= USABLE_SNR_DB else "   <-- below usable"
        print(f"  {f:6.0f}  {m:6.1f}  {s:6.1f}  {bar}{flag}")

    usable = freqs[snr >= USABLE_SNR_DB]
    spread = delay_spread_ms(summary["channel"], profile)
    if len(usable):
        print(f"  usable band: {usable.min():.0f}-{usable.max():.0f} Hz "
              f"({len(usable)}/{len(freqs)} subcarriers at >= {USABLE_SNR_DB:.0f} dB)")
    else:
        print("  usable band: NONE at this threshold")
    print(f"  RMS delay spread: {spread:.2f} ms")
    return {"freqs": freqs.tolist(), "mag_db": mag.tolist(), "snr_db": snr.tolist(),
            "usable_low": float(usable.min()) if len(usable) else None,
            "usable_high": float(usable.max()) if len(usable) else None,
            "delay_spread_ms": spread, "decoded": summary["decoded"]}


def recommend(per_direction):
    """The worst direction decides, as everywhere else in this repo -- the two
    legs of this bench have materially different SNR and a band that holds one
    way is not a band."""
    lows = [d["usable_low"] for d in per_direction.values() if d["usable_low"] is not None]
    highs = [d["usable_high"] for d in per_direction.values() if d["usable_high"] is not None]
    spreads = [d["delay_spread_ms"] for d in per_direction.values()]
    if not lows or not highs:
        return None
    band = (max(lows), min(highs))
    spread = max(spreads)
    print("\n===== RECOMMENDATION (worst direction) =====")
    print(f"  band          {band[0]:.0f}-{band[1]:.0f} Hz "
          f"(measure_band_edges.py said {ofdm.BAND_LOW_HZ:.0f}-{ofdm.BAND_HIGH_HZ:.0f})")
    print(f"  delay spread  {spread:.2f} ms")
    print(f"  a prefix wants to be several times that, so >= {3 * spread:.2f} ms")
    for profile in ofdm.candidates(bits=(2,), band=band)[:6]:
        verdict = "ok" if profile.cp_seconds * 1000 >= 3 * spread else "prefix too short"
        print(f"    {ofdm.describe(profile)}   {verdict}")
    return {"band": list(band), "delay_spread_ms": spread}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=3, help="probe frames per direction")
    ap.add_argument("--out", default=None, help="write the measurement as JSON here")
    args = ap.parse_args()

    profile = probe_profile()
    print(f"probe: {profile.n_carriers} subcarriers, {profile.spacing:.0f} Hz apart, "
          f"{PROBE_BAND[0]:.0f}-{PROBE_BAND[1]:.0f} Hz, {profile.n_train} training symbols, "
          f"{ofdm.keying_seconds(profile, 0):.2f}s keying")

    print("\nopening radios...")
    t_a = RadioTransport("ic705")
    t_b = RadioTransport("ht")
    results = {}
    try:
        t_a.start_receiving()
        t_b.start_receiving()
        print("warming up 2s...")
        time.sleep(2)
        for tx, rx, label in ((t_a, t_b, "ic705->ht"), (t_b, t_a, "ht->ic705")):
            summary = run_direction(tx, rx, profile, args.trials, label)
            if summary is None:
                print(f"  [{label}] nothing decoded -- no measurement this direction")
                continue
            results[label] = report(profile, summary, label)
    finally:
        t_a.close()
        t_b.close()

    recommendation = recommend(results) if len(results) == 2 else None
    if not recommendation:
        print("\nBoth directions are needed for a recommendation; "
              "one or both produced nothing.")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "when": datetime.now(timezone.utc).isoformat(),
            "probe": {"n_fft": PROBE_N_FFT, "cp": PROBE_CP, "n_train": PROBE_TRAIN,
                      "band": list(PROBE_BAND)},
            "directions": results, "recommendation": recommendation,
        }, indent=2))
        print(f"\nwrote {args.out}")
    return 0 if len(results) == 2 else 1


if __name__ == "__main__":
    sys.exit(main())
