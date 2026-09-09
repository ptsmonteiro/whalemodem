"""Render the sweep JSON documents as the delivery tables RESULTS.md quotes.

    python experiments/hc1p_pilots/summarise.py results_*.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def table(document: dict) -> str:
    points = document["points_db"]
    arms, seen = [], set()
    for row in document["results"]:
        if row["arm"] not in seen:
            seen.add(row["arm"])
            arms.append(row["arm"])
    index = {(row["arm"], row["point_db"]): row for row in document["results"]}

    title = (document["watterson_preset"] or "AWGN")
    lines = [f"### {title}",
             "",
             f"{document['trials']} trials per point, delivered frames per 100."
             f"  Payload is the arm's full capacity.",
             "",
             "| Arm | Payload | " + " | ".join(f"{p:g} dB" for p in points) + " |",
             "| --- | ---: | " + " | ".join("---:" for _ in points) + " |"]
    for arm in arms:
        rows = [index[(arm, point)] for point in points]
        payload = f"{rows[0]['max_payload_bytes']} B"
        cells = " | ".join(str(row["delivered"]) for row in rows)
        lines.append(f"| {arm} | {payload} | {cells} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("documents", type=Path, nargs="+")
    args = ap.parse_args(argv)
    for path in args.documents:
        document = json.loads(path.read_text())
        print(table(document))
        print()
        for row in document.get("drift", []):
            drift = row["phase_drift_rad"]
            if drift["median"] is not None:
                print(f"    {row['arm']}: pilot phase drift median "
                      f"{drift['median']:.2f} rad, max {drift['max']:.2f} rad "
                      f"at {row['point_db']:g} dB over {drift['samples']} frames")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
