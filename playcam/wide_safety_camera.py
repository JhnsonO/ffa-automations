#!/usr/bin/env python3
"""
playcam/wide_safety_camera.py

Phase 2.5 -- adds a two-mode camera behaviour on top of Phase 2's smoothing:

  play-follow: clear dense activity -> pan toward person_centroid_yaw at
               the venue's normal FOV.
  wide-safety: no clear cluster (players spread out, low confidence,
               restart) -> hold the venue's known pitch-centre shot at a
               wider FOV, showing most/all of the pitch, until a clear
               active cluster returns.

Mode switching uses hysteresis (sustained concentration for
--hysteresis-sec before flipping either direction) so it doesn't flicker.
FOV eases under its own speed/accel caps, same mechanism as yaw.

Reuses Phase 2's yaw hold/deadband/interpolate/ease machinery (imported,
not duplicated). The existing yaw-only single-mode path stays available
via smooth_camera_path.py directly, or here via --baseline (which forces
mode="follow" always, i.e. no wide-safety fallback -- useful for comparing
old vs new behaviour on the same clip).

No ball_tracker/ imports or edits.

Inputs (from play_location.jsonl):
  person_centroid_yaw, person_centroid_size, person_centroid_dispersion_deg,
  total_retained_players
Inputs (from venue profile):
  camera.pitch / top-level pitch, camera.fov / top-level fov (follow-mode FOV)
  wide_fallback.yaw, wide_fallback.fov (wide-mode shot)

Usage:
  python3 playcam/wide_safety_camera.py \
      --input playcam/output/play_location.jsonl \
      --venue-profile playcam/venue_profiles/st_margarets.json \
      --output playcam/output/wide_safety_timeline.jsonl

  # Baseline (old yaw-only, always-follow) comparison on the same input:
  python3 playcam/wide_safety_camera.py \
      --input playcam/output/play_location.jsonl \
      --venue-profile playcam/venue_profiles/st_margarets.json \
      --output playcam/output/baseline_timeline.jsonl --baseline
"""

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from crop_utils import extract_crop_frame
from smooth_camera_path import (  # noqa: E402  -- reuse, not duplicate
    unwrap_degrees, ease_kinematic,
    DEFAULT_RENDER_FPS, DEFAULT_MAX_PAN_SPEED_DEG_S, DEFAULT_MAX_PAN_ACCEL_DEG_S2,
    DEFAULT_SPRING_STIFFNESS,
)

DEFAULT_FOLLOW_FOV = 85.0
DEFAULT_WIDE_FOV = 100.0   # placeholder -- needs a render test to confirm 95-105 range
DEFAULT_PITCH = 4.0

# Concentration score: density (cluster fraction of all retained players)
# times tightness (exponential falloff on dispersion). Range ~0-1.
#
# CALIBRATION NOTE (2026-07-03): DISPERSION_SCALE_DEG=20 (original guess) was
# too aggressive -- on a real 30s clip of continuous normal small-sided play
# (density 0.56-1.00, dispersion 14-33 deg throughout), it never produced a
# score above 0.33, so follow mode never triggered even during clearly
# clustered play. Rescaled to 40 so real dense-play frames can cross the
# strong threshold. BUT: that same clip never contained a genuine restart/
# spread-out moment, so the WEAK end of this scale is still unvalidated
# against real data -- only synthetic. Re-test against a clip that actually
# contains a kickoff/throw-in/goal-kick before trusting the wide-mode trigger.
DISPERSION_SCALE_DEG = 40.0
CONCENTRATION_STRONG_THRESHOLD = 0.45   # enter/stay in follow mode above this
CONCENTRATION_WEAK_THRESHOLD = 0.30     # enter/stay in wide mode below this -- UNVALIDATED on real spread data
DEFAULT_HYSTERESIS_SEC = 1.5           # must be sustained this long to flip mode

DEFAULT_FOV_MAX_SPEED = 15.0   # deg/s widen/narrow rate
DEFAULT_FOV_MAX_ACCEL = 30.0   # deg/s^2

