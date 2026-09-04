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

from whale import rx_audio
from whale.modes import vf3

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "experiments" / "vf3" / "results" / "captures"
GOLDEN = ROOT / "tests" / "data" / "vf3_capture_golden.json"

# The DQPSK-era capture sets; see the module docstring.
CAPTURE_SETS = ("final_dqpsk_both_3", "probe_dqpsk")

# Result keys pinned as exact array digests, and those pinned as actual
# values compared with a tolerance.  Between them these cover every stage the
# extraction touches: acquisition, timing regression, the header channel fit,
# the soft-bit mapper and the Viterbi input.
#
# Only `raw_payload_bits` -- the hard-decision Viterbi input -- is pinned
# bit-exact: it is thresholded, so it is stable across LAPACK backends. The
# rest (carrier_snr_db, symbol_evm_db, soft_payload_bits, channel,
# interference) all derive from `whale.dsp.equalize.fit_header`'s per-carrier
# `np.linalg.lstsq`, whose last few bits differ between LAPACK
# implementations (e.g. Apple's Accelerate vs. OpenBLAS) even for identical
# input -- confirmed directly by running the same fit under both. Pinning
# those bit-exact made the suite fail on a different machine rather than on
# a real regression, so they are pinned as values and compared with
# `FLOAT_TOLERANCE` instead, the same way the SCALAR_KEYS below already are.
EXACT_ARRAY_KEYS = ("raw_payload_bits",)
TOLERANCE_ARRAY_KEYS = ("carrier_snr_db", "symbol_evm_db", "soft_payload_bits",
                        "channel", "interference")

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


def serialize(array: np.ndarray) -> dict:
    """JSON-round-trippable snapshot of a float or complex array's values."""
    array = np.asarray(array)
    if np.iscomplexobj(array):
        return {"real": array.real.tolist(), "imag": array.imag.tolist()}
    return {"real": array.tolist(), "imag": None}


def deserialize(entry: dict) -> np.ndarray:
    real = np.asarray(entry["real"], dtype=np.float64)
    if entry["imag"] is None:
        return real
    return real + 1j * np.asarray(entry["imag"], dtype=np.float64)


def capture_paths() -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for name in CAPTURE_SETS:
        paths.extend(sorted((CAPTURES / name).glob("*.npy")))
    return paths


def summarise(path: pathlib.Path) -> dict:
    audio = np.load(path)
    expected = (path.with_suffix(".bin")).read_bytes()
    result = vf3.demodulate(rx_audio.downsample(audio))
    if result["payload"] != expected:
        raise SystemExit(f"{path} does not decode to its reference payload")
    entry: dict = {
        "payload_sha256": hashlib.sha256(expected).hexdigest(),
        "payload_bytes": len(expected),
        "arrays": {
            **{key: digest(np.asarray(result[key])) for key in EXACT_ARRAY_KEYS},
            **{key: serialize(result[key]) for key in TOLERANCE_ARRAY_KEYS},
        },
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
