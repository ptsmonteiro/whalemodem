"""Software pre-screen for MFSK candidates: how much SNR each one needs.

Bench time is the scarce thing -- one candidate at full payload, five trials,
both directions, is about 50 seconds of airtime -- and most of the aggressive
sub-orthogonal candidates in mfsk.candidates() can be ruled out without keying
a radio. This walks each candidate down an AWGN waterfall and reports the SNR
it needs to decode every trial, then compares that against what the shipped
1200-baud profile needs on the same yardstick.


Why the comparison is relative, and not against measure_snr.py's dB
-------------------------------------------------------------------

The obvious screen -- run every candidate at the 7.5 dB scripts/measure_snr.py
reported for the weak leg -- gives the wrong answer, and it is worth writing
down why, because the number looks authoritative.

Run PROFILE_1200 itself through this AWGN model at 7.5 dB and it decodes 0 of
10 frames. On the actual radios, at the SNR that script measured, it decodes
100%. So the two 7.5 dBs are not the same quantity, and the model is the
honest one: measure_snr.py estimates in-band noise by taking the PSD of two
side bands just outside the tone span and scaling it into the span. Under FM
that extrapolation is pessimistic by a wide margin -- a captured FM carrier
quiets the noise inside the occupied band far more than beside it, which is
the whole reason FM is used at these levels. The side bands are measuring
unquieted noise and the tone band is not.

Rather than try to calibrate that offset, this screens on a ratio. Walking the
three shipped profiles down the waterfall gives the SNR each needs for 100%:

    300 baud     8 dB
    600 baud    12 dB
    1200 baud   13 dB

and PROFILE_1200 is known to run at 100% on this bench, both directions, at
full 355-byte frames. So 13 dB on this scale is a link margin the bench
demonstrably has. A candidate needing 13 dB or less has at least as much
margin as the mode already in service, measured the same way -- and a
candidate needing 16 dB is asking for 3 dB the bench has not been shown to
have, whatever its throughput looks like on paper.

REFERENCE_PROFILE / REFERENCE_THRESHOLD_DB below is that bar, and it is
re-measured on every run rather than hardcoded, so a change to the modem or to
the profile moves the bar with it.

What this still cannot see: group delay and the frequency response of the FM
audio chain. AWGN is the easy half of the channel. The repo is full of
placements that decoded at 0.99 confidence in software and 0/6 on air, so a
pass here is permission to spend bench time, not a result. sweep_mfsk.py
decides.

--tilt puts one of those effects in scope. A linear gain slope across the tone
band is the failure mode M-ary detection has and binary FSK does not: an
argmax over four tones spanning 1700 Hz is decided by whichever tone came back
loudest, so a chain rolling off across the band biases every decision toward
one end. It is also exactly what the preamble training in mfsk._training_gains
removes, so --tilt with and without --no-training measures what that training
is worth.

Run: python experiments/mfsk/screen_mfsk.py
     python experiments/mfsk/screen_mfsk.py --top 40 --trials 30
     python experiments/mfsk/screen_mfsk.py --tilt 6 --no-training
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

import mfsk
from whale import afsk

# The mode already in service, and the payload throughput it delivers. Any
# candidate that cannot beat this number is not worth building.
REFERENCE_PROFILE = afsk.PROFILE_1200
REFERENCE_BITRATE = REFERENCE_PROFILE.chunk_size * 8 / mfsk.MAX_KEYING_SECONDS

BAND_MARGIN = 200.0  # matches measure_snr.BAND_MARGIN
WATERFALL_DB = np.arange(4.0, 26.1, 1.0)


class _BinaryShim:
    """Lets the noise model see an afsk.Profile through MfskProfile's .tones,
    so the reference profile is measured by exactly the same code path as the
    candidates rather than by a parallel one that might differ."""

    def __init__(self, profile):
        self.tones = np.array(sorted([profile.freq0, profile.freq1]))


def add_awgn(audio, profile, snr_db, rng, sample_rate=mfsk.SAMPLE_RATE):
    """White noise scaled so the in-band SNR matches `snr_db` under
    measure_snr.py's definition -- in-band signal power over noise power
    inside the tone span plus BAND_MARGIN either side.

    White noise of variance s^2 spreads that power flat over 0..fs/2, so the
    power landing in a band of width B is s^2 * B / (fs/2). Inverting that for
    s^2 keeps this script's dB on the same scale as the bench script's, which
    is what makes the ratio in the module docstring meaningful.
    """
    lo = profile.tones[0] - BAND_MARGIN
    hi = profile.tones[-1] + BAND_MARGIN
    spectrum = np.fft.rfft(audio)
    freqs = np.fft.rfftfreq(len(audio), 1 / sample_rate)
    in_band = (freqs >= lo) & (freqs <= hi)

    # Parseval: in-band share of total power, applied to the signal's power.
    total = float(np.sum(np.abs(spectrum) ** 2))
    signal_power = float(np.mean(audio.astype(np.float64) ** 2))
    if total > 0:
        signal_power *= float(np.sum(np.abs(spectrum[in_band]) ** 2)) / total

    variance = (signal_power / (10 ** (snr_db / 10))) * (sample_rate / 2) / (hi - lo)
    return audio + rng.normal(0.0, np.sqrt(variance), len(audio)).astype(np.float32)


def apply_tilt(audio, profile, tilt_db, sample_rate=mfsk.SAMPLE_RATE):
    """A linear gain slope across the tone band: lowest tone +tilt/2 dB,
    highest -tilt/2. Magnitude only -- this models a response without the
    group delay that travels with a real one, so it is the optimistic half of
    what a radio chain does to a signal."""
    if not tilt_db:
        return audio
    spectrum = np.fft.rfft(audio)
    freqs = np.fft.rfftfreq(len(audio), 1 / sample_rate)
    lo, hi = profile.tones[0], profile.tones[-1]
    frac = np.clip((freqs - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    return np.fft.irfft(spectrum * 10 ** ((tilt_db / 2 - tilt_db * frac) / 20),
                        n=len(audio)).astype(np.float32)


def _decode_rate(modulate, demodulate, noise_view, payload_len, snr_db, trials, rng,
                 tilt_db=0.0, pad_seconds=0.5):
    """Decode rate at one SNR, at the full keying-budget payload.

    Full size deliberately. A two-byte probe frame is the easiest thing on the
    channel and says nothing about the mode you would actually run; the point
    of the 3s cap is that the payload fills it, and a frame is all-or-nothing
    under CRC, so the error rate that matters is the one over ~3000 bits.
    """
    ok = 0
    confidences = []
    pad = np.zeros(int(pad_seconds * mfsk.SAMPLE_RATE), dtype=np.float32)
    for _ in range(trials):
        payload = rng.integers(0, 256, payload_len, dtype=np.uint8).tobytes()
        audio = apply_tilt(modulate(payload), noise_view, tilt_db)
        audio = add_awgn(audio, noise_view, snr_db, rng)
        result = demodulate(np.concatenate([pad, audio, pad]))
        confidences.append(result.get("confidence", 0.0))
        ok += int(result.get("payload") == payload)
    return ok / trials, float(np.mean(confidences)) if confidences else 0.0


def threshold_db(modulate, demodulate, noise_view, payload_len, trials, rng, tilt_db=0.0):
    """Lowest SNR on WATERFALL_DB at which every trial decodes, or None if the
    candidate never gets there. Walked from the top down and stopped at the
    first failure, so a candidate that is hopeless costs two points rather
    than the whole waterfall."""
    best = None
    best_conf = 0.0
    for snr in reversed(WATERFALL_DB):
        rate, conf = _decode_rate(modulate, demodulate, noise_view, payload_len,
                                  snr, trials, rng, tilt_db)
        if rate < 1.0:
            return best, best_conf
        best, best_conf = float(snr), conf
    return best, best_conf


def reference_threshold(trials, rng, tilt_db=0.0):
    p = REFERENCE_PROFILE
    return threshold_db(lambda pl: afsk.modulate(pl, profile=p),
                        lambda a: afsk.demodulate(a, profile=p),
                        _BinaryShim(p), p.chunk_size, trials, rng, tilt_db)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--top", type=int, default=30, help="candidates to screen")
    ap.add_argument("--tilt", type=float, default=0.0,
                    help="linear gain slope across the tone band, in dB")
    ap.add_argument("--no-training", action="store_true",
                    help="disable the preamble-trained per-tone equaliser")
    ap.add_argument("--margin", type=float, default=0.0,
                    help="dB a candidate may need *beyond* the reference and still "
                         "make the shortlist (default: none -- must match or beat it)")
    ap.add_argument("--seed", type=int, default=20260818)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    ref_db, _ = reference_threshold(args.trials, rng, args.tilt)
    if ref_db is None:
        print("reference profile never reached 100% -- the model or the tilt is too harsh")
        return 1
    bar = ref_db + args.margin
    print(f"reference: {REFERENCE_PROFILE.name} needs {ref_db:.0f} dB for "
          f"{args.trials}/{args.trials}, delivers {REFERENCE_BITRATE:.0f} payload bits/s")
    print(f"bar: a candidate must need <= {bar:.0f} dB and beat "
          f"{REFERENCE_BITRATE:.0f} bits/s"
          + (f", tilt={args.tilt:g} dB" if args.tilt else "")
          + (", training OFF" if args.no_training else "") + "\n")

    pool = [p for p in mfsk.candidates(train_on_preamble=not args.no_training)[:args.top]
            if p.payload_bitrate > REFERENCE_BITRATE]
    shortlist = []
    for profile in pool:
        need, conf = threshold_db(lambda pl, p=profile: mfsk.modulate(pl, p),
                                  lambda a, p=profile: mfsk.demodulate(a, p),
                                  profile, profile.max_payload, args.trials, rng, args.tilt)
        passes = need is not None and need <= bar
        shown = f"{need:.0f} dB" if need is not None else "never"
        print(f"{'PASS' if passes else '    '} {mfsk.describe(profile)} "
              f"-> needs {shown:>6} conf={conf:.3f}")
        if passes:
            shortlist.append((profile, need))

    print(f"\n{len(shortlist)} of {len(pool)} candidates clear the bar.")
    for profile, need in shortlist:
        print(f"  {profile.name:<22} {profile.payload_bitrate:>6.1f} bits/s "
              f"({profile.payload_bitrate / REFERENCE_BITRATE:.2f}x) at {need:.0f} dB")
    print("\nSoftware only: AWGN is the easy half of this channel, and group delay "
          "is not modelled. sweep_mfsk.py decides.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
