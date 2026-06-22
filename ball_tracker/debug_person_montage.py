#!/usr/bin/env python3
"""
FFA Phase 2 - Person Detection Debug Montage
============================================
Visual validation tool: renders all four perspective crops per sampled frame,
draws raw YOLO person boxes (before any filtering), then shows which survive
pitch/hotspot filtering, and marks the final cluster centre.

Output: debug_montage.mp4  (one composite frame per sampled frame)
        debug_summary.json (per-frame detection counts + config)

Each output frame = 2x2 grid of 1280x720 crops, annotated:
  - GREEN box  : detection passes pitch filter
  - RED box    : detection rejected by pitch filter
  - ORANGE box : detection has no track ID yet (ByteTrack warmup)
  - Box label  : "id=N conf=0.83 yaw=+12.4 p=+3.1" (or "REJECTED: pitch +22")
  - Cyan cross : back-projected cluster centre (if one exists)
  - Top-left   : crop yaw, frame index, raw detection count / kept count

Summary at end: model filename, imgsz, conf threshold, class filter,
total detections, kept after pitch filter, kept after dedup, frames with cluster.

Usage:
  python ball_tracker/debug_person_montage.py \
      --input work/equirect_trim.mp4 \
      --output-video debug_montage.mp4 \
      --output-json  debug_summary.json \
      --start-frame 700 --end-frame 1300 --sample-interval 15

Env:
  YOLO_PERSON_WEIGHTS  - weights to audit (default: yolov8n.pt)
  FFMPEG_BIN           - ffmpeg path (default: /usr/bin/ffmpeg)
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

import cv2
import numpy as np

FFMPEG = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
YOLO_PERSON_WEIGHTS = (
    os.environ.get("YOLO_PERSON_WEIGHTS")
    or os.environ.get("YOLO_MODEL")
    or "yolov8n.pt"
)

# Must exactly match player_activity.py
CROP_YAWS_DEG   = [0, 90, 180, 270]
CROP_FOV_DEG    = 110
CROP_W, CROP_H  = 1280, 720
PERSON_CLASS_ID = 0
YOLO_CONF       = 0.25
YOLO_IMGSZ      = 1280
PITCH_MIN_DEG   = -25.0
PITCH_MAX_DEG   =  20.0
DEDUP_RADIUS_DEG     = 8.0
CLUSTER_RADIUS_DEG   = 25.0
CLUSTER_MIN_PLAYERS  = 2

COL_PASS     = (0, 220, 0)
COL_REJECT   = (0, 0, 220)
COL_NOWARMUP = (0, 165, 255)
COL_CLUSTER  = (0, 220, 220)
COL_TEXT     = (255, 255, 255)
FONT = cv2.FONT_HERSHEY_SIMPLEX
FS, FT = 0.45, 1


def extract_crop_frame(eq, yaw_deg, fov=CROP_FOV_DEG, ow=CROP_W, oh=CROP_H):
    h, w = eq.shape[:2]
    f = (ow / 2.0) / math.tan(math.radians(fov / 2.0))
    xs = np.linspace(0, ow - 1, ow); ys = np.linspace(0, oh - 1, oh)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - ow/2) / f; ry = -(yv - oh/2) / f; rz = np.ones_like(rx)
    n = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx/n, ry/n, rz/n
    cy = math.radians(yaw_deg)
    wx =  math.cos(cy)*rx + math.sin(cy)*rz
    wy = ry
    wz = -math.sin(cy)*rx + math.cos(cy)*rz
    mx = ((np.arctan2(wx, wz) / (2*math.pi)) + 0.5) * w
    my = (0.5 - np.arcsin(np.clip(wy, -1, 1)) / math.pi) * h
    return cv2.remap(eq, mx.astype(np.float32), my.astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def crop_pixel_to_yaw_pitch(px, py, yaw_deg, fov=CROP_FOV_DEG, w=CROP_W, h=CROP_H):
    nx = (px - w/2) / (w/2); ny = (py - h/2) / (h/2)
    f = 1.0 / math.tan(math.radians(fov/2))
    rx = nx/f; ry = -ny/f * (w/h); rz = 1.0
    n = math.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx/n, ry/n, rz/n
    cy = math.radians(yaw_deg)
    wx =  math.cos(cy)*rx + math.sin(cy)*rz
    wy = ry
    wz = -math.sin(cy)*rx + math.cos(cy)*rz
    return math.degrees(math.atan2(wx, wz)), math.degrees(math.asin(max(-1., min(1., wy))))


def angular_distance(y1, p1, y2, p2):
    dy = math.radians(y1 - y2); dp = math.radians(p1 - p2)
    a  = (math.sin(dp/2)**2 + math.cos(math.radians(p1)) *
          math.cos(math.radians(p2)) * math.sin(dy/2)**2)
    return math.degrees(2 * math.asin(math.sqrt(min(1., a))))


def yaw_mean(yaws):
    sx = sum(math.cos(math.radians(y)) for y in yaws)
    sy = sum(math.sin(math.radians(y)) for y in yaws)
    return math.degrees(math.atan2(sy, sx))


def spherical_centroid(pts):
    return yaw_mean([p[0] for p in pts]), sum(p[1] for p in pts)/len(pts)


def dedup(players):
    if not players: return []
    order = sorted(range(len(players)), key=lambda i: -players[i]["conf"])
    used  = [False]*len(players); kept = []
    for i in order:
        if used[i]: continue
        kept.append(players[i]); used[i] = True
        for j in range(len(players)):
            if not used[j] and angular_distance(
                players[i]["yaw"], players[i]["pitch"],
                players[j]["yaw"], players[j]["pitch"]) < DEDUP_RADIUS_DEG:
                used[j] = True
    return kept


def cluster_players(players):
    if not players: return []
    n = len(players); cl = [-1]*n
    def d(i, j): return angular_distance(players[i]["yaw"], players[i]["pitch"],
                                          players[j]["yaw"], players[j]["pitch"])
    cid = 0
    for i in range(n):
        if cl[i] != -1: continue
        nb = [j for j in range(n) if i != j and d(i,j) <= CLUSTER_RADIUS_DEG]
        if len(nb)+1 < CLUSTER_MIN_PLAYERS: continue
        cl[i] = cid; stack = list(nb)
        while stack:
            k = stack.pop()
            if cl[k] == -1:
                cl[k] = cid
                nk = [j for j in range(n) if j!=k and d(k,j) <= CLUSTER_RADIUS_DEG]
                if len(nk)+1 >= CLUSTER_MIN_PLAYERS:
                    stack.extend(j for j in nk if cl[j]==-1)
        cid += 1
    g = defaultdict(list)
    for i, c in enumerate(cl):
        if c != -1: g[c].append(players[i])
    return list(g.values())


def best_cluster(clusters):
    if not clusters: return None
    return max(clusters, key=lambda c: (len(c), sum(p["conf"] for p in c)/len(c)))


def draw_panel(crop_img, crop_yaw, frame_idx, raw_dets, cluster_centre):
    img = crop_img.copy()
    total  = len(raw_dets)
    passed = sum(1 for d in raw_dets if d["reject_reason"] is None)
    header = f"yaw={crop_yaw:+d}  frame={frame_idx}  raw={total} kept={passed}"
    cv2.rectangle(img, (0, 0), (CROP_W, 22), (30, 30, 30), -1)
    cv2.putText(img, header, (4, 15), FONT, FS, COL_TEXT, FT, cv2.LINE_AA)

    for d in raw_dets:
        x1, y1, x2, y2 = int(d["x1"]), int(d["y1"]), int(d["x2"]), int(d["y2"])
        reason = d["reject_reason"]
        if reason is not None:
            col = COL_REJECT
            tag = f"REJECTED:{reason} p={d['pitch']:+.1f}"
        elif d["track_id"] is None:
            col = COL_NOWARMUP
            tag = f"no-id c={d['conf']:.2f} yaw={d['yaw']:+.1f} p={d['pitch']:+.1f}"
        else:
            col = COL_PASS
            tag = f"id={d['track_id']} c={d['conf']:.2f} yaw={d['yaw']:+.1f} p={d['pitch']:+.1f}"
        cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
        ty = max(y1 - 4, 16)
        cv2.putText(img, tag, (x1, ty), FONT, FS, col, FT, cv2.LINE_AA)
        cv2.circle(img, ((x1+x2)//2, (y1+y2)//2), 3, col, -1)

    if cluster_centre is not None:
        cy_g, cp_g = cluster_centre["yaw"], cluster_centre["pitch"]
        p_rad = math.radians(cp_g); wy_rad = math.radians(cy_g)
        wx  = math.cos(p_rad) * math.sin(wy_rad)
        wy_v = math.sin(p_rad)
        wz  = math.cos(p_rad) * math.cos(wy_rad)
        c_ang = -math.radians(crop_yaw)
        rx_c =  math.cos(c_ang)*wx + math.sin(c_ang)*wz
        ry_c =  wy_v
        rz_c = -math.sin(c_ang)*wx + math.cos(c_ang)*wz
        if rz_c > 0:
            f = (CROP_W/2) / math.tan(math.radians(CROP_FOV_DEG/2))
            px = int(rx_c / rz_c * f + CROP_W/2)
            py = int(-ry_c / rz_c * f * (CROP_W/CROP_H) + CROP_H/2)
            if 0 <= px < CROP_W and 0 <= py < CROP_H:
                cv2.drawMarker(img, (px, py), COL_CLUSTER, cv2.MARKER_CROSS, 24, 2)
                cv2.putText(img, f"CLUSTER yaw={cy_g:+.1f} p={cp_g:+.1f}",
                            (px+8, py), FONT, FS, COL_CLUSTER, FT, cv2.LINE_AA)
    return img


def run(args):
    from ultralytics import YOLO
    model_path = args.model
    print(f"[debug] Model      : {model_path}")
    print(f"[debug] imgsz      : {YOLO_IMGSZ}   conf={YOLO_CONF}   class={PERSON_CLASS_ID} (person)")
    print(f"[debug] Pitch gate : {PITCH_MIN_DEG} to {PITCH_MAX_DEG} deg")

    models = {y: YOLO(model_path) for y in CROP_YAWS_DEG}

    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        sys.exit(f"[error] Cannot open: {args.input}")
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps          = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()

    start = max(0, args.start_frame)
    end   = min(total_frames, args.end_frame) if args.end_frame > 0 else total_frames
    iv    = max(1, args.sample_interval)
    sample_set = set(range(start, end, iv))

    out_w, out_h = CROP_W*2, CROP_H*2
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(args.output_video, fourcc, max(1.0, fps/iv), (out_w, out_h))

    cap = cv2.VideoCapture(args.input)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    total_raw = total_pitch_pass = total_dedup_pass = frames_with_cluster = 0
    frame_records = []

    for fidx in range(start, end):
        ret, frame = cap.read()
        if not ret: break
        if fidx not in sample_set: continue

        crops      = {y: extract_crop_frame(frame, y) for y in CROP_YAWS_DEG}
        all_passed = []
        panel_data = {}

        for crop_yaw in CROP_YAWS_DEG:
            crop = crops[crop_yaw]
            results = models[crop_yaw].track(
                crop, persist=True, tracker="bytetrack.yaml",
                conf=YOLO_CONF, imgsz=YOLO_IMGSZ,
                classes=[PERSON_CLASS_ID], verbose=False)
            raw_dets = []
            for r in results:
                if r.boxes is None: continue
                for box in r.boxes:
                    if int(box.cls[0]) != PERSON_CLASS_ID: continue
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    conf_val = float(box.conf[0])
                    track_id = int(box.id[0]) if box.id is not None else None
                    cx, cy_b = (x1+x2)/2, (y1+y2)/2
                    yaw_g, pitch_g = crop_pixel_to_yaw_pitch(cx, cy_b, crop_yaw)
                    total_raw += 1
                    if not (PITCH_MIN_DEG <= pitch_g <= PITCH_MAX_DEG):
                        reason = "pitch"
                    else:
                        reason = None
                        total_pitch_pass += 1
                        all_passed.append({
                            "yaw": yaw_g, "pitch": pitch_g, "conf": conf_val,
                            "crop_yaw": crop_yaw
                        })
                    raw_dets.append({
                        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                        "conf": round(conf_val, 3),
                        "track_id": track_id,
                        "yaw": round(yaw_g, 2), "pitch": round(pitch_g, 2),
                        "reject_reason": reason,
                    })
            panel_data[crop_yaw] = raw_dets

        deduped = dedup(all_passed)
        total_dedup_pass += len(deduped)

        clusters = cluster_players(deduped)
        top      = best_cluster(clusters)
        cluster_centre = None
        if top:
            pts = [(p["yaw"], p["pitch"]) for p in top]
            cy_m, cp_m = spherical_centroid(pts)
            cluster_centre = {"yaw": round(cy_m, 2), "pitch": round(cp_m, 2),
                              "size": len(top)}
            frames_with_cluster += 1

        panels = {y: draw_panel(crops[y], y, fidx, panel_data[y], cluster_centre)
                  for y in CROP_YAWS_DEG}
        top_row    = np.hstack([panels[0],   panels[90]])
        bottom_row = np.hstack([panels[180], panels[270]])
        grid = np.vstack([top_row, bottom_row])

        n_raw  = sum(len(panel_data[y]) for y in CROP_YAWS_DEG)
        n_kept = len(deduped)
        cc_str = (f"CLUSTER yaw={cluster_centre['yaw']:+.1f} p={cluster_centre['pitch']:+.1f} "
                  f"size={cluster_centre['size']}") if cluster_centre else "NO CLUSTER"
        summary_line = (f"frame {fidx}  raw={n_raw}  pitch-pass={len(all_passed)}"
                        f"  dedup={n_kept}  {cc_str}")
        cv2.rectangle(grid, (0, CROP_H-22), (out_w, CROP_H), (40, 40, 40), -1)
        cv2.putText(grid, summary_line, (8, CROP_H-6), FONT, 0.5, COL_TEXT, 1, cv2.LINE_AA)
        vw.write(grid)

        print(f"  frame {fidx:5d}  raw={n_raw:3d}  pitch-pass={len(all_passed):3d}"
              f"  dedup={n_kept:2d}  " +
              (f"cluster size={cluster_centre['size']} yaw={cluster_centre['yaw']:+.1f}"
               f" p={cluster_centre['pitch']:+.1f}" if cluster_centre else "NO CLUSTER"))

        frame_records.append({
            "frame": fidx,
            "raw_detections": n_raw,
            "pitch_pass": len(all_passed),
            "dedup": n_kept,
            "cluster_centre": cluster_centre,
            "per_crop": {str(y): [
                {"conf": d["conf"], "yaw": d["yaw"], "pitch": d["pitch"],
                 "track_id": d["track_id"], "rejected": d["reject_reason"]}
                for d in panel_data[y]
            ] for y in CROP_YAWS_DEG},
        })

    cap.release(); vw.release()

    total_sampled = len(frame_records)
    output = {
        "config": {
            "model": model_path, "imgsz": YOLO_IMGSZ,
            "conf_threshold": YOLO_CONF, "person_class_id": PERSON_CLASS_ID,
            "pitch_min_deg": PITCH_MIN_DEG, "pitch_max_deg": PITCH_MAX_DEG,
            "dedup_radius_deg": DEDUP_RADIUS_DEG,
            "cluster_radius_deg": CLUSTER_RADIUS_DEG,
            "cluster_min_players": CLUSTER_MIN_PLAYERS,
        },
        "totals": {
            "total_raw_detections": total_raw,
            "after_pitch_filter": total_pitch_pass,
            "after_dedup": total_dedup_pass,
            "frames_sampled": total_sampled,
            "frames_with_cluster": frames_with_cluster,
            "cluster_coverage_pct": round(100*frames_with_cluster/max(1, total_sampled), 1),
        },
        "frames": frame_records,
    }
    with open(args.output_json, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'='*70}")
    print(f"MODEL          : {model_path}")
    print(f"IMGSZ          : {YOLO_IMGSZ}   CONF: {YOLO_CONF}   CLASS: {PERSON_CLASS_ID} (person)")
    print(f"PITCH GATE     : {PITCH_MIN_DEG} to {PITCH_MAX_DEG} deg")
    print(f"FRAMES SAMPLED : {total_sampled}")
    print(f"TOTAL RAW DETS : {total_raw}")
    print(f"AFTER PITCH    : {total_pitch_pass}  ({100*total_pitch_pass//max(1,total_raw)}%)")
    print(f"AFTER DEDUP    : {total_dedup_pass}")
    print(f"WITH CLUSTER   : {frames_with_cluster}/{total_sampled}"
          f"  ({100*frames_with_cluster//max(1,total_sampled)}%)")
    print(f"OUTPUT VIDEO   : {args.output_video}")
    print(f"OUTPUT JSON    : {args.output_json}")
    print(f"{'='*70}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input",           default="work/equirect_trim.mp4")
    p.add_argument("--output-video",    default="debug_montage.mp4")
    p.add_argument("--output-json",     default="debug_summary.json")
    p.add_argument("--model",           default=YOLO_PERSON_WEIGHTS)
    p.add_argument("--start-frame",     type=int, default=700)
    p.add_argument("--end-frame",       type=int, default=1300)
    p.add_argument("--sample-interval", type=int, default=15)
    run(p.parse_args())
