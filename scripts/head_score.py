"""How close does the OFDM settling head come to being mistaken for a sync?

The four OFDM modes (hf6, hf7, hf8, hf9) prepend a settling head built from
their own modulation. `whale/streaming.py` raises a candidate whenever an
acquisition window scores at or above 0.12, so a head that scores near 0.12
costs a failed decode attempt every keying. It can never cost more than
that -- the real preamble scores ~1.0 -- but the margin is worth knowing,
and it shrinks or grows with the head's length because a longer head is
more search windows and the in-head score is an order statistic.

This script measures that margin. For each mode and each head length it
generates the keying, adds noise at a spread of SNRs, and searches for the
preamble *inside the head only*, reporting the peak score over all draws
(the number that matters -- one unlucky draw is one false candidate) beside
the mean and spread.

    python scripts/head_score.py
    python scripts/head_score.py --heads 0.6 0.2 --draws 8

Software only -- no radios, no sound cards.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

import numpy as np

from whale import framing
from whale.phy.ofdm49 import DESIGN_RATE
from whale.modes.hf6_mode import HF6, HF6_PHY
from whale.modes.hf7_mode import HF7, HF7_PHY
from whale.modes.hf8_mode import HF8, HF8_PHY
from whale.modes.hf9_mode import HF9, HF9_PHY

PHYS = {"hf6": (HF6, HF6_PHY), "hf7": (HF7, HF7_PHY),
        "hf8": (HF8, HF8_PHY), "hf9": (HF9, HF9_PHY)}

#: streaming.py's candidate gate. Duplicated here rather than imported
#: because it is a bare literal there; a named constant for it is wanted.
GATE = 0.12

DEFAULT_SNRS = (0.0, 5.0, 10.0, 20.0, 40.0)


def in_head_peaks(phy, payload, snr_db, rng):
    """Best acquisition score at a false start inside this keying's head.

    Two regions, because only one of them is a hazard:

    `disjoint` -- starts at which a whole preamble template fits inside the
    head, touching no real preamble sample. A candidate raised here is a
    genuinely false acquisition the streaming receiver must then discard.
    Once the head is shorter than the preamble this region is empty and the
    hazard does not exist at all; the score is reported as nan.

    `overlapping` -- every start before the real one, including those whose
    template runs into the real preamble. These score high by construction,
    but streaming.py merges any candidate within a preamble of another and
    keeps the stronger, so the true alignment absorbs them. Reported for
    context, not as a margin.
    """
    tx = np.asarray(phy.modulate(payload), dtype=np.float64)[::4]
    head = phy.n_settling_head_symbols * phy.symbol_len
    preamble = phy.n_preamble_symbols * phy.symbol_len
    noise = rng.standard_normal(len(tx))
    scale = float(np.std(tx)) * 10.0 ** (-snr_db / 20.0)
    x = tx + noise * scale
    disjoint = float("nan")
    if head > preamble:
        disjoint = float(phy.acquire(x, search_slice=slice(0, head - preamble))[0])
    overlapping = float(phy.acquire(x, search_slice=slice(0, max(head, 1)))[0])
    return disjoint, overlapping


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--modes", nargs="+", default=list(PHYS))
    ap.add_argument("--heads", nargs="+", type=float,
                    default=[0.6, framing.SETTLING_HEAD_SECONDS])
    ap.add_argument("--snrs", nargs="+", type=float, default=list(DEFAULT_SNRS))
    ap.add_argument("--draws", type=int, default=6)
    args = ap.parse_args(argv)

    print(f"in-head acquisition score, gate = {GATE}")
    print(f"{'mode':5} {'head':>6} {'pre':>6} | "
          f"{'peak':>7} {'mean':>7} {'sd':>7} {'margin':>7} {'worst':>6} | "
          f"{'overlap':>7}")
    for name in args.modes:
        mode, base = PHYS[name]
        payload = bytes((i * 37 + 11) & 0xFF
                        for i in range(mode.chunk_size + framing.AIR_HEADER_BYTES))
        for head_seconds in args.heads:
            phy = replace(base, head_seconds=head_seconds)
            disjoint, overlapping, worst_snr, worst = [], [], None, -1.0
            for snr_db in args.snrs:
                rng = np.random.default_rng(
                    abs(hash((name, head_seconds, snr_db))) % (2 ** 32))
                for _ in range(args.draws):
                    d, o = in_head_peaks(phy, payload, snr_db, rng)
                    disjoint.append(d)
                    overlapping.append(o)
                    if d == d and d > worst:
                        worst, worst_snr = d, snr_db
            a, b = np.array(disjoint), np.array(overlapping)
            pre = phy.n_preamble_symbols * phy.symbol_len / DESIGN_RATE
            if np.all(np.isnan(a)):
                print(f"{name:5} {phy.settling_head_seconds():6.3f} {pre:6.3f} | "
                      f"{'--':>7} {'--':>7} {'--':>7} {'none':>7} {'':>6} | "
                      f"{b.max():7.4f}   head shorter than preamble: "
                      f"no disjoint false start exists")
                continue
            print(f"{name:5} {phy.settling_head_seconds():6.3f} {pre:6.3f} | "
                  f"{a.max():7.4f} {a.mean():7.4f} {a.std():7.4f} "
                  f"{GATE - a.max():+7.4f} {worst_snr:5g}d | {b.max():7.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
