"""PROVISIONAL visual A/B test: lens correction strength on one flatcam segment.

Scope: venue/mount-specific visual test (St Margaret's, mount 1) for GoPro Max 2
MSV footage. This is NOT a calibration and produces NO reusable camera profile.
It exists to answer one question by eye: does a mid-range
distortion_correction_strength (e.g. 0.3 / 0.5) look better than raw (0.0) on a
real followcam segment, without confounding dewarp quality with camera-path
movement?

Method
------
1. Trim the requested segment out of the source clip (ffmpeg re-encode, frame
   exact). Frozen code has no trim flags; trimming happens out here.
2. Compute the camera path ONCE by running the production renderer
   (flatcam/render_segment_flat.py) unmodified on the trimmed segment with the
   profile exactly as configured on disk (strength 0.0 == verified identity
   map, so the path CSV is in raw source coordinates). Its MP4 output IS the
   strength-0.0 baseline render.
3. For each non-zero strength: override the strength on the loaded profile
   dict IN MEMORY ONLY (lens_profiles.json is never modified), then replay the
   same segment. Scene framing is preserved, not raw pixel framing:
     - the exact dest->source mapping for that strength is obtained by passing
       coordinate-encoded float32 images through the public undistort_frame()
       (no formula duplication; exact by construction);
     - the raw-space crop's boundary extremes (left/right/top/bottom midpoints)
       are each inverted through that mapping. Horizontal scene span and centre
       are therefore EXACT by construction at every strength (a centre-point
       local-gradient approximation was measured to mis-size the span by up to
       -64% under this warp and was rejected). Height follows the fixed 16:9
       output aspect; its per-frame deviation from the true vertical scene span
       is logged as v_cover in the per-strength CSVs (stated limitation of a
       rectangular 16:9 crop over a non-conformal warp).
4. Per-frame transformed centres/scales are logged to camera_path_s{XXX}.csv
   next to the canonical raw-space camera_path.csv so residual framing
   differences are inspectable.
5. Wall-clock timings written to timings.json. Replay render/encode are
   interleaved in one loop (VideoWriter), so encode_s is null by design rather
   than restructuring frozen code to split it.

Frozen files (imported/executed, never edited): action_centroid.py,
follow_camera_flat.py, render_segment_flat.py, undistort.py,
lens_profiles.json, venue JSONs.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from render_segment_flat import crop_frame  # noqa: E402  (public helper, frozen file untouched)
from undistort import load_profile, undistort_frame  # noqa: E402

OUT_W, OUT_H = 1280, 720
LABEL_ORG = (32, 64)
LABEL_SCALE = 1.6
LABEL_THICK = 3


@dataclass
class ReplayState:
    """Attribute container matching what crop_frame() reads (cx, cy, crop_w, crop_h)."""

    cx: float
    cy: float
    crop_w: float
    crop_h: float


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-25:])
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{tail}")


def trim_segment(src: Path, dst: Path, start_sec: float, end_sec: float) -> None:
    _run([
        "ffmpeg", "-y", "-ss", f"{start_sec:.3f}", "-to", f"{end_sec:.3f}", "-i", str(src),
        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18", str(dst),
    ])


def compute_path_and_baseline(segment: Path, profile_name: str, venue: Path, outdir: Path) -> tuple[Path, Path, float]:
    """Run the production renderer unmodified: yields raw-space path CSV + strength-as-configured baseline."""
    baseline = outdir / "render_s000.mp4"
    path_csv = outdir / "camera_path.csv"
    t0 = time.perf_counter()
    _run([
        sys.executable, str(_HERE / "render_segment_flat.py"),
        "--input", str(segment), "--profile", profile_name, "--venue", str(venue),
        "--output", str(baseline), "--csv-out", str(path_csv),
    ])
    return path_csv, baseline, time.perf_counter() - t0


def load_path(path_csv: Path) -> list[dict]:
    with open(path_csv, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_mapping_fields(width: int, height: int, profile: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Exact dest->source coordinate fields for this profile, via public undistort_frame only.

    Returns (SX, SY, valid): at each undistorted/output pixel u, (SX[u], SY[u]) is the raw
    source coordinate it samples, and valid[u] flags pixels whose sample stayed in-frame
    (out-of-frame samples arrive as border zeros and must not enter the inversion search).
    """
    xs = np.tile(np.arange(width, dtype=np.float32), (height, 1))
    ys = np.tile(np.arange(height, dtype=np.float32).reshape(-1, 1), (1, width))
    ones = np.ones((height, width), dtype=np.float32)
    # Offset by +1 so a true border-fill zero is distinguishable from coordinate 0.
    sx = undistort_frame(xs + 1.0, profile)
    sy = undistort_frame(ys + 1.0, profile)
    validity = undistort_frame(ones, profile)
    valid = validity > 0.999
    return sx - 1.0, sy - 1.0, valid


