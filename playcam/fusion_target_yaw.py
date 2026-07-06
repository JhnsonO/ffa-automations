#!/usr/bin/env python3
"""Offline 4-tier camera target_yaw fusion (playcam).

Combines the MOG2 ball signal, the player-flow direction signal, and the
existing person-centroid yaw into a single proposed target_yaw timeline,
entirely offline -- no rendering, no paid compute, no renderer/tracker
files touched. This checks whether the *idea* is sound before any render
test; it does not itself change camera behavior anywhere.

Priority order (highest first):
  1. Ball-lock        -- MOG2 has a confident, geometrically-clean,
                         multi-frame-persisted candidate this sample.
  2. Flow-hold         -- ball-lock was valid recently (within
                         --hold-max-samples) but isn't now; hold near the
                         last known ball yaw, nudged by current flow
                         direction if present.
  3. Flow-only bias    -- no recent ball-lock, but the player-flow signal
                         shows *sustained* multi-track agreement; nudge
                         centroid yaw gently in that direction.
  4. Centroid fallback -- none of the above; use person_centroid_yaw as-is.

Inputs (produced by existing, already-committed, unmodified scripts):
  - mog2_candidates.json  (ball_tracker/mog2_detector.py output)
  - play_location.jsonl   (playcam/play_location.py output)

This script imports and reuses rather than duplicating:
  - ball_tracker.mog2_blob_filter.filter_candidates()  (static/aspect flags)
  - playcam.player_flow_bias.compute_flow_signal()     (direction/sustain)

Decision cadence matches play_location.jsonl's native ~2Hz sampling, not
full video frame rate -- this is an offline logic check, not a per-frame
render signal. Wiring into an actual per-frame render is a separate,
later step.

All thresholds below are first-pass, chosen from real distributions
observed in this one clip (see comments at each constant) -- they are
NOT tuned to the known t~59-60s goal specifically, and are deliberately
conservative. They should be re-examined once a second labeled clip
exists (project's own outstanding scorecard gap, issue #8).
"""
import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ball_tracker.mog2_blob_filter import filter_candidates as mog2_filter_candidates  # noqa: E402
from playcam.player_flow_bias import compute_flow_signal, load_records as load_play_location  # noqa: E402

# --- Tier 1: ball-lock ----------------------------------------------------
# Real clean-candidate (non static/wide-flat) confidence distribution for
# this clip: median 0.574, p75 0.629. 0.55 sits just under the real median
# -- i.e. it keeps a bit over half of geometrically-clean candidates
# eligible, rather than cherry-picking only the very top confidence tail.
MOG2_CONF_THRESH = 0.55
# A single clean, confident frame can still be a one-off false positive.
# Require a candidate at essentially the same spot (within this pixel
# radius) to reappear in at least this many frames within a local
# neighbourhood before trusting it as a real, persisted detection.
MOG2_PERSIST_RADIUS_PX = 15.0
MOG2_MIN_PERSIST_FRAMES = 2
MOG2_NEIGHBOR_RADIUS_FRAMES = 8  # ~0.27s either side at ~30fps
FRAME_WIDTH = 4032
CLIP_FPS = 3596 / 120.0  # matches mog2_candidates.json's known 3596 frames / 120s for this clip

# --- Tier 2: flow-hold -----------------------------------------------------
# How many ~2Hz samples (~0.5s each) to keep holding near the last known
# ball position after it disappears, before giving up on it entirely.
# Conservative: 4 samples = ~2s, well short of the ~13s flow-only
# build-up seen in this clip's t=41-54s window, so a hold can never be
# mistaken for that separate, longer signal.
HOLD_MAX_SAMPLES = 4
# Small, FIXED (non-cumulative -- always applied relative to the original
# held ball yaw, not stacked sample over sample) nudge when current flow
# has a direction. Kept below the flow-only bias below: a hold should
# lean on the last real ball fix, not chase flow on its own.
HOLD_FLOW_NUDGE_DEG = 2.0

# --- Tier 3: flow-only bias -------------------------------------------------
# Real sustained-sample mean_delta_yaw_deg values in this clip mostly fall
# in the ~1.5-5 deg/sample range (see player_flow_bias.py validation notes
# in docs/ai-project-state.md). 4.0 deg sits inside that real range rather
# than being an arbitrary pick, and is explicitly smaller than a ball fix's
# authority (tier 1 sets target_yaw directly; this only nudges).
FLOW_BIAS_NUDGE_DEG = 4.0