# Wide-follow (2026-07-04, 3B.8 fix): wide mode no longer centre-locks yaw to
# venue["wide_yaw"]. It slowly pursues the current cluster_yaw (same signal
# follow mode uses) so a sustained off-centre attack doesn't get clipped by a
# fixed-centre wide shot. Deliberately much slower than follow's pan cap
# (DEFAULT_MAX_PAN_SPEED_DEG_S=25) and range-clamped around the venue's known
# wide-shot centre so it can drift toward real play but cannot chase a single
# noisy detection or wrap to a nonsensical yaw. Concentration score,
# hysteresis thresholds, and follow-mode logic are untouched by this change.
WIDE_YAW_MAX_SPEED_DEG_S = 10.0   # deg/s -- conservative pursuit rate, per spec
WIDE_YAW_RANGE_DEG = 45.0         # clamp: venue wide_yaw +/- this range


def parse_args():
    p = argparse.ArgumentParser(description="Phase 2.5 -- wide-safety / play-follow camera")
    p.add_argument("--input", required=True, type=Path, help="play_location.jsonl (Phase 1 output)")
    p.add_argument("--venue-profile", required=True, type=Path)
    p.add_argument("--output", type=Path, default=Path("playcam/output/wide_safety_timeline.jsonl"))
    p.add_argument("--render-fps", type=float, default=DEFAULT_RENDER_FPS)
    p.add_argument("--hysteresis-sec", type=float, default=DEFAULT_HYSTERESIS_SEC)
    p.add_argument("--strong-threshold", type=float, default=CONCENTRATION_STRONG_THRESHOLD)
    p.add_argument("--weak-threshold", type=float, default=CONCENTRATION_WEAK_THRESHOLD)
    p.add_argument("--max-pan-speed", type=float, default=DEFAULT_MAX_PAN_SPEED_DEG_S)
    p.add_argument("--max-pan-accel", type=float, default=DEFAULT_MAX_PAN_ACCEL_DEG_S2)
    p.add_argument("--fov-max-speed", type=float, default=DEFAULT_FOV_MAX_SPEED)
    p.add_argument("--fov-max-accel", type=float, default=DEFAULT_FOV_MAX_ACCEL)
    p.add_argument("--wide-yaw-max-speed", type=float, default=WIDE_YAW_MAX_SPEED_DEG_S,
                    help="Wide-mode yaw pursuit rate cap, deg/s "
                         f"(default {WIDE_YAW_MAX_SPEED_DEG_S})")
    p.add_argument("--wide-yaw-range", type=float, default=WIDE_YAW_RANGE_DEG,
                    help="Wide-mode yaw clamp range around venue wide_yaw, +/- deg "
                         f"(default {WIDE_YAW_RANGE_DEG})")
    p.add_argument("--spring-stiffness", type=float, default=DEFAULT_SPRING_STIFFNESS)
    p.add_argument("--baseline", action="store_true",
                    help="Force mode=follow always (no wide-safety fallback) -- "
                         "for comparing old yaw-only behaviour against this on the same clip")
    p.add_argument("--yaw-source-csv", type=Path, default=None,
                    help="Optional action_zone.py comparison CSV (needs timestamp,target_yaw "
                         "columns). When supplied, cluster_yaw is overridden by its target_yaw "
                         "(nearest timestamp match) before mode/FOV/hysteresis logic runs -- "
                         "that logic is otherwise unchanged. Omit for current centroid behaviour.")

    p.add_argument("--render", action="store_true",
                    help="Also render a clean video (per-frame varying yaw+fov, "
                         "fixed pitch, original audio)")
    p.add_argument("--source-video", type=Path, default=None,
                    help="Required with --render")
    p.add_argument("--render-start", type=float, default=0.0,
                    help="Global timeline time --source-video's frame 0 corresponds to")
    p.add_argument("--render-duration", type=float, default=None)
    p.add_argument("--render-output", type=Path, default=Path("playcam/output/wide_safety_render.mp4"))
    p.add_argument("--plot-output", type=Path, default=Path("playcam/output/mode_timeline.png"),
                    help="Set to empty/skip via --no-plot")
    p.add_argument("--no-plot", action="store_true")
    return p.parse_args()


