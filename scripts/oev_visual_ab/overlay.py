#!/usr/bin/env python3
"""Draw OEV's tracker view on top of a rendered broadcast.

Projects panorama yaw/pitch into the output frame with the same maths as
reco-core's renderer (VirtualCamera basis, yaw about world up, pitch about
the yaw-rotated right axis, nalgebra look_at_rh, vertical-FOV perspective),
using the `pose_presented` pose of each output frame.

Draws:
  - every ball candidate this frame (small white ring, label = confidence)
  - the tracked ball (big circle; green Tracking, amber Coasting, cyan Bridged)
  - excluded static sites (red, from site transitions, when provided)
  - a status banner.

Usage: overlay.py IN.mp4 EVENTS.jsonl OUT.mp4 --label TEXT [--sites SITES.json]
       [--start-s A --end-s B] [--width W]
"""
import argparse, json, math, subprocess, sys
import numpy as np
import cv2

EYE = None  # set from match.json cameraAxisOffset
FPS = 60000 / 1001


def norm(v):
    return v / np.linalg.norm(v)


def axis_angle(axis, ang):
    axis = norm(axis)
    x, y, z = axis
    c, s = math.cos(ang), math.sin(ang)
    C = 1 - c
    return np.array([[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                     [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                     [z * x * C - y * s, z * y * C + x * s, c + z * z * C]])


class Cam:
    def __init__(self, axis_offset):
        self.eye = np.array([axis_offset, 0.0, axis_offset])
        self.bf = norm(-self.eye)
        self.up = np.array([0.0, 1.0, 0.0])
        self.br = norm(np.cross(self.bf, self.up))

    def direction(self, yaw, pitch):
        cp = math.cos(pitch)
        h = self.bf * (cp * math.cos(yaw)) - self.br * (cp * math.sin(yaw))
        return np.array([h[0], math.sin(pitch), h[2]])

    def basis(self, yaw, pitch):
        yq = axis_angle(self.up, yaw)
        right = yq @ self.br
        pq = axis_angle(right, pitch)
        rot = pq @ yq
        fwd = rot @ self.bf
        up = rot @ self.up
        z = norm(-fwd)
        x = norm(np.cross(up, z))
        y = np.cross(z, x)
        return x, y, z

    def project(self, basis, fov_deg, w, h, yaw, pitch):
        x, y, z = basis
        d = self.direction(yaw, pitch)
        xc, yc, zc = d @ x, d @ y, d @ z
        if zc >= -1e-6:
            return None
        t = math.tan(math.radians(fov_deg) / 2)
        nx = (xc / -zc) / (t * w / h)
        ny = (yc / -zc) / t
        return ((nx + 1) / 2 * w, (1 - ny) / 2 * h)


COL = {"Tracking": (60, 220, 60), "Coasting": (0, 190, 255), "Bridged": (255, 220, 0), "Lost": (0, 0, 255)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp"); ap.add_argument("events"); ap.add_argument("out")
    ap.add_argument("--label", default="")
    ap.add_argument("--match", default="/root/oevsrc/match.json")
    ap.add_argument("--sites")
    ap.add_argument("--start-s", type=float, default=0.0)
    ap.add_argument("--end-s", type=float, default=1e9)
    ap.add_argument("--width", type=int, default=1920)
    a = ap.parse_args()
    cam = Cam(json.load(open(a.match))["params"]["cameraAxisOffset"])

    det, ball, pose, ts = {}, {}, {}, {}
    for line in open(a.events):
        e = json.loads(line)
        i = e.get("frame_index")
        k = e["kind"]
        if k == "detections_raw":
            det[i] = [(d["position"]["yaw"], d["position"]["pitch"], d["confidence"])
                      for d in e["detections"] if d["class_id"] == 32 and d["position"]]
        elif k == "world_state":
            ball[i] = e["ball"]; ts[i] = e["timestamp_ms"]
        elif k == "pose_presented":
            pose[i] = e["pose"]
    sites = json.load(open(a.sites)) if a.sites else []  # [{frame, kind, yaw, pitch}]

    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", a.inp], capture_output=True, text=True)
    W, H = map(int, probe.stdout.strip().split(","))
    f0, f1 = int(round(a.start_s * FPS)), int(round(min(a.end_s, 1e7) * FPS))
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{f0 / FPS:.4f}", "-i", a.inp,
                            "-frames:v", str(max(1, f1 - f0)), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                           stdout=subprocess.PIPE)
    oh = int(round(H * a.width / W / 2) * 2)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                            "-s", f"{a.width}x{oh}", "-r", "60000/1001", "-i", "-", "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", a.out],
                           stdin=subprocess.PIPE)
    s = a.width / W
    i = f0
    active = []  # dormant sites (yaw,pitch) active at frame
    events = sorted(sites, key=lambda t: t["frame"])
    ei = 0
    while True:
        buf = dec.stdout.read(W * H * 3)
        if len(buf) < W * H * 3:
            break
        img = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
        if s != 1:
            img = cv2.resize(img, (a.width, oh), interpolation=cv2.INTER_AREA)
        while ei < len(events) and events[ei]["frame"] <= i:
            t = events[ei]
            key = (round(t["yaw"], 3), round(t["pitch"], 3))
            if t["kind"] == "dormant":
                active.append(key)
            elif key in active:
                active.remove(key)
            ei += 1
        p = pose.get(i)
        if p:
            fov = p["fov_degrees"] or 40.0
            B = cam.basis(p["yaw"], p["pitch"])
            pr = lambda y, pt: cam.project(B, fov, a.width, oh, y, pt)
            for (y, pt) in active:
                q = pr(y, pt)
                if q:
                    r = max(10, int(0.0175 / math.radians(fov) * oh))
                    cv2.circle(img, (int(q[0]), int(q[1])), r, (0, 0, 255), 2)
                    cv2.putText(img, "excluded", (int(q[0]) - 30, int(q[1]) - r - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1, cv2.LINE_AA)
            for (y, pt, c) in det.get(i, []):
                q = pr(y, pt)
                if q:
                    cv2.circle(img, (int(q[0]), int(q[1])), 7, (255, 255, 255), 1, cv2.LINE_AA)
            b = ball.get(i)
            state = b["state"] if b else "No ball"
            if b:
                q = pr(b["yaw"], b["pitch"])
                if q:
                    cv2.circle(img, (int(q[0]), int(q[1])), 16, COL.get(state, (200, 200, 200)), 3, cv2.LINE_AA)
            hdr = f"{a.label}   t={i / FPS:6.2f}s   tracker: {state}"
            cv2.rectangle(img, (0, 0), (a.width, 34), (0, 0, 0), -1)
            cv2.putText(img, hdr, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        enc.stdin.write(img.tobytes())
        i += 1
    enc.stdin.close(); enc.wait(); dec.wait()
    print(f"overlay frames {f0}..{i} -> {a.out}")


if __name__ == "__main__":
    main()
