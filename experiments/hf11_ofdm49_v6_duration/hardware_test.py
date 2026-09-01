"""Real-hardware trial runner for hf11_ofdm49_v6_duration.

This experiment pushes FRAME DURATION (payload size) on top of hf10's
exact PHY: `experiments/hf10_ofdm49_v6/ofdm49_v6.py` is imported read-only,
unmodified -- 49-bin OFDM, 16-QAM, rate-3/4 LDPC. Nothing about the PHY
changes here; every "step" in this experiment is a different
--packet-bytes/--pilot-interval combination on the same code hf10
qualified at 176 B payload (12/13 decoded, zero residual bit errors).

This file is a thin copy of experiments/hf10_ofdm49_v6/hardware_test.py
(itself read-only, unmodified) with the output directory/label defaults
pointed at this experiment's own logs, per this project's convention of a
fresh, disposable test-harness copy per experiment while sharing the PHY
module read-only when it is not itself under test.

Run, e.g. hf10's own winning baseline (re-confirmation step):
    python experiments/hf11_ofdm49_v6_duration/hardware_test.py \
        --bps 4 --fec-rate 3/4 --packet-bytes 182 --pilot-interval 20 --trials 3

SAFETY: IC-705 must never transmit. --direction is hardcoded to "ab" (no
"ba"/"both" choice exists) and only transport_a (ic7300 by default) is ever
keyed via .send(). There is deliberately no code path here that calls
transport_b.send().
"""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bench
from experiments.hf10_ofdm49_v6 import ofdm49_v6 as ofdm49  # noqa: F401  (re-exported for callers)
from experiments.hf10_ofdm49_v6.hardware_test import main as _hf10_main

DEFAULT_OUTPUT_ROOT = Path("logs") / "mode_qualification" / "hf-ssb" / "hf11"


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not any(a == "--output-dir" for a in argv):
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        argv = argv + ["--output-dir", str(DEFAULT_OUTPUT_ROOT / stamp)]
    return _hf10_main(argv, pair_factory=bench.radio_pair)


if __name__ == "__main__":
    sys.exit(main())