def load_venue_profile(path):
    profile = json.loads(path.read_text())
    pitch = profile.get("camera", {}).get("pitch", profile.get("pitch", DEFAULT_PITCH))
    follow_fov = profile.get("camera", {}).get("fov", profile.get("fov", DEFAULT_FOLLOW_FOV))
    wide = profile.get("wide_fallback", {})
    wide_yaw = wide.get("yaw", 0.0)
    wide_fov = wide.get("fov", DEFAULT_WIDE_FOV)
    return {"pitch": pitch, "follow_fov": follow_fov, "wide_yaw": wide_yaw, "wide_fov": wide_fov}


def concentration_score(cluster_size, total_players, dispersion_deg):
    if not total_players or not cluster_size:
        return 0.0
    density = cluster_size / total_players
    tightness = math.exp(-dispersion_deg / DISPERSION_SCALE_DEG) if dispersion_deg is not None else 0.0
    return max(0.0, min(1.0, density * tightness))


def load_yaw_source_csv(path):
    """Load an action_zone.py comparison CSV's (timestamp, target_yaw) pairs,
    sorted for nearest-timestamp lookup. Analysis-only input; unrelated to
    ball_tracker, venue mask, or FSM logic -- it only supplies an alternate
    yaw number at each sparse timestamp."""
    import csv as csv_mod
    rows = []
    with open(path) as f:
        for rec in csv_mod.DictReader(f):
            rows.append((float(rec["timestamp"]), float(rec["target_yaw"])))
    rows.sort()
    return rows


def nearest_yaw(rows, t):
    if not rows:
        return None
    lo, hi = 0, len(rows) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    best = rows[lo]
    if lo > 0 and abs(rows[lo - 1][0] - t) < abs(best[0] - t):
        best = rows[lo - 1]
    return best[1]


def load_sparse_records(input_path, yaw_overrides=None):
    """Read the fields Phase 2.5 needs from play_location.jsonl.

    yaw_overrides: optional sorted list of (timestamp, target_yaw) from
    load_yaw_source_csv(). When given, cluster_yaw is replaced by the
    nearest-timestamp override value; default (None) is the unchanged
    person_centroid_yaw path.
    """
    out = []
    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t = rec["timestamp"]
            cluster_yaw = rec.get("person_centroid_yaw")
            if yaw_overrides is not None:
                cluster_yaw = nearest_yaw(yaw_overrides, t)
            out.append({
                "timestamp": t,
                "cluster_yaw": cluster_yaw,
                "cluster_size": rec.get("person_centroid_size", 0),
                "dispersion": rec.get("person_centroid_dispersion_deg"),
                "total_players": rec.get("total_retained_players", 0),
            })
    return out


