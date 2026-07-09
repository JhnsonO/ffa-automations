"""Headless flatcam venue polygon writer in undistorted pixel space."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from undistort import load_profile


def parse_points(values: list[str]) -> list[list[int]]:
    pts = []
    for value in values:
        x_s, y_s = value.split(",", 1)
        pts.append([int(round(float(x_s))), int(round(float(y_s)))])
    if len(pts) < 3:
        raise ValueError("venue polygon needs at least three points")
    return pts


def points_from_file(path: str | Path) -> list[list[int]]:
    values = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                values.append(line.replace(" ", ","))
    return parse_points(values)


def write_venue(name: str, profile_name: str, polygon: list[list[int]], out_dir: str | Path = "flatcam/venues") -> Path:
    out_path = Path(out_dir) / f"{name}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"profile_name": profile_name, "polygon": polygon, "created": datetime.now(timezone.utc).isoformat()}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--input", required=True, help="frame/video path, retained for CLI symmetry and auditability")
    ap.add_argument("--name", required=True)
    ap.add_argument("--point", action="append", default=[], help="x,y polygon vertex in undistorted pixel space")
    ap.add_argument("--points-file")
    args = ap.parse_args()
    load_profile(args.profile)
    polygon = points_from_file(args.points_file) if args.points_file else parse_points(args.point)
    print(write_venue(args.name, args.profile, polygon))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
