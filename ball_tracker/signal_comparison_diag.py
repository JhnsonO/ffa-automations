#!/usr/bin/env python3
"""
FFA Signal Comparison Diagnostic
==================================
Reads tracking.json and activity_700_1300.json and produces a multi-panel
time-series chart showing — per frame — every signal that could drive the
camera during the fallback window (frames 700–1300):

  Panel 1 — YAW signals:
    • Ball-tracker smoothed yaw  (blue, solid)
    • Activity cluster_centre yaw (orange, dashed — only where conf >= 0.5)
    • Activity cluster_centre yaw (orange, dotted — all frames with cluster)
    • Fixed fallback yaw = 0° (grey, dotted reference)
    • v7 activity-EMA yaw  (red, solid — what v7 actually sent to camera)

  Panel 2 — PITCH signals:
    • Ball-tracker smoothed pitch (blue, solid)
    • Activity cluster_centre pitch (orange, dashed — only conf >= 0.5)
    • Fixed fallback pitch = 5°  (grey, dotted reference)
    • v7 camera pitch (should be fixed at 5° — shows if it drifted)

  Panel 3 — Confidence & camera mode:
    • Activity confidence (purple, left axis)
    • conf >= 0.5 threshold line (red dashed)
    • Tracker best_score (blue shaded, normalised, right axis)
    • Colour-banded background: FOLLOW / ZOOMING_OUT / WIDE_HOLD

  Panel 4 — Delta: activity yaw vs fixed fallback yaw:
    • Shows exactly how far the activity bias would pull camera from safe overview
    • Annotates peak deviation frames

Usage:
  python3 signal_comparison_diag.py \\
      --tracking   /path/to/tracking.json \\
      --activity   /path/to/activity_700_1300.json \\
      --start      700 \\
      --end        1300 \\
      --output     signal_comparison.png
"""

import argparse
import json
import math
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ── Constants matching v6/v7 renderer ─────────────────────────────────────────
FALLBACK_YAW           = 0.0
FALLBACK_PITCH         = 5.0
ACTIVITY_CONF_THRESH   = 0.5
ACTIVITY_EMA_ALPHA     = 0.04
HOLD_BEFORE_ZOOM       = 15
FALLBACK_ZOOM_FRAMES   = 45
REACQUIRE_MIN_FRAMES   = 5
EMA_ALPHA_TRACKING     = 0.25
EMA_ALPHA_LOSS         = 0.05

# ── FSM state labels (to shade background) ────────────────────────────────────
FOLLOW      = "FOLLOW"
ZOOM_OUT    = "ZOOMING_OUT"
WIDE_HOLD   = "WIDE_HOLD"
ZOOM_IN     = "ZOOMING_IN"