# --- Tier 1 continuity gate ------------------------------------------------
# The confidence+geometry+short persistence checks above are NOT sufficient
# on their own: smoke-testing this script against real data showed target_yaw
# swinging ~80-90 degrees between consecutive 0.5s samples when tier-1 just
# picks the highest-confidence qualifying candidate independently per window
# -- because MOG2 has no player/ball semantic distinction, "clean, confident,
# locally-persisted" also matches players' feet/legs, and different ones can
# win in different windows. A real ball cannot teleport corner-to-corner of
# the visible arc in half a second, so a continuity check against the last
# accepted ball-lock is required. This bound is a rough, conservative,
# explicitly-inspectable placeholder -- NOT derived from real ball-speed/
# camera-geometry data -- and should be revisited before this is trusted.
MOG2_MAX_JUMP_DEG_PER_SEC = 60.0
# If the ball has been missing longer than this, don't require continuity at
# all -- treat the next candidate as a fresh reacquisition instead of judging
# it against a now-stale last-known position.
MOG2_MAX_REACQUIRE_GAP_SEC = 2.0


def yaw_from_x(x, frame_width=FRAME_WIDTH):
    return (x / frame_width - 0.5) * 360.0


def xdist(x1, x2, frame_width=FRAME_WIDTH):
    dx = abs(x1 - x2)
    return min(dx, frame_width - dx)


def build_ball_lock_lookup(filtered_mog2_data, conf_thresh, persist_radius_px, min_persist_frames):
    """For each raw MOG2 frame index, find confident, persisted,
    geometrically-clean ball candidates. Returns dict:
        frame_idx -> list of (yaw, conf)
    A candidate qualifies if: not static_suspect, not wide_flat_suspect,
    conf >= conf_thresh, AND a same-location (within persist_radius_px)
    qualifying candidate also appears in enough nearby frames (within
    MOG2_NEIGHBOR_RADIUS_FRAMES) to reach min_persist_frames total --
    i.e. it wasn't a one-off single-frame blip. This function does NOT
    pick a single winner -- see sample_ball_lock_for_window() for the
    continuity-gated selection that happens across a 2Hz sample window.
    """
    fc = filtered_mog2_data["frame_candidates"]

    qualifying = {}  # frame_idx -> list of (cx, cy, conf)
    for f_str, cands in fc.items():
        f = int(f_str)
        q = []
        for c in cands:
            if c.get("static_suspect") or c.get("wide_flat_suspect"):
                continue
            if c["conf"] < conf_thresh:
                continue
            cx = c["x"] + c["w"] / 2.0
            cy = c["y"] + c["h"] / 2.0
            q.append((cx, cy, c["conf"]))
        if q:
            qualifying[f] = q

    qframes = sorted(qualifying.keys())
    result = {}
    for f in qframes:
        survivors = []
        for (cx, cy, conf) in qualifying[f]:
            persist_count = 1  # counts itself
            for g in qframes:
                if g == f or abs(g - f) > MOG2_NEIGHBOR_RADIUS_FRAMES:
                    continue
                for (gx, gy, _gconf) in qualifying[g]:
                    d = (xdist(cx, gx) ** 2 + (cy - gy) ** 2) ** 0.5
                    if d <= persist_radius_px:
                        persist_count += 1
                        break
            if persist_count >= min_persist_frames:
                survivors.append((yaw_from_x(cx), conf))
        if survivors:
            result[f] = survivors
    return result


def sample_ball_lock_for_window(
    ball_lock_lookup, frame_lo, frame_hi, last_ball_yaw, seconds_since_last_ball,
    max_jump_deg_per_sec, max_reacquire_gap_sec,
):
    """Among frames [frame_lo, frame_hi] inclusive, return the
    highest-confidence ball-lock (yaw, conf) that also passes a continuity
    check against the last accepted ball position, or None if nothing
    qualifies. If there is no recent last_ball_yaw (never locked, or lost
    for longer than max_reacquire_gap_sec), no continuity check is applied
    -- the best candidate is accepted as a fresh reacquisition.
    """
    require_continuity = (
        last_ball_yaw is not None
        and seconds_since_last_ball is not None
        and seconds_since_last_ball <= max_reacquire_gap_sec
    )
    max_jump = max_jump_deg_per_sec * (seconds_since_last_ball or 0.0)

    best = None
    for f in range(frame_lo, frame_hi + 1):
        for (yaw, conf) in ball_lock_lookup.get(f, []):
            if require_continuity:
                jump = abs(((yaw - last_ball_yaw + 180) % 360) - 180)
                if jump > max_jump:
                    continue
            if best is None or conf > best[1]:
                best = (yaw, conf)
    return best


