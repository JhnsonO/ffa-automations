"""Render a 16:9 follow-camera segment for flat single-lens footage."""
from __future__ import annotations

import argparse, csv, json
from pathlib import Path

import cv2

from action_centroid import ActionCentroid
from follow_camera_flat import FollowCameraFlat
from undistort import load_profile, load_profiles, undistort_frame


def crop_frame(frame, state, out_w=1280, out_h=720):
    h, w = frame.shape[:2]
    cw, ch = int(round(state.crop_w)), int(round(state.crop_h))
    x1 = max(0, min(w - cw, int(round(state.cx - cw / 2))))
    y1 = max(0, min(h - ch, int(round(state.cy - ch / 2))))
    return cv2.resize(frame[y1:y1+ch, x1:x1+cw], (out_w, out_h), interpolation=cv2.INTER_LINEAR)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--venue", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--csv-out")
    args = ap.parse_args()
    try:
        profile = load_profile(args.profile)
    except KeyError:
        profile = load_profiles(Path("flatcam/test_lens_profiles.json"))[args.profile]
    venue = json.load(open(args.venue, "r", encoding="utf-8"))
    cap = cv2.VideoCapture(args.input)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    ok, frame = cap.read()
    if not ok: raise RuntimeError("empty input")
    frame = undistort_frame(frame, profile)
    h, w = frame.shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(args.output, fourcc, fps, (1280, 720))
    detector = ActionCentroid(frame.shape, venue["polygon"])
    fsm = FollowCameraFlat(w, h, fps)
    csv_fh = open(args.csv_out, "w", newline="", encoding="utf-8") if args.csv_out else None
    csv_writer = csv.DictWriter(csv_fh, fieldnames=["frame_idx","mode","cx","cy","crop_w","crop_h","score"]) if csv_fh else None
    if csv_writer: csv_writer.writeheader()
    idx = 0
    while ok:
        if idx > 0: frame = undistort_frame(frame, profile)
        stats = detector.process(frame)
        state = fsm.update(idx, stats.cx, stats.cy, stats.concentration_score)
        writer.write(crop_frame(frame, state))
        if csv_writer: csv_writer.writerow(state.__dict__)
        ok, frame = cap.read(); idx += 1
    if csv_fh: csv_fh.close()
    writer.release(); cap.release()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
