#!/usr/bin/env python3
"""
tracklets_to_tracking.py
Converts Stage 2 tracklets.json into tracking.json for render_segment.py.

For anchor/flight_anchor frames: use observed yaw/pitch, best_score set.
For gap frames: linearly interpolate yaw/pitch between surrounding anchors.
  - Short gaps (<=30f): interpolate, best_score=None (renderer wide fallbacks anyway)
  - Long gaps: no interpolation, best_score=None (full wide fallback)
Gap threshold configurable via --interp-gap-max.
"""
import argparse, json, math

INTERP_GAP_MAX = 90  # interpolate across gaps up to this many frames
FPS = 29.97

def slerp_yaw_pitch(y0, p0, y1, p1, t):
    """Linear interpolation on yaw/pitch (wraps yaw)."""
    # Handle yaw wrap
    dy = y1 - y0
    if dy > 180:  y1 -= 360
    if dy < -180: y1 += 360
    yaw   = y0 + t * (y1 - y0)
    pitch = p0 + t * (p1 - p0)
    return yaw, pitch

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracklets",      required=True)
    ap.add_argument("--total-frames",   type=int, required=True)
    ap.add_argument("--output",         required=True)
    ap.add_argument("--interp-gap-max", type=int, default=INTERP_GAP_MAX)
    args = ap.parse_args()

    with open(args.tracklets) as f:
        data = json.load(f)

    tracklets = data["tracklets"]
    anchor_statuses = {"anchor", "flight_anchor"}

    # Build frame->observation map from all anchor/flight_anchor tracklets
    frame_obs = {}  # frame -> (yaw, pitch, score, tracklet_id)
    for tr in tracklets:
        if tr["status"] not in anchor_statuses:
            continue
        for fr in tr["frames"]:
            fidx = fr["frame"]
            score = fr.get("score") or tr.get("anchor_strength_candidate") or 0.5
            # Keep highest-score observation if multiple anchors overlap
            if fidx not in frame_obs or score > frame_obs[fidx][2]:
                frame_obs[fidx] = (fr["yaw"], fr["pitch"], score, tr["id"])

    print(f"Anchor observations: {len(frame_obs)} frames")

    # Build sorted list of anchor keyframes
    keyframes = sorted(frame_obs.keys())

    # Build output frame list
    frames_out = []
    total = args.total_frames

    for fidx in range(total):
        if fidx in frame_obs:
            yaw, pitch, score, tid = frame_obs[fidx]
            frames_out.append({
                "smoothed":     {"yaw": round(yaw, 3), "pitch": round(pitch, 3)},
                "best_score":   round(float(score), 4),
                "tracker_state": "TRACKING",
                "source_tracklet": tid
            })
        else:
            # Find surrounding keyframes
            prev_kf = next((k for k in reversed(keyframes) if k < fidx), None)
            next_kf = next((k for k in keyframes if k > fidx), None)

            if prev_kf is not None and next_kf is not None:
                gap = next_kf - prev_kf
                if gap <= args.interp_gap_max:
                    t = (fidx - prev_kf) / gap
                    y0, p0 = frame_obs[prev_kf][:2]
                    y1, p1 = frame_obs[next_kf][:2]
                    yaw, pitch = slerp_yaw_pitch(y0, p0, y1, p1, t)
                    frames_out.append({
                        "smoothed":     {"yaw": round(yaw, 3), "pitch": round(pitch, 3)},
                        "best_score":   None,
                        "tracker_state": "LOST",
                        "source_tracklet": f"interp({frame_obs[prev_kf][3]}->{frame_obs[next_kf][3]})"
                    })
                else:
                    # Long gap — wide fallback
                    ref_yaw = frame_obs[prev_kf][0] if prev_kf is not None else 0.0
                    ref_pitch = frame_obs[prev_kf][1] if prev_kf is not None else 0.0
                    frames_out.append({
                        "smoothed":     {"yaw": round(ref_yaw, 3), "pitch": round(ref_pitch, 3)},
                        "best_score":   None,
                        "tracker_state": "LOST",
                        "source_tracklet": None
                    })
            else:
                ref_yaw = frame_obs[prev_kf][0] if prev_kf is not None else 0.0
                ref_pitch = frame_obs[prev_kf][1] if prev_kf is not None else 0.0
                frames_out.append({
                    "smoothed":     {"yaw": round(ref_yaw, 3), "pitch": round(ref_pitch, 3)},
                    "best_score":   None,
                    "tracker_state": "LOST",
                    "source_tracklet": None
                })

    confirmed = sum(1 for f in frames_out if f["best_score"] is not None)
    interped  = sum(1 for f in frames_out if f["tracker_state"] == "LOST" and f["source_tracklet"] and "interp" in str(f["source_tracklet"]))
    lost      = sum(1 for f in frames_out if f["tracker_state"] == "LOST" and not (f["source_tracklet"] and "interp" in str(f["source_tracklet"])))

    print(f"Output: {total} frames")
    print(f"  Confirmed (anchor):     {confirmed} ({100*confirmed/total:.1f}%)")
    print(f"  Interpolated (gap):     {interped} ({100*interped/total:.1f}%)")
    print(f"  Wide fallback (lost):   {lost} ({100*lost/total:.1f}%)")

    out = {"fps": FPS, "frames": frames_out}
    with open(args.output, "w") as f:
        json.dump(out, f)
    print(f"Written: {args.output}")

if __name__ == "__main__":
    main()
