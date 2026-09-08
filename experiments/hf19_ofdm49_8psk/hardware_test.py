"""Real-hardware trial runner for hf19_ofdm49_8psk (the HF8 candidate).

`sweep.py` in this directory chose HF8's configuration entirely in
simulation. This file is how that choice meets a radio, which is the thing
`docs/SIMULATION_RADIO_DIFFERENCES.md` says the simulation cannot predict --
on this very PHY family, a 16-QAM configuration that passed simulation was
1/5 on the air.

hf10's PHY (`whale/phy/ofdm49.py`) and its hardware
harness are imported read-only and unmodified; this file only supplies HF8's
parameters as defaults and points the output at this experiment's own logs,
per the project convention of a disposable harness copy per experiment. It is
a thin copy of `experiments/hf18_ofdm49_vara/hardware_test.py`.

The defaults below ARE `whale/modes/hf8_mode.py`: 49 carriers at 50 Hz
spacing over 300-2700 Hz, 2 ms guard, 8PSK, rate-2/3 LDPC over an
interleaved frame, a full pilot symbol every 10, 270-byte (5-codeword)
frames, and HF7's bench-calibrated drive. Run it with no waveform arguments
at all and it transmits exactly the mode:

    python experiments/hf19_ofdm49_8psk/hardware_test.py --trials 40

The HF7 baseline, through the same path in the same session, is the
comparison that makes the numbers mean anything:

    python experiments/hf19_ofdm49_8psk/hardware_test.py \\
        --bps 5 --fec-rate 3/4 --pilot-interval 20 --packet-bytes 4738 \\
        --trials 40 --label hf7-baseline

SAFETY, inherited unchanged from the hf10 harness: the default direction is
IC-7300 -> IC-705 and opens the IC-705 receive-only, so nothing in the
process can key it. The reverse exists only behind `--direction ba
--allow-ic705-tx`, which opens the IC-7300 receive-only instead.
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
from whale.modes import hf8_mode

DEFAULT_OUTPUT_ROOT = Path("logs") / "mode_qualification" / "hf-ssb" / "hf19"

# Every waveform default here is read from the mode module rather than
# retyped, so this runner cannot drift away from what HF8 actually is.
HF8_DEFAULTS = [
    "--fft-size", str(hf8_mode.FFT_SIZE),
    "--cp-len", str(hf8_mode.CP_LEN),
    "--bps", str(hf8_mode.BITS_PER_SYMBOL),
    "--fec-rate", hf8_mode.FEC_RATE,
    "--pilot-interval", str(hf8_mode.PILOT_INTERVAL),
    "--packet-bytes", str(hf8_mode.PACKET_BYTES),
    "--noise-estimator", hf8_mode.NOISE_ESTIMATOR,
    "--drive-scale", str(hf8_mode.HF8_PHY.drive_scale),
]
if hf8_mode.INTERLEAVE:
    HF8_DEFAULTS.append("--interleave")


def _apply_defaults(argv: list[str]) -> list[str]:
    supplied = {a.split("=", 1)[0] for a in argv if a.startswith("--")}
    prefix: list[str] = []
    i = 0
    while i < len(HF8_DEFAULTS):
        flag = HF8_DEFAULTS[i]
        takes_value = (i + 1 < len(HF8_DEFAULTS)
                       and not HF8_DEFAULTS[i + 1].startswith("--"))
        if flag not in supplied:
            prefix.append(flag)
            if takes_value:
                prefix.append(HF8_DEFAULTS[i + 1])
        i += 2 if takes_value else 1
    return prefix + argv


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv[1:]
    argv = _apply_defaults(argv)
    if not any(a == "--output-dir" for a in argv):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        argv = argv + ["--output-dir", str(DEFAULT_OUTPUT_ROOT / stamp)]
    return _hf10_main(argv, pair_factory=bench.radio_pair)


if __name__ == "__main__":
    sys.exit(main())
