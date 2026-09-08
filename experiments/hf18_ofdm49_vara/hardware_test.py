"""Real-hardware trial runner for hf18_ofdm49_vara.

This experiment revisits hf10's ORIGINAL 49-bin geometry (fft_size=240 at
the 12 kHz design rate -> 50.0 Hz spacing, bins 6..54 = 300-2700 Hz,
centred exactly on 1500 Hz) but sweeps the cyclic prefix down towards
zero, because 49 contiguous carriers filling the SSB passband pins the
useful symbol time at 20 ms: 50 OFDM symbols/s is reachable only with
cp_len=0.  The motivation is that this is the geometry VARA HF uses at
its top speed level, so the arrangement is known to work on real HF
somewhere -- the open question here is how much guard interval this
particular radio pair's SSB filters actually require.

hf10's PHY (whale/phy/ofdm49.py) is imported
read-only, unmodified.  Every step is a --cp-len/--bps/--fec-rate
combination on the code hf10 qualified; no PHY code is written here.

This file is a thin copy of experiments/hf10_ofdm49_v6/hardware_test.py
with the output directory default pointed at this experiment's own logs,
per the project convention of a disposable harness copy per experiment.

Run, e.g. the zero-guard candidate:
    python experiments/hf18_ofdm49_vara/hardware_test.py \
        --fft-size 240 --cp-len 0 --bps 5 --fec-rate 3/4 --interleave \
        --noise-estimator repeat --drive-scale 0.008 \
        --packet-bytes 2394 --trials 5

Direction is A(ic7300) -> B(ic705) as in every hf10/hf11 trial.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bench
from whale.phy import ofdm49 as ofdm49  # noqa: F401
from experiments.hf10_ofdm49_v6.hardware_test import main as _hf10_main

DEFAULT_OUTPUT_ROOT = Path("logs") / "mode_qualification" / "hf-ssb" / "hf18"


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not any(a == "--output-dir" for a in argv):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        argv = argv + ["--output-dir", str(DEFAULT_OUTPUT_ROOT / stamp)]
    return _hf10_main(argv, pair_factory=bench.radio_pair)


if __name__ == "__main__":
    sys.exit(main())