def invert_point(sx: np.ndarray, sy: np.ndarray, valid: np.ndarray, px: float, py: float,
                 coarse_step: int = 8, refine_half: int = 12) -> tuple[float, float]:
    """Find the output pixel whose source sample is nearest (px, py). Coarse grid then local refine."""
    h, w = sx.shape
    cs_x = sx[::coarse_step, ::coarse_step]
    cs_y = sy[::coarse_step, ::coarse_step]
    cs_v = valid[::coarse_step, ::coarse_step]
    d2 = (cs_x - px) ** 2 + (cs_y - py) ** 2
    d2[~cs_v] = np.inf
    iy, ix = np.unravel_index(int(np.argmin(d2)), d2.shape)
    cy0, cx0 = iy * coarse_step, ix * coarse_step
    y1, y2 = max(0, cy0 - refine_half), min(h, cy0 + refine_half + 1)
    x1, x2 = max(0, cx0 - refine_half), min(w, cx0 + refine_half + 1)
    win_d2 = (sx[y1:y2, x1:x2] - px) ** 2 + (sy[y1:y2, x1:x2] - py) ** 2
    win_d2[~valid[y1:y2, x1:x2]] = np.inf
    wy, wx = np.unravel_index(int(np.argmin(win_d2)), win_d2.shape)
    return float(x1 + wx), float(y1 + wy)


def burn_label(img: np.ndarray, text: str) -> np.ndarray:
    cv2.putText(img, text, LABEL_ORG, cv2.FONT_HERSHEY_SIMPLEX, LABEL_SCALE, (0, 0, 0), LABEL_THICK + 3, cv2.LINE_AA)
    cv2.putText(img, text, LABEL_ORG, cv2.FONT_HERSHEY_SIMPLEX, LABEL_SCALE, (255, 255, 255), LABEL_THICK, cv2.LINE_AA)
    return img


def relabel_video(path: Path, text: str) -> float:
    """Rewrite a finished MP4 with a burned-in label (used for the production baseline)."""
    t0 = time.perf_counter()
    tmp = path.with_suffix(".labeled.mp4")
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (OUT_W, OUT_H))
    ok, frame = cap.read()
    while ok:
        writer.write(burn_label(frame, text))
        ok, frame = cap.read()
    writer.release(); cap.release()
    tmp.replace(path)
    return time.perf_counter() - t0