def run_hysteresis(records, venue, strong_thresh, weak_thresh, hysteresis_sec, baseline,
                    wide_yaw_max_speed=WIDE_YAW_MAX_SPEED_DEG_S,
                    wide_yaw_range=WIDE_YAW_RANGE_DEG):
    """
    Sparse-rate mode decision with hysteresis. Returns list of
    (timestamp, mode, score, target_yaw, target_fov).
    """
    out = []
    mode = "follow" if baseline else "wide"
    strong_streak = 0.0
    weak_streak = 0.0
    prev_t = None
    # Wide-follow pursuit state (3B.8 fix): starts at the venue's known
    # wide-shot centre and is kept in sync with the current yaw whenever
    # follow mode is active, so a follow->wide flip resumes the slow pursuit
    # from wherever the camera actually is, not a stale earlier position.
    wide_yaw_state = venue["wide_yaw"]

    for rec in records:
        t = rec["timestamp"]
        dt = 0.0 if prev_t is None else (t - prev_t)
        prev_t = t

        score = concentration_score(rec["cluster_size"], rec["total_players"], rec["dispersion"])

        if not baseline:
            if score >= strong_thresh:
                strong_streak += dt
                weak_streak = 0.0
            elif score <= weak_thresh:
                weak_streak += dt
                strong_streak = 0.0
            else:
                strong_streak = 0.0
                weak_streak = 0.0

            if mode == "wide" and strong_streak >= hysteresis_sec:
                mode = "follow"
            elif mode == "follow" and weak_streak >= hysteresis_sec:
                mode = "wide"

        if mode == "follow" and rec["cluster_yaw"] is not None:
            target_yaw = rec["cluster_yaw"]
            target_fov = venue["follow_fov"]
            # Keep wide-follow state synced while in follow, so a later
            # follow->wide flip resumes pursuit from here, not a stale spot.
            wide_yaw_state = target_yaw
        else:
            # No stable cluster yaw available, or in wide mode: keep FOV at
            # the venue's known wide shot, but (3B.8 fix) no longer lock yaw
            # to a fixed centre. Slowly pursue the current cluster_yaw --
            # rate-capped at wide_yaw_max_speed and clamped to
            # venue["wide_yaw"] +/- wide_yaw_range -- so a sustained
            # off-centre attack isn't clipped, but a single noisy detection
            # or a genuine "no idea where play is" moment can't drag the
            # wide shot somewhere nonsensical. Per spec: play-follow always
            # resumes toward the CURRENT cluster yaw the instant mode flips
            # to follow -- unaffected by this change.
            raw_center = rec["cluster_yaw"] if rec["cluster_yaw"] is not None else venue["wide_yaw"]
            clamped_center = max(venue["wide_yaw"] - wide_yaw_range,
                                  min(venue["wide_yaw"] + wide_yaw_range, raw_center))
            diff = clamped_center - wide_yaw_state
            max_step = wide_yaw_max_speed * dt
            step = max(-max_step, min(max_step, diff))
            wide_yaw_state += step
            target_yaw = wide_yaw_state
            target_fov = venue["wide_fov"]

        out.append((t, mode, round(score, 3), target_yaw, target_fov))

    return out


def interpolate_dense_generic(samples, render_fps, total_duration, circular):
    """
    samples: list of (timestamp, value). Linear interpolation to dense
    render-frame rate. circular=True unwraps before interpolating (for yaw);
    circular=False interpolates directly (for FOV, which isn't circular).
    Before first / after last sample: hold that value.
    """
    if not samples:
        return []
    ts = [s[0] for s in samples]
    vals = [s[1] for s in samples]
    if circular:
        vals = unwrap_degrees(vals)

    dt = 1.0 / render_fps
    n_frames = int(math.ceil(total_duration / dt)) + 1
    dense = []
    j = 0
    for k in range(n_frames):
        t = k * dt
        if t <= ts[0]:
            val = vals[0]
        elif t >= ts[-1]:
            val = vals[-1]
        else:
            while j + 1 < len(ts) and ts[j + 1] < t:
                j += 1
            t0, t1 = ts[j], ts[j + 1]
            v0, v1 = vals[j], vals[j + 1]
            frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            val = v0 + frac * (v1 - v0)
        if circular:
            val = ((val + 180) % 360) - 180
        dense.append((t, val))
    return dense


def nearest_earlier_categorical(samples, dense_ts):
    """For a categorical field (mode), carry forward the most recent sparse
    sample's value at each dense timestamp -- no interpolation of a string."""
    ts = [s[0] for s in samples]
    vals = [s[1] for s in samples]
    out = []
    j = 0
    for t in dense_ts:
        while j + 1 < len(ts) and ts[j + 1] <= t:
            j += 1
        out.append(vals[j] if ts[j] <= t else vals[0])
    return out


