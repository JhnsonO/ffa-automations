#!/usr/bin/env python3
"""Broadcast-level metrics from two render event logs (same definitions).

- on_static: share of frames whose ball output (Tracking/Coasting/Bridged)
  lies within 1 deg of any spot either arm ever excluded (union of dormant
  sites, local calibration). LOCATION PROXY, not identity truth.
- players_in_view: mean share of detected players (person class) whose
  panorama position projects inside the broadcast frame (action framing).
- no_ball: share of frames with no ball output.
- cam speed p95 (deg/s) and yaw reversals > 2 deg/s (jitter) from the
  presented pose.
"""
import json, math, sys
sys.path.insert(0, "/root/oevrender")
from overlay import Cam
FPS = 60000 / 1001
W, H = 1920, 1080
cam = Cam(json.load(open("/root/oevsrc/match.json"))["params"]["cameraAxisOffset"])

def load(p):
    ws, pose = {}, {}
    for l in open(p):
        e = json.loads(l)
        if e["kind"] == "world_state": ws[e["frame_index"]] = e
        elif e["kind"] == "pose_presented": pose[e["frame_index"]] = e["pose"]
    return ws, pose

def sites(rep):
    return [(t["yaw"], t["pitch"]) for t in json.load(open(rep))["sites"]["transitions"] if t["kind"] == "dormant"]

arms = {"BEFORE": load("before_events.jsonl"), "AFTER": load("after_events.jsonl")}
S = sites("rep_before.json") + sites("rep_after.json")
wins = [("full 0-179.7 s", 0, 179.7), ("13-21 s early spare", 13, 21), ("34-43 s wrong ball", 34, 43),
        ("117-131 s stationary ball", 117, 131), ("132-141 s long pass", 132, 141)]

def metrics(ws, pose, a, b):
    f0, f1 = int(a * FPS), int(b * FPS)
    n = on_static = no_ball = 0; piv = []; speeds = []; rev = 0; prev_v = 0.0
    for i in range(f0, min(f1, len(ws))):
        e = ws[i]; p = pose[i]; n += 1
        ball = e["ball"]
        if not ball or ball["state"] == "Lost":
            no_ball += 1
        elif any(math.hypot(ball["yaw"] - y, ball["pitch"] - q) <= 0.0175 for y, q in S):
            on_static += 1
        B = cam.basis(p["yaw"], p["pitch"]); fov = p["fov_degrees"] or 40
        pl = e["players"]
        if pl:
            inside = 0
            for x in pl:
                pr = cam.project(B, fov, W, H, x["yaw"], x["pitch"])
                inside += bool(pr and 0 <= pr[0] < W and 0 <= pr[1] < H)
            piv.append(inside / len(pl))
        if i > f0:
            q = pose[i - 1]
            dy = math.degrees(p["yaw"] - q["yaw"]) * FPS
            dp = math.degrees(p["pitch"] - q["pitch"]) * FPS
            speeds.append(math.hypot(dy, dp))
            if abs(dy) > 2 and abs(prev_v) > 2 and (dy > 0) != (prev_v > 0): rev += 1
            if abs(dy) > 2: prev_v = dy
    speeds.sort()
    return {"on_static_%": round(100 * on_static / n, 1), "no_ball_%": round(100 * no_ball / n, 1),
            "players_in_view_%": round(100 * sum(piv) / max(1, len(piv)), 1),
            "cam_speed_p95_deg_s": round(speeds[int(0.95 * len(speeds))], 1) if speeds else 0,
            "yaw_reversals": rev}

print("| Window | Arm | ball on excluded/static spot % | no ball % | players in view % | camera speed p95 (deg/s) | yaw reversals |")
print("|---|---|---:|---:|---:|---:|---:|")
for name, a, b in wins:
    for arm, (ws, pose) in arms.items():
        m = metrics(ws, pose, a, b)
        print(f"| {name} | {arm} | {m['on_static_%']} | {m['no_ball_%']} | {m['players_in_view_%']} | {m['cam_speed_p95_deg_s']} | {m['yaw_reversals']} |")
print(f"\nstatic spots (union, local calibration): {sorted(set((round(y,3),round(q,3)) for y,q in S))}")
