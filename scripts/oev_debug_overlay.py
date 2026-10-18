#!/usr/bin/env python3
"""OEV-006 debug video: what each cameraman is looking at, on both cameras.

Side-by-side left|right camera view with:
  - the venue pitch outline (green) used by the arms;
  - every ball detection: orange ring = inside our pitch (kept by the
    production field-mode ROI test), grey ring + x = rejected by the outline;
  - person detections: small green box inside the outline, grey outside;
  - TRACKED ball per camera path, as a thick ring with its tracker state:
      cyan    = A0 (ball-first WITH the pitch outline, CPU replay trace)
      magenta = the GPU render you watched (no outline, recorded world)
  - the virtual camera's 16:9 frame for each path (same colours).
Panorama angles are mapped back into each camera image with a per-camera
polynomial fitted on this window's own detections (position -> camera_center).

Usage:
  python3 scripts/oev_debug_overlay.py --events events.jsonl --left L.mp4 --right R.mp4 \
      --roi venue_roi.json --trace-a0 dev_s01_A0.csv --out debug.mp4 [--every 2] [--stills-dir d]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os

os.environ.setdefault("OPENCV_FFMPEG_READ_ATTEMPTS", "16384")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

BALL, PERSON = 32, 0
GREEN, ORANGE, GREY = (60, 220, 60), (0, 150, 255), (150, 150, 150)
CYAN, MAGENTA, WHITE = (255, 220, 0), (220, 60, 255), (255, 255, 255)
FONT = cv2.FONT_HERSHEY_SIMPLEX


def opener(p):
    return gzip.open(p, "rt") if p.endswith(".gz") else open(p)


def pip(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def ball_kept(cx, cy, poly, margin=0.40):
    """Production ball_passes_roi: centre inside, or a point up to `margin`
    straight below it inside (balls above the pitch line)."""
    if len(poly) < 3 or pip(cx, cy, poly):
        return True
    n = math.ceil(margin / 0.02)
    return any(pip(cx, cy + min(i * 0.02, margin), poly) for i in range(1, n + 1))


def person_kept(cx, cy, h, poly):
    return len(poly) < 3 or (pip(cx, cy + h / 2, poly) and pip(cx, cy + h / 4, poly))


def feats(a, b, deg=4):
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    return np.stack([a**i * b**j for i in range(deg + 1) for j in range(deg + 1 - i)], -1)


class InvMap:
    """(yaw, pitch) -> camera (x, y) fractions, per camera."""

    def __init__(self, pairs):
        self.m = {}
        for cam, arr in pairs.items():
            a = np.array(arr, float)
            A = feats(a[:, 2], a[:, 3])
            self.m[cam] = (np.linalg.lstsq(A, a[:, 0], rcond=None)[0], np.linalg.lstsq(A, a[:, 1], rcond=None)[0],
                           (a[:, 2].min() - 0.15, a[:, 2].max() + 0.15))

    def __call__(self, cam, yaw, pitch):
        cx, cy, (lo, hi) = self.m[cam]
        if not (lo <= yaw <= hi):
            return None
        f = feats([yaw], [pitch])[0]
        return float(f @ cx), float(f @ cy)


def load_events(path):
    frames, pairs = {}, {"Left": [], "Right": []}
    with opener(path) as f:
        for line in f:
            if '"detections_raw"' in line:
                o = json.loads(line)
                frames.setdefault(o["frame_index"], {})["dets"] = o["detections"]
                for d in o["detections"]:
                    p = d.get("position")
                    if p and len(pairs[d["camera"]]) < 200000:
                        pairs[d["camera"]].append((*d["camera_center"], p["yaw"], p["pitch"]))
            elif '"world_state"' in line:
                o = json.loads(line)
                frames.setdefault(o["frame_index"], {})["ball"] = o.get("ball")
            elif '"pan_decision"' in line:
                o = json.loads(line)
                frames.setdefault(o["frame_index"], {})["pan"] = o["pose"]
    return frames, pairs


def load_trace(path):
    out = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            out[int(r["frame"])] = r
    return out


def frame_corners(yaw, pitch, fov_deg, aspect=9 / 16):
    h = math.radians(fov_deg) / 2
    v = math.atan(math.tan(h) * aspect)
    return [(yaw - h, pitch + v), (yaw + h, pitch + v), (yaw + h, pitch - v), (yaw - h, pitch - v)]


def draw_view(img, cam, inv, yaw, pitch, fov, color, label):
    W, H = img.shape[1], img.shape[0]
    pts = []
    for (cy_, cp_) in frame_corners(yaw, pitch, fov):
        q = inv(cam, cy_, cp_)
        if q is None:
            return
        pts.append((int(q[0] * W), int(q[1] * H)))
    cv2.polylines(img, [np.array(pts, np.int32)], True, color, 2, cv2.LINE_AA)
    c = inv(cam, yaw, pitch)
    if c:
        x, y = int(c[0] * W), int(c[1] * H)
        cv2.drawMarker(img, (x, y), color, cv2.MARKER_CROSS, 18, 2)
        cv2.putText(img, label, (pts[0][0] + 4, pts[0][1] + 16), FONT, 0.45, color, 1, cv2.LINE_AA)


def draw_tracked(img, cam, inv, yaw, pitch, state, color, tag):
    if yaw is None:
        return
    q = inv(cam, yaw, pitch)
    if q is None or not (0 <= q[0] <= 1 and 0 <= q[1] <= 1):
        return
    W, H = img.shape[1], img.shape[0]
    x, y = int(q[0] * W), int(q[1] * H)
    cv2.circle(img, (x, y), 16, color, 3, cv2.LINE_AA)
    cv2.putText(img, f"{tag} {state}", (x + 18, y - 10), FONT, 0.45, color, 1, cv2.LINE_AA)


def annotate(img, cam_name, dets, poly):
    W, H = img.shape[1], img.shape[0]
    P = [(int(x * W), int(y * H)) for x, y in poly]
    if len(P) >= 3:
        cv2.polylines(img, [np.array(P, np.int32)], True, GREEN, 2, cv2.LINE_AA)
    for d in dets:
        if d["camera"] != cam_name:
            continue
        cx, cy = d["camera_center"]
        w, h = d["camera_size"]
        if d["class_id"] == BALL:
            ok = ball_kept(cx, cy, poly)
            x, y = int(cx * W), int(cy * H)
            r = max(6, int(max(w * W, h * H)))
            cv2.circle(img, (x, y), r, ORANGE if ok else GREY, 2, cv2.LINE_AA)
            if not ok:
                cv2.drawMarker(img, (x, y), GREY, cv2.MARKER_TILTED_CROSS, 10, 1)
            cv2.putText(img, f"{d['confidence']:.2f}", (x + r + 2, y), FONT, 0.35, ORANGE if ok else GREY, 1, cv2.LINE_AA)
        elif d["class_id"] == PERSON:
            ok = person_kept(cx, cy, h, poly)
            x0, y0 = int((cx - w / 2) * W), int((cy - h / 2) * H)
            x1, y1 = int((cx + w / 2) * W), int((cy + h / 2) * H)
            cv2.rectangle(img, (x0, y0), (x1, y1), GREEN if ok else GREY, 1)


def legend(canvas, i, fps, a0, rec):
    t = i / fps
    lines = [
        (f"t={int(t // 60):02d}:{t % 60:05.2f}  frame {i}", WHITE),
        ("green outline = our pitch | orange ring = ball inside | grey ring x = ball rejected", WHITE),
        (f"CYAN = A0 ball-first WITH outline  (ball: {a0.get('ball_state', '-') if a0 else '-'})", CYAN),
        (f"MAGENTA = GPU render you watched, NO outline  (ball: {rec or '-'})", MAGENTA),
    ]
    cv2.rectangle(canvas, (0, 0), (760, 30 + 20 * len(lines)), (0, 0, 0), -1)
    for k, (s, c) in enumerate(lines):
        cv2.putText(canvas, s, (10, 22 + 20 * k), FONT, 0.5, c, 1, cv2.LINE_AA)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--left", required=True)
    ap.add_argument("--right", required=True)
    ap.add_argument("--roi", required=True)
    ap.add_argument("--trace-a0", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--every", type=int, default=2)
    ap.add_argument("--pane-width", type=int, default=960)
    ap.add_argument("--sync-offset", type=int, default=0)
    ap.add_argument("--stills-dir", default="")
    ap.add_argument("--still-times", default="20,95,170,245")
    ap.add_argument("--max-frames", type=int, default=0)
    a = ap.parse_args(argv)

    roi = json.load(open(a.roi))
    frames, pairs = load_events(a.events)
    inv = InvMap(pairs)
    trace = load_trace(a.trace_a0)
    capL, capR = cv2.VideoCapture(a.left), cv2.VideoCapture(a.right)
    if a.sync_offset > 0:
        capR.set(cv2.CAP_PROP_POS_FRAMES, a.sync_offset)
    elif a.sync_offset < 0:
        capL.set(cv2.CAP_PROP_POS_FRAMES, -a.sync_offset)
    fps = capL.get(cv2.CAP_PROP_FPS) or 59.94
    pw = a.pane_width
    ph = pw * 9 // 16
    vw = cv2.VideoWriter(a.out + ".tmp.mp4", cv2.VideoWriter_fourcc(*"mp4v"), fps / a.every, (pw * 2, ph))
    still_frames = {int(float(s) * fps) for s in a.still_times.split(",") if s} if a.stills_dir else set()
    if a.stills_dir:
        os.makedirs(a.stills_dir, exist_ok=True)
    n_total = max(frames) + 1 if frames else 0
    i = 0
    while True:
        okL, L = capL.read()
        okR, R = capR.read()
        if not (okL and okR) or i >= n_total or (a.max_frames and i >= a.max_frames):
            break
        if i % a.every == 0 or i in still_frames:
            fr = frames.get(i, {})
            dets = fr.get("dets", [])
            panes = []
            for cam, img, poly in (("Left", L, roi["left"]), ("Right", R, roi["right"])):
                img = cv2.resize(img, (pw, ph), interpolation=cv2.INTER_AREA)
                annotate(img, cam, dets, poly)
                tr = trace.get(i)
                if tr and tr.get("ball_yaw"):
                    draw_tracked(img, cam, inv, float(tr["ball_yaw"]), float(tr["ball_pitch"]), tr["ball_state"], CYAN, "A0")
                rb = fr.get("ball")
                if rb:
                    draw_tracked(img, cam, inv, rb["yaw"], rb["pitch"], rb["state"].lower(), MAGENTA, "GPU")
                if tr:
                    draw_view(img, cam, inv, float(tr["yaw"]), float(tr["pitch"]), float(tr["fov"] or 44), CYAN, "A0 camera")
                pan = fr.get("pan")
                if pan:
                    draw_view(img, cam, inv, pan["yaw"], pan["pitch"], pan.get("fov_degrees") or 44, MAGENTA, "GPU render camera")
                cv2.putText(img, f"{cam.upper()} camera", (pw - 150, ph - 12), FONT, 0.55, WHITE, 2, cv2.LINE_AA)
                panes.append(img)
            canvas = np.hstack(panes)
            rb = fr.get("ball")
            legend(canvas, i, fps, trace.get(i), rb["state"].lower() if rb else "none")
            if i % a.every == 0:
                vw.write(canvas)
            if i in still_frames:
                p = os.path.join(a.stills_dir, f"still_t{i / fps:06.1f}s.jpg")
                cv2.imwrite(p, canvas, [cv2.IMWRITE_JPEG_QUALITY, 88])
        i += 1
        if i % 1800 == 0:
            print(f"  {i}/{n_total} frames", flush=True)
    vw.release()
    rc = os.system(f"ffmpeg -v error -y -i '{a.out}.tmp.mp4' -c:v libx264 -preset veryfast -crf 26 -pix_fmt yuv420p -movflags +faststart '{a.out}'")
    if rc != 0:
        raise SystemExit(f"ffmpeg transcode failed ({rc}); raw file kept at {a.out}.tmp.mp4")
    os.remove(a.out + ".tmp.mp4")
    print(f"wrote {a.out} ({i} source frames, every {a.every})")


if __name__ == "__main__":
    main()