def render_wide_safety(records, source_video, clean_start, clean_duration, out_path):
    """
    Render the wide-safety timeline: yaw AND fov vary per frame (pitch
    fixed). Mirrors smooth_camera_path.py's render_clean but with per-frame
    fov instead of a constant -- extract_crop_frame already accepts fov_deg
    per call, so this is a straightforward extension.
    """
    import cv2
    import subprocess

    cap = cv2.VideoCapture(str(source_video))
    if not cap.isOpened():
        print(f"ERROR: cannot open --source-video: {source_video}", file=sys.stderr)
        sys.exit(1)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    src_duration = src_total_frames / src_fps

    duration = clean_duration if clean_duration is not None else src_duration
    duration = min(duration, src_duration)

    frames = [r for r in records if clean_start <= r["timestamp"] < clean_start + duration]
    if not frames:
        print(f"ERROR: no timeline entries in window "
              f"[{clean_start}, {clean_start + duration})", file=sys.stderr)
        sys.exit(1)

    out_w, out_h = 1920, 1080
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    tmp_path = out_path.with_suffix(".tmp.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(tmp_path), fourcc, src_fps, (out_w, out_h))

    print(f"[render_wide_safety] {len(frames)} frames, window "
          f"[{clean_start:.1f}, {clean_start + duration:.1f}) -> {out_path}")

    total = len(frames)
    progress_interval = max(1, total // 20)
    src_frame = None
    src_idx = -1
    written = 0
    for r in frames:
        local_t = r["timestamp"] - clean_start
        target_src_idx = int(round(local_t * src_fps))
        while src_idx < target_src_idx:
            ret, f = cap.read()
            if not ret:
                break
            src_idx += 1
            src_frame = f
        if src_frame is None:
            continue
        crop = extract_crop_frame(src_frame, r["smoothed_yaw"], pitch_deg=r["pitch"],
                                   fov_deg=r["smoothed_fov"], out_w=out_w, out_h=out_h)
        writer.write(crop)
        written += 1
        if written % progress_interval == 0 or written == total:
            print(f"[render_wide_safety] {written}/{total} frames ({written/total*100:.0f}%)", flush=True)

    writer.release()
    cap.release()

    if written == 0:
        print("ERROR: no frames written", file=sys.stderr)
        tmp_path.unlink(missing_ok=True)
        sys.exit(1)

    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(tmp_path),
         "-ss", "0", "-i", str(source_video),
         "-t", str(duration),
         "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", "mpeg4", "-q:v", "3", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k",
         "-shortest", str(out_path)],
        capture_output=True, text=True)
    tmp_path.unlink(missing_ok=True)
    if result.returncode != 0:
        print("ERROR: ffmpeg mux failed:", result.stderr[-2000:], file=sys.stderr)
        sys.exit(1)

    print(f"[render_wide_safety] Done -> {out_path} ({written} frames)")


