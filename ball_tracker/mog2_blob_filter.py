#!/usr/bin/env python3
"""Post-process geometric/motion filter for raw MOG2 blob candidates.

Consumes the frame_candidates JSON produced by mog2_detector.py and flags
candidates that are unlikely to be the ball based on two data-validated
criteria (see docs/ai-project-state.md, "Ball tracker - MOG2-primary track"):

1. static_suspect: the candidate sits in effectively the same pixel location
   (within --static-radius-px) for at least --static-min-frames consecutive
   frames. A blob held still for >0.5s cannot be a moving ball. This is the
   exact failure mode already identified in the playcam fusion work: the
   yaw~-40.4 deg blob at t=57-58s that got treated as a confident MOG2 lock
   in fusion v1/v2 despite not moving.
2. wide_flat_suspect: bounding-box width/height ratio exceeds
   --max-aspect-ratio. mog2_detector.py's contour_candidate() already
   rejects tall/thin blobs (h/w > max_aspect_ratio) but not wide/flat ones
   (w/h > max_aspect_ratio). This closes that gap symmetrically.

This script does not modify mog2_detector.py, stage1_candidate_gen.py,
run_tracker.py, or any other frozen/production file. It is an additive,
standalone step that runs between raw MOG2 detection and any downstream
fusion-CSV builder, consuming only mog2_detector.py's JSON output.

Validated against real data (artifact 8093056660, run 28745136405,
equirect_full.mp4 t=0-120s, 3596 frames, 2967 raw candidate-detections,
frame_width=4032):
  - static_suspect (radius=5px, min_frames=15, ~0.5s at ~30fps) flags 10
    distinct held-still chains, including the known t=57.16-57.83s false
    lock, at roughly 9% of all raw candidate-detections.
  - wide_flat_suspect (aspect>2.5) flags 3/2967 candidates.
Both checks are independent; a candidate can be flagged by neither, either,
or both. Thresholds were chosen by inspecting the real displacement/chain
distribution for this clip, not assumed a priori -- see chat handoff notes
for the full derivation before changing them.
"""
import argparse
import json
import math
from pathlib import Path


def xdist(x1, x2, frame_width):
    """Horizontal distance accounting for equirectangular wraparound."""
    dx = abs(x1 - x2)
    return min(dx, frame_width - dx)


def find_static_suspects(frame_candidates, frame_width, radius_px, min_frames):
    """Greedy tight-radius frame-to-frame association, purpose-built to find
    near-motionless chains -- not a general-purpose tracker. Because the
    radius is small, a genuinely moving ball simply fails to link and starts
    a new one-frame chain instead of being falsely flagged; this only ever
    catches things that truly held still for a sustained period.
    """
    frames = sorted(int(k) for k in frame_candidates.keys())
    if not frames:
        return set()
    max_f = frames[-1]

    tracks = {}
    next_id = 0
    active = []  # list of (track_id, cx, cy)
    for f in range(0, max_f + 1):
        cands = frame_candidates.get(str(f), [])
        pts = [(c["x"] + c["w"] / 2.0, c["y"] + c["h"] / 2.0, c) for c in cands]
        used = set()
        new_active = []
        for tid, pcx, pcy in active:
            best, best_d = None, None
            for i, (cx, cy, c) in enumerate(pts):
                if i in used:
                    continue
                d = math.hypot(xdist(cx, pcx, frame_width), abs(cy - pcy))
                if d <= radius_px and (best_d is None or d < best_d):
                    best_d, best = d, i
            if best is not None:
                used.add(best)
                cx, cy, c = pts[best]
                tracks[tid].append((f, cx, cy, c))
                new_active.append((tid, cx, cy))
        for i, (cx, cy, c) in enumerate(pts):
            if i not in used:
                tid = next_id
                next_id += 1
                tracks[tid] = [(f, cx, cy, c)]
                new_active.append((tid, cx, cy))
        active = new_active

    flagged = set()
    for chain in tracks.values():
        if len(chain) >= min_frames:
            for (f, cx, cy, c) in chain:
                flagged.add((f, c["x"], c["y"], c["w"], c["h"]))
    return flagged


def filter_candidates(data, frame_width, radius_px, min_frames, max_aspect_ratio):
    frame_candidates = data["frame_candidates"]
    static_keys = find_static_suspects(frame_candidates, frame_width, radius_px, min_frames)

    out = {}
    stats = {"total": 0, "static_suspect": 0, "wide_flat_suspect": 0, "both": 0}
    for f, cands in frame_candidates.items():
        new_cands = []
        for c in cands:
            stats["total"] += 1
            key = (int(f), c["x"], c["y"], c["w"], c["h"])
            is_static = key in static_keys
            is_wide_flat = c["h"] > 0 and (c["w"] / c["h"]) > max_aspect_ratio
            if is_static:
                stats["static_suspect"] += 1
            if is_wide_flat:
                stats["wide_flat_suspect"] += 1
            if is_static and is_wide_flat:
                stats["both"] += 1
            new_c = dict(c)
            new_c["static_suspect"] = is_static
            new_c["wide_flat_suspect"] = is_wide_flat
            new_cands.append(new_c)
        out[f] = new_cands
    return {"frame_candidates": out}, stats


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("input", help="Raw mog2_candidates.json from mog2_detector.py")
    p.add_argument("--output", default="mog2_candidates_filtered.json")
    p.add_argument(
        "--frame-width", type=int, default=4032, help="Source video width (for equirect x-wrap)"
    )
    p.add_argument("--static-radius-px", type=float, default=5.0)
    p.add_argument(
        "--static-min-frames", type=int, default=15, help="Consecutive frames, ~0.5s at ~30fps"
    )
    p.add_argument("--max-aspect-ratio", type=float, default=2.5)
    p.add_argument(
        "--drop-suspects",
        action="store_true",
        help="Remove flagged candidates from the output entirely instead of just tagging them",
    )
    return p.parse_args()


def main():
    args = parse_args()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    filtered, stats = filter_candidates(
        data, args.frame_width, args.static_radius_px, args.static_min_frames, args.max_aspect_ratio
    )
    if args.drop_suspects:
        for f, cands in filtered["frame_candidates"].items():
            filtered["frame_candidates"][f] = [
                c for c in cands if not (c["static_suspect"] or c["wide_flat_suspect"])
            ]
    Path(args.output).write_text(json.dumps(filtered, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {args.output}: {stats['total']} candidates, "
        f"{stats['static_suspect']} static_suspect, "
        f"{stats['wide_flat_suspect']} wide_flat_suspect, "
        f"{stats['both']} both"
    )


if __name__ == "__main__":
    main()
