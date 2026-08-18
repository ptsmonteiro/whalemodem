"""Why did that frame fail? Symbol-level post-mortem for one MFSK candidate.

sweep_mfsk.py answers "does it decode", which is the question that decides the
mode. This answers the follow-up, and it exists because the repo has a standing
unexplained failure with exactly the shape this experiment keeps reproducing:
sync locks confidently, and the frame fails its CRC anyway. From the decoder's
output alone that looks the same whatever caused it.

The payload is known here, so the transmitted symbol sequence is known, and the
received symbols can be lined up against it one for one. Three signatures
separate the plausible causes, and they do not look alike:

  - **Sub-orthogonal leakage.** Errors concentrate on tone pairs that are
    adjacent in frequency, because that is where the box integrator's sinc
    skirt puts its energy -- a neighbour one spacing away contributes
    |sinc(ratio)| of its amplitude, which is 0.19 at ratio 0.833 but 0.50 at
    0.60. Read the confusion matrix: leakage lives on the first off-diagonal.

  - **Sample-clock offset.** Errors are rare at the start of the frame and
    common at the end, because symbol points are laid on a rigid grid from the
    sync peak with no timing recovery, so timing error accumulates and the
    frame dies when it reaches half a symbol. Read the per-decile error
    profile: drift ramps, and nothing else does.

  - **Noise, or a frequency response that suits one tone worse than the
    others.** Errors spread roughly evenly through the frame, and the confusion
    matrix has an entire row or column that is worse than the rest -- one tone
    the chain does not carry. Not a ramp, not confined to the off-diagonal.

The per-tone received energies are printed for the same reason: a tone the
audio chain rolls off shows up there directly, before any of it reaches a
symbol decision.

Run: python experiments/mfsk/diagnose_mfsk.py --profile 4fsk_800bd_x0.6
     python experiments/mfsk/diagnose_mfsk.py --profile 4fsk_800bd_x0.6 --trials 3
     python experiments/mfsk/diagnose_mfsk.py --profile 4fsk_700bd_x0.7 --direction ht
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

import mfsk
from whale.transport import RadioTransport

CAPTURE_TAIL = 0.8


def received_symbols(audio, profile):
    """The symbol decisions the decoder would make, plus where it synced.

    Reimplements demodulate()'s inner path rather than calling it, because
    demodulate() deliberately returns only a payload -- the whole point here is
    the symbols it threw away on the way to failing a CRC.
    """
    env = mfsk.tone_envelopes(audio, profile)
    template, lead = mfsk._sync_template(profile)
    if env.shape[1] < template.shape[1]:
        return None
    ncc = mfsk._normalised_correlation(env, template)
    peaks = mfsk._sync_peaks(ncc, profile.confidence_threshold,
                             profile.sps * mfsk._MIN_PEAK_SEPARATION_SYMBOLS)
    if not peaks:
        return None

    sps = profile.sps
    peak = peaks[0]
    preamble_start = peak - lead
    data_start = preamble_start + sps * mfsk.PREAMBLE_SYMBOLS
    available = (env.shape[1] - data_start) // sps
    if preamble_start < 0 or available <= 0:
        return None

    if profile.train_on_preamble:
        gains = mfsk._training_gains(env, preamble_start, profile)
        if gains is not None:
            env = env / gains[:, None]

    points = data_start + sps * np.arange(available) + (sps - 1)
    return {
        "symbols": np.argmax(env[:, points], axis=0),
        "confidence": float(ncc[peak]),
        "tone_energy": env[:, points].mean(axis=1),
        "preamble_start": preamble_start,
    }


def analyse(sent, got, profile):
    n = min(len(sent), len(got))
    sent, got = sent[:n], got[:n]
    wrong = sent != got
    errors = int(wrong.sum())

    print(f"  symbols compared: {n}, wrong: {errors} "
          f"({errors / max(n, 1) * 100:.2f}% symbol error rate)")
    if errors == 0:
        return errors

    # Where in the frame. A ramp is clock drift; flat is noise.
    deciles = np.array_split(wrong, 10)
    profile_line = " ".join(f"{d.mean()*100:4.1f}" for d in deciles)
    print(f"  errors by decile of frame (%):  {profile_line}")
    first, last = int(np.argmax(wrong)), int(n - 1 - np.argmax(wrong[::-1]))
    print(f"  first error at symbol {first} ({first/n*100:.0f}% in), "
          f"last at {last} ({last/n*100:.0f}% in)")

    # Which tones. Off-diagonal-adjacent is leakage; a bad row/column is a
    # tone the chain does not carry.
    confusion = np.zeros((profile.m, profile.m), dtype=int)
    for s, g in zip(sent[wrong], got[wrong]):
        confusion[s, g] += 1
    print("  confusion (sent -> got), errors only:")
    print("        " + "".join(f"{g:>6}" for g in range(profile.m)))
    for s in range(profile.m):
        print(f"    {s} |" + "".join(f"{confusion[s, g]:>6}" for g in range(profile.m)))

    adjacent = sum(confusion[s, g] for s in range(profile.m) for g in range(profile.m)
                   if abs(s - g) == 1)
    print(f"  adjacent-tone confusions: {adjacent}/{errors} "
          f"({adjacent / errors * 100:.0f}%) -- high means sub-orthogonal leakage")
    return errors


def run(tx, rx, profile, trials, label):
    print(f"\n=== {label}: {mfsk.describe(profile)}")
    rng = np.random.default_rng()
    for i in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        payload = rng.integers(0, 256, profile.max_payload, dtype=np.uint8).tobytes()
        sent_all = mfsk.frame_symbol_sequence(payload, profile)
        head = len(mfsk._pad_symbols(profile, mfsk.HEAD_PAD_SECONDS))
        sent_data = sent_all[head + mfsk.PREAMBLE_SYMBOLS:
                             len(sent_all) - len(mfsk._pad_symbols(profile,
                                                                   mfsk.TAIL_PAD_SECONDS))]

        keyed = tx.send(mfsk.modulate(payload, profile))
        time.sleep(CAPTURE_TAIL)
        captured = rx.snapshot_rx()

        result = mfsk.demodulate(captured, profile)
        decoded = result.get("payload") == payload
        print(f"\n  trial {i}/{trials}: keyed={keyed:.2f}s decoded={decoded} "
              f"conf={result.get('confidence', 0.0):.3f}")

        detail = received_symbols(captured, profile)
        if detail is None:
            print("  no sync -- nothing to compare")
            continue
        energies = " ".join(f"{f:.0f}Hz={e:.2f}" for f, e in
                            zip(profile.tones, detail["tone_energy"]))
        print(f"  mean received energy per tone: {energies}")
        analyse(np.asarray(sent_data), np.asarray(detail["symbols"]), profile)
        time.sleep(0.6)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="candidate name, e.g. 4fsk_800bd_x0.6")
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--direction", choices=("both", "ic705", "ht"), default="both",
                    help="which station transmits (default: both, one after the other)")
    ap.add_argument("--no-training", action="store_true")
    args = ap.parse_args()

    matches = [p for p in mfsk.candidates(train_on_preamble=not args.no_training)
               if p.name == args.profile]
    if not matches:
        raise SystemExit(f"unknown candidate {args.profile!r}")
    profile = matches[0]

    print("opening radios...")
    t_a = RadioTransport("ic705")
    t_b = RadioTransport("ht")
    try:
        t_a.start_receiving()
        t_b.start_receiving()
        print("warming up 2s...")
        time.sleep(2)
        if args.direction in ("both", "ic705"):
            run(t_a, t_b, profile, args.trials, "ic705 -> ht")
        if args.direction in ("both", "ht"):
            run(t_b, t_a, profile, args.trials, "ht -> ic705")
    finally:
        t_a.close()
        t_b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