def run_fusion(mog2_path, play_location_path, args):
    mog2_data = json.loads(Path(mog2_path).read_text(encoding="utf-8"))
    filtered, filt_stats = mog2_filter_candidates(
        mog2_data, FRAME_WIDTH, radius_px=5.0, min_frames=15, max_aspect_ratio=2.5
    )
    ball_lock_lookup = build_ball_lock_lookup(
        filtered, args.mog2_conf_thresh, args.mog2_persist_radius_px, args.mog2_min_persist_frames
    )

    records = load_play_location(play_location_path)
    flow_rows = compute_flow_signal(
        records, args.min_moving_vel, args.min_agreeing_tracks, args.min_sustain_samples
    )
    flow_by_ts = {round(r["timestamp"], 3): r for r in flow_rows}

    out_rows = []
    last_ball_yaw = None
    last_ball_time = None
    samples_since_ball = None  # None until the first ball-lock is ever seen
    prev_t = 0.0

    for i, rec in enumerate(records):
        t = rec["timestamp"]
        centroid_yaw = rec.get("person_centroid_yaw")
        flow = flow_by_ts.get(round(t, 3), {})
        flow_dir = int(flow.get("direction", 0))
        flow_sustained = int(flow.get("sustained", 0))

        frame_lo = 0 if i == 0 else int(round(prev_t * CLIP_FPS)) + 1
        frame_hi = int(round(t * CLIP_FPS))
        if frame_hi < frame_lo:
            frame_hi = frame_lo
        seconds_since_last_ball = (t - last_ball_time) if last_ball_time is not None else None
        ball_hit = sample_ball_lock_for_window(
            ball_lock_lookup, frame_lo, frame_hi, last_ball_yaw, seconds_since_last_ball,
            args.mog2_max_jump_deg_per_sec, args.mog2_max_reacquire_gap_sec,
        )

        ball_conf = ""
        if ball_hit is not None:
            tier = 1
            ball_yaw, conf = ball_hit
            target_yaw = ball_yaw
            ball_conf = round(conf, 3)
            last_ball_yaw = ball_yaw
            last_ball_time = t
            samples_since_ball = 0
        elif (
            samples_since_ball is not None
            and samples_since_ball < args.hold_max_samples
            and last_ball_yaw is not None
        ):
            tier = 2
            samples_since_ball += 1
            nudge = args.hold_flow_nudge_deg * flow_dir
            target_yaw = ((last_ball_yaw + nudge + 180) % 360) - 180
        elif flow_sustained and flow_dir != 0 and centroid_yaw is not None:
            tier = 3
            nudge = args.flow_bias_nudge_deg * flow_dir
            target_yaw = ((centroid_yaw + nudge + 180) % 360) - 180
            if samples_since_ball is not None:
                samples_since_ball += 1
        else:
            tier = 4
            target_yaw = centroid_yaw
            if samples_since_ball is not None:
                samples_since_ball += 1

        out_rows.append(
            {
                "timestamp": t,
                "tier": tier,
                "target_yaw": round(target_yaw, 2) if target_yaw is not None else "",
                "centroid_yaw": centroid_yaw,
                "flow_direction": flow_dir,
                "flow_sustained": flow_sustained,
                "flow_agreeing_tracks": flow.get("agreeing_tracks", 0),
                "ball_conf": ball_conf,
                "last_ball_yaw": round(last_ball_yaw, 2) if last_ball_yaw is not None else "",
                "samples_since_ball": samples_since_ball if samples_since_ball is not None else "",
            }
        )
        prev_t = t

    return out_rows, filt_stats


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mog2_candidates", help="Raw mog2_candidates.json (from mog2_detector.py)")
    p.add_argument("play_location", help="play_location.jsonl (from play_location.py)")
    p.add_argument("--output", default="fusion_target_yaw.csv")
    p.add_argument("--mog2-conf-thresh", type=float, default=MOG2_CONF_THRESH)
    p.add_argument("--mog2-persist-radius-px", type=float, default=MOG2_PERSIST_RADIUS_PX)
    p.add_argument("--mog2-min-persist-frames", type=int, default=MOG2_MIN_PERSIST_FRAMES)
    p.add_argument("--mog2-max-jump-deg-per-sec", type=float, default=MOG2_MAX_JUMP_DEG_PER_SEC)
    p.add_argument("--mog2-max-reacquire-gap-sec", type=float, default=MOG2_MAX_REACQUIRE_GAP_SEC)
    p.add_argument("--hold-max-samples", type=int, default=HOLD_MAX_SAMPLES)
    p.add_argument("--hold-flow-nudge-deg", type=float, default=HOLD_FLOW_NUDGE_DEG)
    p.add_argument("--flow-bias-nudge-deg", type=float, default=FLOW_BIAS_NUDGE_DEG)
    p.add_argument("--min-moving-vel", type=float, default=2.0)
    p.add_argument("--min-agreeing-tracks", type=int, default=3)
    p.add_argument("--min-sustain-samples", type=int, default=3)
    return p.parse_args()


def main():
    args = parse_args()
    rows, _filt_stats = run_fusion(args.mog2_candidates, args.play_location, args)
    fieldnames = [
        "timestamp", "tier", "target_yaw", "centroid_yaw", "flow_direction",
        "flow_sustained", "flow_agreeing_tracks", "ball_conf", "last_ball_yaw",
        "samples_since_ball",
    ]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for r in rows:
        tier_counts[r["tier"]] += 1
    print(f"Wrote {args.output}: {len(rows)} rows")
    print(
        f"Tier distribution: ball-lock={tier_counts[1]} flow-hold={tier_counts[2]} "
        f"flow-only={tier_counts[3]} centroid={tier_counts[4]}"
    )


if __name__ == "__main__":
    main()
