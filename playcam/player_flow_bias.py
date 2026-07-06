#!/usr/bin/env python3
"""Player-flow average-direction-of-travel signal (playcam).

Design origin: chat discussion 6 July 2026 ("Trim offset mapping for video
frame extraction"). Johnson's spec: players generally move in the direction
of play/the ball, so a sustained multi-player *direction* of travel (not
position) is a useful bias for the camera when ball evidence (MOG2) is weak
or absent -- distinct from the existing `stable_group_yaw_pitch()` in
play_location.py, which averages stable tracks' current *position*, not
their direction of movement.

This is a standalone, additive script. It consumes `play_location.jsonl`
(already emitted by play_location.py -- per-timestamp player records with
`track_id`, `yaw`, `vel_deg_per_sec`) and does not modify play_location.py,
mog2_detector.py, mog2_blob_filter.py, wide_safety_camera.py, or any frozen
file. It requires no new capture/detection and no paid compute -- direction
is derived from consecutive JSONL samples of tracks that already persist
across frames via `track_id`.

Method per timestamp t_i (i>0), compared against the previous sample t_{i-1}:
  1. For every track present in both samples, compute circular delta_yaw.
  2. Keep only tracks with |vel_deg_per_sec| >= --min-moving-vel at t_i --
     this is what excludes a stationary/near-stationary keeper or player
     "for free", per Johnson's call in the design chat (no explicit keeper
     detection needed).
  3. Split moving tracks into leftward (delta_yaw < 0) and rightward
     (delta_yaw > 0) groups; the majority side is the instantaneous flow
     direction IF it has >= --min-agreeing-tracks members.
  4. A "confirmed" flow bias requires the same majority direction to hold
     for >= --min-sustain-samples consecutive timestamps (Johnson: sustained
     over "maybe 1-2 seconds"; at 2Hz sampling that is --min-sustain-samples=3
     by default, i.e. ~1.0s of agreement after the trigger sample).

Output CSV columns: timestamp, direction (+1/-1/0), agreeing_tracks,
moving_tracks, mean_delta_yaw_deg, sustained (0/1), sustained_run_len.

This produces a *direction/confidence* signal only -- it deliberately does
not compute an absolute target_yaw or combine with MOG2/centroid. That
fusion (ball-lock > flow-hold > centroid, per the design chat's 4-tier
policy) is a separate next step once this signal itself is validated.
"""
import argparse
import csv
import json
from pathlib import Path


def circular_delta(a, b):
    """Shortest signed angular difference a-b, in degrees, wrap-safe."""
    return ((a - b + 180) % 360) - 180


def load_records(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def compute_flow_signal(records, min_moving_vel, min_agreeing_tracks, min_sustain_samples):
    # index players by track_id per record for fast consecutive-sample lookup
    by_track_prev = {}
    rows = []
    sustained_run = 0
    prev_direction = 0

    for rec in records:
        cur_by_track = {p["track_id"]: p for p in rec["players"] if p.get("track_id") is not None}
        left = []
        right = []
        for tid, p in cur_by_track.items():
            prev_p = by_track_prev.get(tid)
            if prev_p is None:
                continue
            if abs(p.get("vel_deg_per_sec", 0.0)) < min_moving_vel:
                continue
            dyaw = circular_delta(p["yaw"], prev_p["yaw"])
            if dyaw < 0:
                left.append(dyaw)
            elif dyaw > 0:
                right.append(dyaw)

        moving_tracks = len(left) + len(right)
        if len(left) >= min_agreeing_tracks and len(left) > len(right):
            direction = -1
            agreeing = left
        elif len(right) >= min_agreeing_tracks and len(right) > len(left):
            direction = 1
            agreeing = right
        else:
            direction = 0
            agreeing = []

        if direction != 0 and direction == prev_direction:
            sustained_run += 1
        elif direction != 0:
            sustained_run = 1
        else:
            sustained_run = 0
        prev_direction = direction

        mean_dyaw = round(sum(agreeing) / len(agreeing), 3) if agreeing else 0.0
        sustained = 1 if (direction != 0 and sustained_run >= min_sustain_samples) else 0

        rows.append(
            {
                "timestamp": rec["timestamp"],
                "direction": direction,
                "agreeing_tracks": len(agreeing),
                "moving_tracks": moving_tracks,
                "mean_delta_yaw_deg": mean_dyaw,
                "sustained": sustained,
                "sustained_run_len": sustained_run,
            }
        )
        by_track_prev = cur_by_track

    return rows


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="play_location.jsonl from play_location.py")
    p.add_argument("--output", default="player_flow_signal.csv")
    p.add_argument("--min-moving-vel", type=float, default=2.0,
                    help="deg/sec; tracks slower than this are excluded (median observed ~2.17)")
    p.add_argument("--min-agreeing-tracks", type=int, default=3,
                    help="minimum tracks agreeing on direction to count as instantaneous flow")
    p.add_argument("--min-sustain-samples", type=int, default=3,
                    help="consecutive same-direction samples required to mark 'sustained' (at 2Hz, 3=~1.0s after trigger)")
    return p.parse_args()


def main():
    args = parse_args()
    records = load_records(args.input)
    rows = compute_flow_signal(
        records, args.min_moving_vel, args.min_agreeing_tracks, args.min_sustain_samples
    )
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp", "direction", "agreeing_tracks", "moving_tracks",
                "mean_delta_yaw_deg", "sustained", "sustained_run_len",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    n_sustained = sum(r["sustained"] for r in rows)
    print(f"Wrote {args.output}: {len(rows)} rows, {n_sustained} sustained-flow samples")


if __name__ == "__main__":
    main()