FSM_COLOURS = {
    FOLLOW:    "#d4edda",   # green tint
    ZOOM_OUT:  "#fff3cd",   # amber tint
    WIDE_HOLD: "#d1ecf1",   # blue tint
    ZOOM_IN:   "#fce4ec",   # pink tint
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def lerp(a, b, t):
    return a + (b - a) * t

def lerp_yaw(a, b, t):
    diff = (b - a + 540) % 360 - 180
    return a + diff * t

def ease_inout(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


# ── Simulate v6 renderer EMA + FSM ────────────────────────────────────────────
def simulate_v6(frames_data, start_frame, end_frame):
    """
    Replays the v6 render loop for frames start_frame..end_frame.
    Returns per-frame dict with all signals.
    """
    ema_yaw = ema_pitch = ema_yaw_ref = None
    prev_best = None

    # FSM state
    fsm_mode         = FOLLOW
    hold_counter     = 0
    reacquire_streak = 0
    zoom_t           = 0.0
    zoom_start_yaw   = 0.0
    zoom_start_pitch = 0.0
    zoom_start_fov   = 90.0
    zoom_start_roll  = 0.0

    dt_out = 1.0 / FALLBACK_ZOOM_FRAMES
    dt_in  = 1.0 / FALLBACK_ZOOM_FRAMES

    records = []

    for frame_idx in range(start_frame, end_frame):
        fd = frames_data[frame_idx] if frame_idx < len(frames_data) else {}
        smoothed    = fd.get("smoothed") or {}
        ball_yaw    = smoothed.get("yaw",   0.0)
        ball_pitch  = smoothed.get("pitch", 0.0)
        best_score  = fd.get("best_score")
        tracker_state = fd.get("tracker_state", "")

        confirmed = (best_score is not None)
        alpha = EMA_ALPHA_TRACKING if confirmed else EMA_ALPHA_LOSS

        if ema_yaw is None:
            ema_yaw = ball_yaw
            ema_pitch = ball_pitch
            ema_yaw_ref = ball_yaw
        else:
            was_confirmed = (prev_best is not None)
            if confirmed and not was_confirmed:
                ema_yaw = ball_yaw
                ema_pitch = ball_pitch
                ema_yaw_ref = ball_yaw
            else:
                dyaw = ball_yaw - ema_yaw_ref
                if dyaw > 180:    ball_yaw -= 360
                elif dyaw < -180: ball_yaw += 360
                ema_yaw_ref = ball_yaw
                ema_yaw   = alpha * ball_yaw   + (1 - alpha) * ema_yaw
                ema_pitch = alpha * ball_pitch + (1 - alpha) * ema_pitch

        prev_best = best_score

        # ── FSM ──────────────────────────────────────────────────────────────
        def _interp(t, wy, wp):
            et = ease_inout(t)
            y  = lerp_yaw(zoom_start_yaw,   wy, et)
            p  = lerp(zoom_start_pitch, wp, et)
            return y, p

        if fsm_mode == FOLLOW:
            cam_yaw, cam_pitch = ema_yaw, ema_pitch
            if confirmed:
                hold_counter     = 0
                reacquire_streak = 0
                zoom_t           = 0.0
            else:
                hold_counter += 1
                if hold_counter >= HOLD_BEFORE_ZOOM:
                    fsm_mode         = ZOOM_OUT
                    zoom_t           = 0.0
                    zoom_start_yaw   = ema_yaw
                    zoom_start_pitch = ema_pitch
                    reacquire_streak = 0

        elif fsm_mode == ZOOM_OUT:
            zoom_t = min(1.0, zoom_t + dt_out)
            cam_yaw, cam_pitch = _interp(zoom_t, FALLBACK_YAW, FALLBACK_PITCH)
            if confirmed:
                reacquire_streak += 1
            else:
                reacquire_streak = 0
            if zoom_t >= 1.0:
                fsm_mode = WIDE_HOLD

        elif fsm_mode == WIDE_HOLD:
            cam_yaw, cam_pitch = FALLBACK_YAW, FALLBACK_PITCH
            if confirmed:
                reacquire_streak += 1
                if reacquire_streak >= REACQUIRE_MIN_FRAMES:
                    fsm_mode         = ZOOM_IN
                    zoom_start_yaw   = cam_yaw
                    zoom_start_pitch = cam_pitch
            else:
                reacquire_streak = 0

        elif fsm_mode == ZOOM_IN:
            if confirmed:
                reacquire_streak += 1
                zoom_t = max(0.0, zoom_t - dt_in)
                et = ease_inout(zoom_t)
                cam_yaw   = lerp_yaw(ema_yaw, zoom_start_yaw, et)
                cam_pitch = lerp(ema_pitch, zoom_start_pitch, et)
                if zoom_t <= 0.0:
                    fsm_mode         = FOLLOW
                    hold_counter     = 0
                    reacquire_streak = 0
                    zoom_t           = 0.0
                    cam_yaw          = ema_yaw
                    cam_pitch        = ema_pitch
            else:
                cur_yaw, cur_pitch = _interp(zoom_t, FALLBACK_YAW, FALLBACK_PITCH)
                fsm_mode         = ZOOM_OUT
                zoom_start_yaw   = cur_yaw
                zoom_start_pitch = cur_pitch
                reacquire_streak = 0
                cam_yaw          = cur_yaw
                cam_pitch        = cur_pitch
        else:
            cam_yaw, cam_pitch = ema_yaw, ema_pitch

        records.append({
            "frame":        frame_idx,
            "ball_yaw":     smoothed.get("yaw"),
            "ball_pitch":   smoothed.get("pitch"),
            "best_score":   best_score,
            "tracker_state": tracker_state,
            "ema_yaw":      ema_yaw,
            "ema_pitch":    ema_pitch,
            "cam_yaw":      cam_yaw,
            "cam_pitch":    cam_pitch,
            "fsm_mode":     fsm_mode,
            "confirmed":    confirmed,
        })

    return records


# ── Simulate activity EMA (v7 logic, isolated) ────────────────────────────────
def simulate_activity_ema(activity_samples, frame_range):
    """
    Replays v7 ActivityBias.update() for every frame in frame_range.
    Returns dict: frame -> (ema_yaw, ema_pitch, is_active, raw_yaw, raw_pitch, raw_conf)
    """
    samples_sorted = sorted(activity_samples, key=lambda x: x[0])
    ema_yaw = ema_pitch = None
    result = {}
    for frame_idx in frame_range:
        if not samples_sorted:
            result[frame_idx] = (FALLBACK_YAW, FALLBACK_PITCH, False, None, None, 0.0)
            continue
        best = min(samples_sorted, key=lambda x: abs(x[0] - frame_idx))
        raw_yaw, raw_pitch, raw_conf = best[1], best[2], best[3]

        if raw_conf < ACTIVITY_CONF_THRESH:
            result[frame_idx] = (
                ema_yaw if ema_yaw is not None else FALLBACK_YAW,
                ema_pitch if ema_pitch is not None else FALLBACK_PITCH,
                False, raw_yaw, raw_pitch, raw_conf,
            )
            continue

        if ema_yaw is None:
            ema_yaw   = raw_yaw
            ema_pitch = FALLBACK_PITCH
        else:
            diff = (raw_yaw - ema_yaw + 540) % 360 - 180
            ema_yaw   = ema_yaw + ACTIVITY_EMA_ALPHA * diff
            ema_pitch = FALLBACK_PITCH   # v7 held pitch fixed

        result[frame_idx] = (ema_yaw, ema_pitch, True, raw_yaw, raw_pitch, raw_conf)

    return result


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="FFA signal comparison diagnostic")
    ap.add_argument("--tracking",  required=True)
    ap.add_argument("--activity",  required=True)
    ap.add_argument("--start",     type=int, default=700)
    ap.add_argument("--end",       type=int, default=1300)
    ap.add_argument("--output",    default="signal_comparison.png")
    args = ap.parse_args()

    # ── Load data ─────────────────────────────────────────────────────────────
    with open(args.tracking) as f:
        tracking = json.load(f)
    frames_data = tracking.get("frames", [])
    fps = float(tracking.get("fps", 29.97))

    with open(args.activity) as f:
        activity = json.load(f)
    activity_samples = []
    for row in activity.get("frames", []):
        frame  = row.get("frame")
        centre = row.get("cluster_centre")
        conf   = row.get("confidence", 0.0)
        if frame is not None and centre is not None:
            activity_samples.append((frame, float(centre["yaw"]), float(centre["pitch"]), float(conf)))

    # ── Simulate signals ──────────────────────────────────────────────────────
    frame_range = range(args.start, args.end)
    v6_records  = simulate_v6(frames_data, args.start, args.end)
    act_ema     = simulate_activity_ema(activity_samples, frame_range)

    frames       = [r["frame"]      for r in v6_records]
    ball_yaws    = [r["ball_yaw"]   for r in v6_records]
    ball_pitches = [r["ball_pitch"] for r in v6_records]
    cam_yaws     = [r["cam_yaw"]    for r in v6_records]
    cam_pitches  = [r["cam_pitch"]  for r in v6_records]
    fsm_modes    = [r["fsm_mode"]   for r in v6_records]
    best_scores  = [r["best_score"] for r in v6_records]

    act_ema_yaws      = [act_ema[f][0]    for f in frame_range]
    act_ema_pitches   = [act_ema[f][1]    for f in frame_range]
    act_active        = [act_ema[f][2]    for f in frame_range]
    act_raw_yaws      = [act_ema[f][3]    for f in frame_range]
    act_raw_pitches   = [act_ema[f][4]    for f in frame_range]
    act_confs         = [act_ema[f][5]    for f in frame_range]

    # ── Compute yaw delta (activity EMA vs fixed overview) ────────────────────
    yaw_deltas = []
    for ey in act_ema_yaws:
        d = (ey - FALLBACK_YAW + 540) % 360 - 180
        yaw_deltas.append(d)

    # ── Build figure ──────────────────────────────────────────────────────────
    fig, axes = plt.subplots(4, 1, figsize=(18, 20), sharex=True)
    fig.suptitle(
        f"FFA Signal Comparison Diagnostic — frames {args.start}–{args.end}\n"
        "What each signal sends to the camera vs the safe fixed fallback",
        fontsize=13, fontweight="bold", y=0.98,
    )

    def shade_fsm(ax):
        """Shade background by FSM mode."""
        prev_mode = fsm_modes[0]
        seg_start = frames[0]
        for i, (f, m) in enumerate(zip(frames, fsm_modes)):
            if m != prev_mode or i == len(frames) - 1:
                ax.axvspan(seg_start, f, alpha=0.18,
                           color=FSM_COLOURS.get(prev_mode, "#ffffff"), zorder=0)
                seg_start = f
                prev_mode = m
        # Legend patches
        return [mpatches.Patch(color=c, alpha=0.4, label=k)
                for k, c in FSM_COLOURS.items()]

    # ── Panel 1: YAW ──────────────────────────────────────────────────────────
    ax = axes[0]
    fsm_patches = shade_fsm(ax)

    ax.axhline(FALLBACK_YAW, color="grey", ls=":", lw=1.2, label=f"Fixed fallback yaw = {FALLBACK_YAW}°")

    # Ball-tracker smoothed yaw (only where not None)
    by_x = [f for f, y in zip(frames, ball_yaws) if y is not None]
    by_y = [y for y in ball_yaws if y is not None]
    ax.plot(by_x, by_y, color="#1976D2", lw=1.5, label="Ball-tracker smoothed yaw", zorder=4)

    # Activity raw yaw — all frames with a cluster
    ar_x = [f for f, y in zip(list(frame_range), act_raw_yaws) if y is not None]
    ar_y = [y for y in act_raw_yaws if y is not None]
    ax.scatter(ar_x, ar_y, color="orange", s=15, alpha=0.5, label="Activity raw yaw (all clusters)", zorder=3)

    # Activity raw yaw — only conf >= threshold
    ah_x = [f for f, y, c in zip(list(frame_range), act_raw_yaws, act_confs)
            if y is not None and c >= ACTIVITY_CONF_THRESH]
    ah_y = [y for y, c in zip(act_raw_yaws, act_confs)
            if y is not None and c >= ACTIVITY_CONF_THRESH]
    ax.scatter(ah_x, ah_y, color="darkorange", s=40, marker="D",
               label=f"Activity raw yaw (conf ≥ {ACTIVITY_CONF_THRESH})", zorder=5)

    # v7 activity EMA yaw (what v7 actually sent)
    ax.plot(list(frame_range), act_ema_yaws, color="#C62828", lw=2,
            ls="-", label="v7 activity-EMA yaw (sent to camera)", zorder=6)

    # v6 camera yaw (what the revert sends)
    ax.plot(frames, cam_yaws, color="#2E7D32", lw=1.5, ls="--",
            label="v6 renderer camera yaw (fixed fallback)", zorder=5)

    ax.set_ylabel("Yaw (°)", fontsize=10)
    ax.set_title("YAW — all signals", fontsize=11)
    ax.legend(loc="upper right", fontsize=8, handles=ax.get_legend_handles_labels()[0] + fsm_patches)
    ax.grid(True, alpha=0.3)
    ax.set_ylim(-100, 120)

    # Annotate the "aimed at ground / trees" zones
    ax.axvspan(700, 875, alpha=0.07, color="purple", label="No cluster zone")
    ax.text(785, 100, "No cluster\n(conf=0)", ha="center", va="top", fontsize=7, color="purple")
    ax.text(980, -80, "Left play\nyaw≈-50°", ha="center", fontsize=7, color="darkorange")
    ax.text(1170, 100, "Right play\nyaw≈+65°", ha="center", fontsize=7, color="darkorange")

    # ── Panel 2: PITCH ────────────────────────────────────────────────────────
    ax = axes[1]
    shade_fsm(ax)

    ax.axhline(FALLBACK_PITCH, color="grey", ls=":", lw=1.2, label=f"Fixed fallback pitch = {FALLBACK_PITCH}°")

    bp_x = [f for f, p in zip(frames, ball_pitches) if p is not None]
    bp_y = [p for p in ball_pitches if p is not None]
    ax.plot(bp_x, bp_y, color="#1976D2", lw=1.5, label="Ball-tracker smoothed pitch", zorder=4)

    # Activity raw pitch (foot-proxy — the dangerous one)
    ap_x = [f for f, p in zip(list(frame_range), act_raw_pitches) if p is not None]
    ap_y = [p for p in act_raw_pitches if p is not None]
    ax.scatter(ap_x, ap_y, color="red", s=20, alpha=0.6, marker="x",
               label="Activity raw pitch (foot-proxy — DO NOT use as camera pitch)", zorder=5)

    # v7 camera pitch (held fixed, should be flat at 5°)
    ax.plot(list(frame_range), act_ema_pitches, color="#C62828", lw=2,
            label="v7 camera pitch (should be 5° flat)", zorder=6)

    ax.plot(frames, cam_pitches, color="#2E7D32", lw=1.5, ls="--",
            label="v6 camera pitch", zorder=5)

    ax.set_ylabel("Pitch (°)", fontsize=10)
    ax.set_title("PITCH — ball-tracker vs activity raw (foot-proxy) vs camera pitch", fontsize=11)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.set_ylim(-15, 50)
    ax.axhline(0, color="black", lw=0.5)

    # Annotate the dangerous pitch range
    ax.axhspan(15, 50, alpha=0.06, color="red")
    ax.text(700 + 10, 35, "Activity pitch > 15°\n(foot-proxy, points camera at ground/sky)",
            fontsize=7, color="red", va="center")

    # ── Panel 3: CONFIDENCE + TRACKER ─────────────────────────────────────────
    ax  = axes[2]
    ax2 = ax.twinx()

    shade_fsm(ax)
    ax.axhline(ACTIVITY_CONF_THRESH, color="red", ls="--", lw=1.2,
               label=f"Activity conf threshold = {ACTIVITY_CONF_THRESH}")
    ax.plot(list(frame_range), act_confs, color="purple", lw=1.5,
            label="Activity cluster confidence")
    ax.fill_between(list(frame_range), 0, act_confs, alpha=0.15, color="purple")
    ax.set_ylim(0, 1.1)
    ax.set_ylabel("Activity confidence", fontsize=10, color="purple")

    # Tracker best_score (normalised)
    norm_scores = []
    for s in best_scores:
        norm_scores.append(float(s) if s is not None else float("nan"))
    ax2.fill_between(frames, 0, norm_scores, alpha=0.2, color="#1976D2")
    ax2.plot(frames, norm_scores, color="#1976D2", lw=0.8, label="Ball best_score")
    ax2.set_ylim(0, 1.5)
    ax2.set_ylabel("Ball best_score", fontsize=10, color="#1976D2")

    ax.set_title("Activity confidence + tracker ball score + FSM mode", fontsize=11)
    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    fsm_patches = [mpatches.Patch(color=c, alpha=0.4, label=k)
                   for k, c in FSM_COLOURS.items()]
    ax.legend(lines1 + lines2 + fsm_patches, labels1 + labels2 + [k for k in FSM_COLOURS],
              loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)

    # ── Panel 4: YAW DELTA (activity EMA vs safe overview) ────────────────────
    ax = axes[3]
    shade_fsm(ax)

    ax.axhline(0, color="grey", ls=":", lw=1.2, label="0° = safe fixed fallback direction")
    ax.fill_between(list(frame_range), 0, yaw_deltas, alpha=0.3, color="red")
    ax.plot(list(frame_range), yaw_deltas, color="#C62828", lw=2,
            label="Activity-EMA yaw delta from fixed fallback (how far camera is pulled)")

    # Mark frames where activity is active
    active_frames = [f for f, a in zip(list(frame_range), act_active) if a]
    active_deltas = [yaw_deltas[i] for i, a in enumerate(act_active) if a]
    ax.scatter(active_frames, active_deltas, color="darkorange", s=20,
               label="Frames where activity is active (conf ≥ thresh)", zorder=5)

    # Annotate peak deviations
    if yaw_deltas:
        peak_idx = int(np.argmax(np.abs(yaw_deltas)))
        peak_frame = list(frame_range)[peak_idx]
        peak_val   = yaw_deltas[peak_idx]
        ax.annotate(f"Peak: {peak_val:+.1f}°\n@ frame {peak_frame}",
                    xy=(peak_frame, peak_val),
                    xytext=(peak_frame + 30, peak_val * 0.7),
                    fontsize=8, color="#C62828",
                    arrowprops=dict(arrowstyle="->", color="#C62828"))

    ax.set_ylabel("Yaw delta from\nfixed fallback (°)", fontsize=10)
    ax.set_xlabel("Frame", fontsize=10)
    ax.set_title("YAW DELTA — how far activity bias pulls camera from the safe overview position", fontsize=11)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)

    # ── Summary text box ──────────────────────────────────────────────────────
    fsm_summary = {}
    for m in fsm_modes:
        fsm_summary[m] = fsm_summary.get(m, 0) + 1

    high_pitch_frames = sum(1 for p in act_raw_pitches if p is not None and p > 15)
    active_count      = sum(1 for a in act_active if a)
    max_delta         = max((abs(d) for d in yaw_deltas), default=0)

    summary = (
        f"DIAGNOSTIC SUMMARY  |  frames {args.start}–{args.end}  ({len(frames)} rendered)\n"
        f"  FSM modes: " + "  ".join(f"{k}={v}f" for k, v in fsm_summary.items()) + "\n"
        f"  Activity: {active_count}/{len(frames)} frames with conf≥{ACTIVITY_CONF_THRESH}  "
        f"| max yaw delta from safe overview: {max_delta:.1f}°\n"
        f"  Activity raw pitch >15° (foot-proxy danger zone): {high_pitch_frames} frames\n"
        f"  Ball-tracker confirmed frames: {sum(1 for s in best_scores if s is not None)}"
    )
    fig.text(0.01, 0.01, summary, fontsize=8.5, family="monospace",
             va="bottom", ha="left",
             bbox=dict(boxstyle="round", facecolor="lightyellow", alpha=0.8))

    plt.tight_layout(rect=[0, 0.05, 1, 0.97])
    fig.savefig(args.output, dpi=150, bbox_inches="tight")
    print(f"[diag] Saved → {args.output}")
    print()
    print(summary)


if __name__ == "__main__":
    main()