def plot_mode_timeline(records, out_path):
    """Plot mode, concentration_score, smoothed_yaw, smoothed_fov across
    the full timeline -- the visual record of when/why the camera switched."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ts = [r["timestamp"] for r in records]
    score = [r["concentration_score"] for r in records]
    yaw = [r["smoothed_yaw"] for r in records]
    fov = [r["smoothed_fov"] for r in records]
    mode_num = [1 if r["mode"] == "follow" else 0 for r in records]

    fig, axes = plt.subplots(3, 1, figsize=(14, 8), sharex=True)

    axes[0].plot(ts, mode_num, color="tab:purple", drawstyle="steps-post", linewidth=1.5)
    axes[0].fill_between(ts, mode_num, step="post", alpha=0.2, color="tab:purple")
    axes[0].set_ylabel("mode")
    axes[0].set_yticks([0, 1])
    axes[0].set_yticklabels(["wide", "follow"])
    axes[0].plot(ts, score, color="tab:orange", alpha=0.6, linewidth=1, label="concentration_score")
    axes[0].legend(loc="upper right", fontsize=8)
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(ts, yaw, color="tab:blue", linewidth=1.2)
    axes[1].set_ylabel("smoothed_yaw (deg)")
    axes[1].grid(True, alpha=0.3)

    axes[2].plot(ts, fov, color="tab:green", linewidth=1.2)
    axes[2].set_ylabel("smoothed_fov (deg)")
    axes[2].set_xlabel("time (s)")
    axes[2].grid(True, alpha=0.3)

    fig.suptitle("Wide-safety camera: mode / concentration / yaw / fov over time")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)




def main():
    args = parse_args()

    if not args.input.exists():
        print(f"ERROR: --input does not exist: {args.input}", file=sys.stderr)
        sys.exit(1)
    if not args.venue_profile.exists():
        print(f"ERROR: --venue-profile does not exist: {args.venue_profile}", file=sys.stderr)
        sys.exit(1)

    venue = load_venue_profile(args.venue_profile)
    yaw_overrides = None
    if args.yaw_source_csv is not None:
        if not args.yaw_source_csv.exists():
            print(f"ERROR: --yaw-source-csv does not exist: {args.yaw_source_csv}", file=sys.stderr)
            sys.exit(1)
        yaw_overrides = load_yaw_source_csv(args.yaw_source_csv)
    records = load_sparse_records(args.input, yaw_overrides)
    if not records:
        print("ERROR: no records in input", file=sys.stderr)
        sys.exit(1)

    decisions = run_hysteresis(records, venue, args.strong_threshold, args.weak_threshold,
                                args.hysteresis_sec, args.baseline,
                                wide_yaw_max_speed=args.wide_yaw_max_speed,
                                wide_yaw_range=args.wide_yaw_range)

    total_duration = records[-1]["timestamp"]
    yaw_samples = [(t, ty) for t, _m, _s, ty, _tf in decisions]
    fov_samples = [(t, tf) for t, _m, _s, _ty, tf in decisions]
    mode_samples = [(t, m) for t, m, _s, _ty, _tf in decisions]
    score_samples = [(t, s) for t, _m, s, _ty, _tf in decisions]

    dense_yaw = interpolate_dense_generic(yaw_samples, args.render_fps, total_duration, circular=True)
    dense_fov = interpolate_dense_generic(fov_samples, args.render_fps, total_duration, circular=False)
    dense_ts = [t for t, _ in dense_yaw]
    dense_mode = nearest_earlier_categorical(mode_samples, dense_ts)
    dense_score = interpolate_dense_generic(score_samples, args.render_fps, total_duration, circular=False)

    eased_yaw = ease_kinematic(dense_yaw, args.render_fps, args.max_pan_speed,
                                args.max_pan_accel, args.spring_stiffness)
    eased_fov = ease_kinematic(dense_fov, args.render_fps, args.fov_max_speed,
                                args.fov_max_accel, args.spring_stiffness)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        for i in range(len(dense_ts)):
            t = dense_ts[i]
            rec = {
                "timestamp": round(t, 4),
                "mode": dense_mode[i],
                "concentration_score": round(dense_score[i][1], 3),
                "target_yaw": round(dense_yaw[i][1], 2),
                "smoothed_yaw": round(eased_yaw[i][1], 2),
                "target_fov": round(dense_fov[i][1], 2),
                "smoothed_fov": round(eased_fov[i][1], 2),
                "pitch": venue["pitch"],
            }
            f.write(json.dumps(rec) + "\n")

    mode_changes = sum(1 for i in range(1, len(dense_mode)) if dense_mode[i] != dense_mode[i - 1])
    wide_frac = sum(1 for m in dense_mode if m == "wide") / len(dense_mode)
    print(f"[wide_safety_camera] {len(dense_ts)} dense frames -> {args.output} "
          f"({'BASELINE (follow-only)' if args.baseline else 'wide-safety enabled'}, "
          f"yaw source: {'action_zone CSV (' + str(args.yaw_source_csv) + ')' if yaw_overrides is not None else 'person_centroid_yaw'})")
    print(f"[wide_safety_camera] mode changes: {mode_changes}, wide-mode fraction: {wide_frac:.1%}")
    max_fov_vel = max(abs(v) for _, _, v, _ in eased_fov) if eased_fov else 0.0
    print(f"[wide_safety_camera] Peak FOV velocity: {max_fov_vel:.1f} deg/s (cap: {args.fov_max_speed})")

    if args.render:
        if args.source_video is None:
            print("ERROR: --render requires --source-video", file=sys.stderr)
            sys.exit(1)
        out_records = [json.loads(l) for l in open(args.output)]
        render_wide_safety(out_records, args.source_video, args.render_start,
                            args.render_duration, args.render_output)

    if not args.no_plot:
        out_records = [json.loads(l) for l in open(args.output)]
        plot_mode_timeline(out_records, args.plot_output)
        print(f"[wide_safety_camera] Mode timeline plot -> {args.plot_output}")


if __name__ == "__main__":
    main()
