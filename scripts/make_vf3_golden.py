"""Regenerate the VF3 capture-replay golden file.

The goldens pin `whale.modes.vf3.demodulate` against the recorded on-air
captures under `experiments/vf3/results/captures/`.  They exist so the DSP
kernel extraction can be proved bit-for-bit behaviour-preserving on real
signals rather than only on synthetic ones.

Only the DQPSK-era captures are usable: the `probe/` and `final_both_3/`
sets were recorded against the earlier coherent-payload VF3 and no longer
decode under the shipped differential module.

Run only to re-baseline deliberately:

    python scripts/make_vf3_golden.py
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import numpy as np

from whale.modes import vf3

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "experiments" / "vf3" / "results" / "captures"
GOLDEN = ROOT / "tests" / "data" / "vf3_capture_golden.json"

# The DQPSK-era capture sets; see the module docstring.
CAPTURE_SETS = ("final_dqpsk_both_3", "probe_dqpsk")

# Result keys pinned as exact array digests.  Between them these cover every
# stage the extraction touches: acquisition, timing regression, the header
# channel fit, the soft-bit mapper and the Viterbi input.
ARRAY_KEYS = ("carrier_snr_db", "symbol_evm_db", "raw_payload_bits",
              "soft_payload_bits", "channel", "interference")

# ...and the scalars, which are cheap to read in a diff when a digest moves.
SCALAR_KEYS = ("confidence", "start_index", "sync_end_index", "end_index",
               "present_carriers", "timing_drift_samples",
               "timing_confidence", "clock_offset_ppm",
               "head_cores_received", "head_match", "decoded_length",
               "crc_ok", "fec_tail_ok")


def digest(array: np.ndarray) -> str:
    """A stable content hash of an array's exact bytes."""
    array = np.ascontiguousarray(array)
    return hashlib.sha256(
        f"{array.dtype.str}{array.shape}".encode() + array.tobytes()
    ).hexdigest()


def capture_paths() -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for name in CAPTURE_SETS:
        paths.extend(sorted((CAPTURES / name).glob("*.npy")))
    return paths


def summarise(path: pathlib.Path) -> dict:
    audio = np.load(path)
    expected = (path.with_suffix(".bin")).read_bytes()
    result = vf3.demodulate(audio)
    if result["payload"] != expected:
        raise SystemExit(f"{path} does not decode to its reference payload")
    entry: dict = {
        "payload_sha256": hashlib.sha256(expected).hexdigest(),
        "payload_bytes": len(expected),
        "arrays": {key: digest(np.asarray(result[key])) for key in ARRAY_KEYS},
    }
    for key in SCALAR_KEYS:
        value = result[key]
        entry[key] = bool(value) if isinstance(value, (bool, np.bool_)) else (
            int(value) if isinstance(value, (int, np.integer))
            else float(value))
    return entry


def main() -> None:
    paths = capture_paths()
    if not paths:
        raise SystemExit(f"no captures found under {CAPTURES}")
    golden = {
        str(path.relative_to(CAPTURES)).replace("\\", "/"): summarise(path)
        for path in paths
    }
    GOLDEN.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN} ({len(golden)} captures)")


if __name__ == "__main__":
    main()