def replay_render(segment: Path, profile: dict, strength: float, path_rows: list[dict],
                  outdir: Path) -> tuple[Path, Path, float]:
    """Render the segment at `strength`, preserving per-frame scene target and horizontal scene FOV."""
    tag = f"s{int(round(strength * 100)):03d}"
    out_mp4 = outdir / f"render_{tag}.mp4"
    out_csv = outdir / f"camera_path_{tag}.csv"
    prof = dict(profile)
    prof["distortion_correction_strength"] = float(strength)  # in-memory only

    t0 = time.perf_counter()
    cap = cv2.VideoCapture(str(segment))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    ok, frame = cap.read()
    if not ok:
        raise RuntimeError("empty segment")
    h, w = frame.shape[:2]
    sx, sy, valid = build_mapping_fields(w, h, prof)

    writer = cv2.VideoWriter(str(out_mp4), cv2.VideoWriter_fourcc(*"mp4v"), fps, (OUT_W, OUT_H))
    label = f"strength {strength:.2f}"
    fields = ["frame_idx", "raw_cx", "raw_cy", "cx", "cy", "crop_w", "crop_h", "v_cover", "corner_err_px"]
    csv_fh = open(out_csv, "w", newline="", encoding="utf-8")
    csv_writer = csv.DictWriter(csv_fh, fieldnames=fields)
    csv_writer.writeheader()

    idx = 0
    n_rows = len(path_rows)
    while ok:
        row = path_rows[min(idx, n_rows - 1)]
        raw_cx, raw_cy = float(row["cx"]), float(row["cy"])
        base_cw, base_ch = float(row["crop_w"]), float(row["crop_h"])
        # Exact boundary inversion: the warp is too nonlinear for a centre-point
        # local gradient (measured up to -64% span error / +219 px centre bias at
        # s=0.3), so the raw crop's horizontal extremes and vertical extremes are
        # each inverted through the mapping directly. Horizontal span/centre are
        # exact by construction; height follows the 16:9 output requirement and
        # its deviation from the true vertical scene span is logged as v_cover.
        ul = invert_point(sx, sy, valid, raw_cx - base_cw / 2.0, raw_cy)
        ur = invert_point(sx, sy, valid, raw_cx + base_cw / 2.0, raw_cy)
        ut = invert_point(sx, sy, valid, raw_cx, raw_cy - base_ch / 2.0)
        ub = invert_point(sx, sy, valid, raw_cx, raw_cy + base_ch / 2.0)
        cw = min(float(w), max(16.0, ur[0] - ul[0]))
        ch = min(float(h), cw * 9.0 / 16.0)
        ux = (ul[0] + ur[0]) / 2.0
        uy = (ut[1] + ub[1]) / 2.0
        true_vspan = max(1e-6, ub[1] - ut[1])
        v_cover = ch / true_vspan
        # Corner check (edge-midpoint inversion does not prove the rectangle holds
        # at the corners under radial warping): invert the raw box's four actual
        # corners and compare each to the nearest corner of the assumed output
        # rectangle (ux,uy,cw,ch). Worst-case distance across the four is logged;
        # this is a diagnostic, not a silent correction — no gate is auto-applied.
        raw_corners = [
            (raw_cx - base_cw / 2.0, raw_cy - base_ch / 2.0),
            (raw_cx + base_cw / 2.0, raw_cy - base_ch / 2.0),
            (raw_cx - base_cw / 2.0, raw_cy + base_ch / 2.0),
            (raw_cx + base_cw / 2.0, raw_cy + base_ch / 2.0),
        ]
        true_corners = [invert_point(sx, sy, valid, cx_, cy_) for cx_, cy_ in raw_corners]
        assumed_corners = [
            (ux - cw / 2.0, uy - ch / 2.0), (ux + cw / 2.0, uy - ch / 2.0),
            (ux - cw / 2.0, uy + ch / 2.0), (ux + cw / 2.0, uy + ch / 2.0),
        ]
        corner_err = max(
            ((tc[0] - ac[0]) ** 2 + (tc[1] - ac[1]) ** 2) ** 0.5
            for tc, ac in zip(true_corners, assumed_corners)
        )
        und = undistort_frame(frame, prof)
        out = crop_frame(und, ReplayState(ux, uy, cw, ch), OUT_W, OUT_H)
        writer.write(burn_label(out, label))
        csv_writer.writerow({"frame_idx": idx, "raw_cx": f"{raw_cx:.2f}", "raw_cy": f"{raw_cy:.2f}",
                             "cx": f"{ux:.2f}", "cy": f"{uy:.2f}",
                             "crop_w": f"{cw:.1f}", "crop_h": f"{ch:.1f}", "v_cover": f"{v_cover:.4f}",
                             "corner_err_px": f"{corner_err:.2f}"})
        ok, frame = cap.read()
        idx += 1

    csv_fh.close(); writer.release(); cap.release()
    return out_mp4, out_csv, time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--venue", required=True)
    ap.add_argument("--start-sec", type=float, default=115.0)
    ap.add_argument("--end-sec", type=float, default=145.0)
    ap.add_argument("--strengths", default="0.0,0.3,0.5")
    ap.add_argument("--outdir", default="out")
    args = ap.parse_args()

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    strengths = [float(s) for s in args.strengths.split(",") if s.strip() != ""]
    if any(s < 0.0 or s > 1.0 for s in strengths):
        raise SystemExit("strengths must be within [0.0, 1.0]")

    profile = load_profile(args.profile)
    configured = float(profile["distortion_correction_strength"])
    if abs(configured) > 1e-9:
        # Path CSV is only raw-space canonical while the configured profile is the identity map.
        raise SystemExit(
            f"profile {args.profile} has on-disk strength {configured}; this test assumes 0.0 "
            "(raw-space path). Aborting rather than silently changing semantics.")

    segment = outdir / "segment.mp4"
    t_trim0 = time.perf_counter()
    trim_segment(Path(args.input), segment, args.start_sec, args.end_sec)
    trim_s = time.perf_counter() - t_trim0

    path_csv, baseline_mp4, path_compute_s = compute_path_and_baseline(
        segment, args.profile, Path(args.venue), outdir)
    path_rows = load_path(path_csv)

    renders: list[dict] = []
    label_pass_s = 0.0
    for s in sorted(set(strengths)):
        if abs(s) < 1e-9:
            label_pass_s += relabel_video(baseline_mp4, "strength 0.0")
            renders.append({"strength": 0.0, "render_s": None, "encode_s": None,
                            "note": "rendered by production renderer during path compute; relabel only"})
            continue
        _, _, render_s = replay_render(segment, profile, s, path_rows, outdir)
        renders.append({"strength": s, "render_s": round(render_s, 2), "encode_s": None})

    timings = {
        "segment_sec": round(args.end_sec - args.start_sec, 2),
        "trim_s": round(trim_s, 2),
        "path_compute_s": round(path_compute_s, 2),
        "label_pass_s": round(label_pass_s, 2),
        "renders": renders,
        "notes": "path_compute_s includes decode+detector+FSM+baseline render (production renderer, "
                 "one pass). Replay render/encode interleaved; encode_s null by design.",
    }
    with open(outdir / "timings.json", "w", encoding="utf-8") as fh:
        json.dump(timings, fh, indent=2)
    segment.unlink(missing_ok=True)  # keep the artifact lean; renders + CSVs + timings only
    print(json.dumps(timings, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
