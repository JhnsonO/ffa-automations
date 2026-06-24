#!/usr/bin/env python3
"""Pose-guided candidate selection diagnostic — EXPERIMENT ONLY.

This does not alter Stage 1, Stage 1b, Stage 2, Tier A, thresholds, or the
renderer. It tests a simple frame-level question: when several detector
candidates exist, does proximity to a detected player's lower body help choose
a more plausible candidate than raw detector confidence alone?

The diagnostic has a conservative aerial/occlusion fallback: pose is never a
hard gate. If no candidate in a frame has usable lower-body support, the raw
candidate ranking is retained.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

CROP_YAWS = (0, 90, 180, 270)
CROP_FOV = 110.0
CROP_W, CROP_H = 1280, 720
SAMPLE_IDS = ("T0001", "T0025", "T0093", "T0080", "T0079", "T0036", "T0130", "T0030", "T0090", "T0175")
LOWER_BODY_RADIUS_PX = 320.0
POSE_SUPPORT_MIN = 0.20

COL = {
    "bg": (14, 14, 22), "panel": (23, 23, 36), "white": (235, 235, 235),
    "dim": (145, 145, 160), "all": (230, 190, 50), "raw": (70, 85, 235),
    "pose": (60, 225, 100), "track": (220, 70, 235), "person": (60, 225, 100),
    "lower": (255, 205, 55), "warn": (235, 165, 55), "header": (38, 42, 72),
}


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def font(size: int, bold: bool = False):
    for path in (
        f"/usr/share/fonts/truetype/dejavu/DejaVuSansMono{'Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationMono-{'Bold' if bold else 'Regular'}.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def angular_distance(y1: float, p1: float, y2: float, p2: float) -> float:
    dy = math.radians(y1 - y2)
    val = math.sin(math.radians(p1))*math.sin(math.radians(p2)) + math.cos(math.radians(p1))*math.cos(math.radians(p2))*math.cos(dy)
    return math.degrees(math.acos(clamp(val, -1.0, 1.0)))


def extract_crop(eq: np.ndarray, crop_yaw: float) -> np.ndarray:
    h_eq, w_eq = eq.shape[:2]
    f = (CROP_W/2.0) / math.tan(math.radians(CROP_FOV/2.0))
    xs = np.linspace(0, CROP_W-1, CROP_W); ys = np.linspace(0, CROP_H-1, CROP_H)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv-CROP_W/2.0)/f; ry = -(yv-CROP_H/2.0)/f; rz = np.ones_like(rx)
    norm = np.sqrt(rx*rx + ry*ry + rz*rz); rx, ry, rz = rx/norm, ry/norm, rz/norm
    cy = math.radians(crop_yaw)
    wx = math.cos(cy)*rx + math.sin(cy)*rz
    wy = ry
    wz = -math.sin(cy)*rx + math.cos(cy)*rz
    mx = ((np.arctan2(wx, wz)/(2*math.pi))+0.5)*w_eq
    my = (0.5 - np.arcsin(np.clip(wy, -1, 1))/math.pi)*h_eq
    return cv2.remap(eq, mx.astype(np.float32), my.astype(np.float32), cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def nearest_crop_yaw(yaw: float) -> int:
    def d(a, b):
        x = abs(a-b) % 360.0
        return min(x, 360.0-x)
    return min(CROP_YAWS, key=lambda y: d(yaw, y))


def yp_to_pixel(yaw: float, pitch: float, crop_yaw: float) -> Optional[Tuple[float, float]]:
    f = (CROP_W/2.0) / math.tan(math.radians(CROP_FOV/2.0))
    ya, pa = math.radians(yaw), math.radians(pitch)
    wx, wy, wz = math.sin(ya)*math.cos(pa), math.sin(pa), math.cos(ya)*math.cos(pa)
    cy = math.radians(crop_yaw)
    rx = math.cos(-cy)*wx + math.sin(-cy)*wz
    rz = -math.sin(-cy)*wx + math.cos(-cy)*wz
    if rz <= 0: return None
    x, y = rx/rz*f + CROP_W/2.0, -wy/rz*f + CROP_H/2.0
    return (x, y) if 0 <= x < CROP_W and 0 <= y < CROP_H else None


def bbox(candidate: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    geo = candidate.get("detection_geometry")
    vals = geo.get("bbox_xyxy") if isinstance(geo, dict) else None
    if not isinstance(vals, (list, tuple)) or len(vals) != 4: return None
    try:
        x1, y1, x2, y2 = map(float, vals)
        return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None
    except Exception:
        return None


def candidate_point(candidate: Dict[str, Any], crop_yaw: int) -> Optional[Tuple[float, float]]:
    b = bbox(candidate)
    if b: return ((b[0]+b[2])/2.0, (b[1]+b[3])/2.0)
    if candidate.get("yaw") is None or candidate.get("pitch") is None: return None
    return yp_to_pixel(float(candidate["yaw"]), float(candidate["pitch"]), crop_yaw)


def selected_observations(tracklet: Dict[str, Any]):
    fr = tracklet.get("frames", [])
    if not fr: return []
    if len(fr) == 1: return [("EARLY", fr[0]), ("MID", fr[0]), ("LATE", fr[0])]
    if len(fr) == 2: return [("EARLY", fr[0]), ("MID", fr[0]), ("LATE", fr[-1])]
    return [("EARLY", fr[0]), ("MID", fr[len(fr)//2]), ("LATE", fr[-1])]


def match_tracklet_candidate(cands: Sequence[Dict[str, Any]], obs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    best = None
    for c in cands:
        if c.get("yaw") is None or c.get("pitch") is None: continue
        d = angular_distance(float(obs["yaw"]), float(obs["pitch"]), float(c["yaw"]), float(c["pitch"]))
        if best is None or d < best[0]: best = (d, c)
    return best[1] if best and best[0] <= 0.03 else None


def load_pose(weights: str):
    from ultralytics import YOLO
    return YOLO(weights)


def pose_people(model, crop: np.ndarray, conf: float, imgsz: int):
    result = model.predict(crop, conf=conf, imgsz=imgsz, device="cpu", verbose=False)[0]
    if result.boxes is None or len(result.boxes) == 0: return []
    boxes = result.boxes.xyxy.cpu().numpy(); scores = result.boxes.conf.cpu().numpy()
    kps = result.keypoints.data.cpu().numpy() if result.keypoints is not None else None
    out = []
    for i, b in enumerate(boxes):
        lower = []
        if kps is not None and i < len(kps):
            for kp_i in (13, 14, 15, 16):
                if kp_i < len(kps[i]):
                    x, y, s = map(float, kps[i][kp_i])
                    if s >= 0.2: lower.append((x, y))
        out.append({"bbox": tuple(map(float, b)), "conf": float(scores[i]), "lower": lower})
    return out


def lower_distance(point: Optional[Tuple[float, float]], people: Sequence[Dict[str, Any]]) -> Optional[float]:
    if point is None: return None
    lower = [q for p in people for q in p["lower"]]
    if not lower: return None
    return min(math.dist(point, q) for q in lower)


def raw_conf(c: Dict[str, Any]) -> float:
    for k in ("weighted_conf", "raw_conf", "score"):
        if c.get(k) is not None:
            try: return float(c[k])
            except Exception: pass
    return 0.0


def rank_candidates(candidates: Sequence[Dict[str, Any]], people: Sequence[Dict[str, Any]], crop_yaw: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str, List[Dict[str, Any]]]:
    scored = []
    for c in candidates:
        point = candidate_point(c, crop_yaw)
        dist = lower_distance(point, people)
        support = None if dist is None else clamp(1.0 - dist / LOWER_BODY_RADIUS_PX)
        item = {"candidate": c, "raw": raw_conf(c), "lower_distance_px": dist, "pose_support": support}
        scored.append(item)
    if not scored: return None, None, "NO_CANDIDATES", scored
    raw_item = max(scored, key=lambda x: x["raw"])
    supported = [x for x in scored if x["pose_support"] is not None and x["pose_support"] >= POSE_SUPPORT_MIN]
    if not supported:
        return raw_item["candidate"], raw_item["candidate"], "RAW_FALLBACK_NO_LOWER_BODY_SUPPORT", scored
    # Only candidates with actual support compete; detector confidence remains dominant.
    raw_max = max(x["raw"] for x in supported) or 1.0
    for item in supported:
        item["guided_score"] = 0.65*(item["raw"]/raw_max) + 0.35*float(item["pose_support"])
    guided = max(supported, key=lambda x: x["guided_score"])
    return raw_item["candidate"], guided["candidate"], "POSE_GUIDED", scored


def same_candidate(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    if not a or not b: return False
    if a.get("yaw") is None or b.get("yaw") is None: return False
    return angular_distance(float(a["yaw"]), float(a.get("pitch", 0)), float(b["yaw"]), float(b.get("pitch", 0))) <= 0.03


def draw_marker(img, point, colour, text):
    if point is None: return
    x, y = map(int, map(round, point))
    cv2.drawMarker(img, (x, y), colour, cv2.MARKER_CROSS, 30, 2, cv2.LINE_AA)
    cv2.putText(img, text, (x+8, max(18, y-8)), cv2.FONT_HERSHEY_SIMPLEX, 0.48, colour, 1, cv2.LINE_AA)


def draw_people(img, people):
    for p in people:
        x1,y1,x2,y2 = map(int, map(round, p["bbox"]))
        cv2.rectangle(img, (x1,y1), (x2,y2), COL["person"], 2)
        for q in p["lower"]: cv2.circle(img, tuple(map(int, map(round, q))), 4, COL["lower"], -1)


def page(tracklet: Dict[str, Any], records: List[Dict[str, Any]]) -> Image.Image:
    W, H = 1920, 660
    out = Image.new("RGB", (W,H), COL["bg"]); d = ImageDraw.Draw(out)
    fb, fs = font(20, True), font(14)
    d.rectangle([0,0,W,58], fill=COL["header"])
    d.text((14,8), f"POSE-GUIDED CANDIDATE SELECTION DIAGNOSTIC — {tracklet['id']}", fill=COL["white"], font=fb)
    d.text((14,35), "Blue = raw confidence choice | Green = pose-guided choice | Magenta = existing tracklet observation | Yellow dots = lower body", fill=COL["dim"], font=fs)
    for i, r in enumerate(records):
        x0 = 640*i
        im = cv2.resize(r["image"], (640,360), interpolation=cv2.INTER_AREA)
        out.paste(Image.fromarray(cv2.cvtColor(im, cv2.COLOR_BGR2RGB)), (x0,58))
        d.rectangle([x0+5,426,x0+635,622], fill=COL["panel"])
        obs = r["obs"]
        d.text((x0+12,435), f"{r['phase']}  frame {obs['frame']}  mode: {r['mode']}", fill=COL["white"], font=font(16,True))
        d.text((x0+12,463), f"raw choice matches tracklet: {r['raw_matches']} | pose choice matches tracklet: {r['pose_matches']}", fill=COL["white"], font=fs)
        d.text((x0+12,486), f"raw conf={r['raw_conf']:.3f} | pose conf={r['pose_conf']:.3f} | pose support={r['pose_support']}", fill=COL["white"], font=fs)
        d.text((x0+12,509), f"tracklet lower-body distance={r['tracklet_lower_distance']} px | people={r['people_count']} | candidates={r['candidate_count']}", fill=COL["white"], font=fs)
        d.text((x0+12,548), "Interpretation: a changed green choice is only a candidate-priority suggestion, never an auto-rejection.", fill=COL["dim"], font=font(11))
    d.text((14,634), "Aerial/occluded cases retain raw fallback when no lower-body support exists. Diagnostic only; no tracker outputs modified.", fill=COL["dim"], font=font(12))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage1-candidates", required=True); ap.add_argument("--tracklets", required=True); ap.add_argument("--video", required=True)
    ap.add_argument("--output-dir", required=True); ap.add_argument("--pose-model", default="yolov8n-pose.pt"); ap.add_argument("--pose-conf", type=float, default=0.25); ap.add_argument("--pose-imgsz", type=int, default=960)
    ap.add_argument("--tracklet-ids", default=",".join(SAMPLE_IDS)); ap.add_argument("--preflight", action="store_true")
    args = ap.parse_args()
    model = load_pose(args.pose_model)
    if args.preflight:
        model.predict(np.zeros((64,64,3), dtype=np.uint8), imgsz=64, device="cpu", verbose=False)
        print("POSE_SELECTION_PREFLIGHT_OK"); return
    cdata = json.load(open(args.stage1_candidates)); tdata = json.load(open(args.tracklets))
    by_frame = {int(k): v for k,v in cdata.get("frames", {}).items()}; by_id = {t["id"]: t for t in tdata.get("tracklets", [])}
    ids = [x.strip() for x in args.tracklet_ids.split(",") if x.strip()]
    missing = [x for x in ids if x not in by_id]
    if missing: raise SystemExit("FATAL missing tracklets: " + ", ".join(missing))
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened(): raise SystemExit("FATAL cannot open video")
    out_dir = Path(args.output_dir); out_dir.mkdir(parents=True, exist_ok=True)
    rows=[]
    for tid in ids:
        t=by_id[tid]; records=[]
        print("[pose-select]", tid)
        for phase, obs in selected_observations(t):
            fidx=int(obs["frame"]); cands=by_frame.get(fidx, []); tracked=match_tracklet_candidate(cands, obs)
            cyaw=int(round(float(tracked.get("crop_yaw")))) if tracked and tracked.get("crop_yaw") is not None else nearest_crop_yaw(float(obs["yaw"]))
            crop_yaw_cands=[c for c in cands if c.get("crop_yaw") is None or int(round(float(c.get("crop_yaw"))))==cyaw]
            cap.set(cv2.CAP_PROP_POS_FRAMES, fidx); ok, eq=cap.read()
            if not ok: raise SystemExit(f"FATAL unreadable frame {fidx}")
            image=extract_crop(eq,cyaw); people=pose_people(model,image.copy(),args.pose_conf,args.pose_imgsz)
            raw, guided, mode, scored=rank_candidates(crop_yaw_cands,people,cyaw)
            draw_people(image, people)
            for c in crop_yaw_cands:
                b=bbox(c)
                if b: cv2.rectangle(image,tuple(map(int,b[:2])),tuple(map(int,b[2:])),COL["all"],1)
            draw_marker(image,candidate_point(raw,cyaw) if raw else None,COL["raw"],"RAW")
            draw_marker(image,candidate_point(guided,cyaw) if guided else None,COL["pose"],"POSE")
            draw_marker(image,candidate_point(tracked,cyaw) if tracked else yp_to_pixel(float(obs['yaw']),float(obs['pitch']),cyaw),COL["track"],"TRACK")
            track_dist=lower_distance(candidate_point(tracked,cyaw) if tracked else None,people)
            guided_item=next((x for x in scored if same_candidate(x['candidate'],guided)),None)
            record={"phase":phase,"obs":obs,"image":image,"mode":mode,"raw_matches":same_candidate(raw,tracked),"pose_matches":same_candidate(guided,tracked),"raw_conf":raw_conf(raw) if raw else 0.0,"pose_conf":raw_conf(guided) if guided else 0.0,"pose_support":None if not guided_item else guided_item.get('pose_support'),"tracklet_lower_distance":None if track_dist is None else round(track_dist,1),"people_count":len(people),"candidate_count":len(crop_yaw_cands)}
            records.append(record)
            rows.append({"tracklet_id":tid,"phase":phase.lower(),"frame":fidx,"selection_mode":mode,"raw_matches_tracklet":record['raw_matches'],"pose_matches_tracklet":record['pose_matches'],"raw_conf":record['raw_conf'],"pose_choice_conf":record['pose_conf'],"pose_choice_support":record['pose_support'],"tracklet_lower_body_distance_px":track_dist,"person_count":len(people),"same_crop_candidate_count":len(crop_yaw_cands)})
        page(t,records).save(out_dir/f"pose_selection_{tid}.png",format="PNG")
    cap.release()
    with open(out_dir/'pose_selection_summary.csv','w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=rows[0].keys());w.writeheader();w.writerows(rows)
    (out_dir/'pose_selection_readme.txt').write_text("POSE-GUIDED CANDIDATE SELECTION DIAGNOSTIC\n\nThis is a visual experiment only. Blue is highest raw detector confidence. Green is the pose-guided choice when a candidate is near detected lower-body keypoints. Magenta is the existing Stage 2 associated candidate. If no lower-body support is available, green deliberately falls back to blue so aerial/occluded play is not rejected. No tracker output or threshold is changed.\n")
    print(f"WROTE {len(rows)} frame records and {len(ids)} PNG pages")

if __name__=='__main__': main()
