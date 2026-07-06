#!/usr/bin/env python3
"""Offline camera target_yaw fusion (playcam).

REVISED 6 July 2026 (2nd correction): reframed around Johnson's explicit
safety spec so player-flow cannot cause unbounded drift. The mental model
is now: "Ball/player cluster = where the camera lives. Flow = which side
of that area the camera gives extra room to." Flow is a single, small,
continuously fading look-ahead OFFSET added on top of wherever the anchor
already is -- never a separate signal that can walk the camera off on its
own, and never able to point outside the venue's usable range.

Ball input still comes from the established tracker's own Stage 2 output
(tracklets.json, status=="anchor") -- unchanged from the prior correction,
still not raw MOG2, still not re-diagnosing the tracker. What changed this
revision is entirely in how the flow offset is applied and safeguarded:

  1. Anchor position ("where the camera lives"), in priority order:
       - a fresh status=="anchor" tracklet observation this sample -> its yaw
       - else, if one was seen within --hold-max-samples (~2s) -> hold there
       - else -> person_centroid_yaw (player-group centroid; safety fallback)
  2. Flow offset ("which side to give extra room"): a single continuously-
     decaying value, not a per-tier nudge. When flow is sustained, it ramps
     TOWARD a bounded look-ahead offset (default 10deg, within Johnson's
     specified 5-15deg range) in the flow direction; whenever flow is not
     sustained (slows, reverses, disappears), it ramps back toward zero at
     the same rate -- "fades out quickly", never snaps to/from zero.
     Applied additively on top of the anchor position in ALL cases,
     including a fresh precise anchor -- exactly Johnson's "anchored at
     -30, then lean a controlled amount further left" model, not a replacement.
  3. Hard venue clamp: anchor+offset is clamped to the venue's usable yaw
     range before being pursued -- reuses the SAME range already
     established and used by the real renderer (`wide_safety_camera.py`:
     WIDE_YAW_RANGE_DEG=45, applied around venue["wide_yaw"] from
     playcam/venue_profiles/st_margarets.json, currently yaw=0.0). This
     script cannot pan the proposed target outside venue_wide_yaw+/-45deg,
     regardless of what ball/flow evidence suggests.
  4. Rate limiting, not EMA: the previous version used an ad-hoc EMA smooth.
     This revision instead pursues the clamped target at a maximum angular
     speed, mirroring wide_safety_camera.py's own established wide-mode
     pursuit pattern exactly (max_step = max_pan_speed * dt). Constant
     reused verbatim from smooth_camera_path.py's DEFAULT_MAX_PAN_SPEED_DEG_S
     (=25 deg/s) -- copied, not imported, to avoid pulling crop_utils/cv2
     into an offline yaw-only script. This is real rate-limiting, not
     cosmetic smoothing, and is the single mechanism guaranteeing "no hard
     snaps" for every source of change (fresh anchor, held anchor, flow
     offset ramp, or falling back to centroid).

Wide-mode zoom-out-when-uncertain (Johnson's "safety" bullet) is NOT
implemented here -- this script only ever proposes a yaw, it has no FOV/
zoom concept. That behaviour already exists in wide_safety_camera.py's own
wide/follow FSM and is unaffected by this script; wiring this script's
output into that FSM is a separate future integration step.

Inputs:
  - tracklets.json      (ball_tracker Stage 2 output, e.g. artifact 7944978610
                         from run 28355256427 -- already computed, zero paid
                         compute to reuse)
  - play_location.jsonl  (playcam/play_location.py output)
  - playcam/venue_profiles/st_margarets.json (read directly for wide_yaw;
                         not modified)

Reuses rather than duplicates:
  - playcam.player_flow_bias.compute_flow_signal()  (direction/sustain)

Still offline, still no render, still no paid compute, still no tracker/
renderer/frozen file modified or re-run. Decision cadence still matches
play_location.jsonl's ~2Hz sampling, not full frame rate.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from playcam.player_flow_bias import compute_flow_signal, load_records as load_play_location  # noqa: E402

CLIP_FPS = 3596 / 120.0  # shared assumption with tracklets/play_location frame range 6-3596 for this clip

# --- Anchor source (established tracker's own Stage 2 output) --------------
# Unchanged from the prior correction: only Stage 2's own strictest,
# pre-existing category (stage2_temporal_link.py: MIN_OBS_FOR_ANCHOR=8,
# MIN_MEAN_CONF_FOR_ANCHOR=0.20, MIN_COVERAGE_FOR_ANCHOR=0.50,
# MIN_ANCHOR_STRENGTH=0.55) counts as a reliable ball position. No
# interpolation within a tracklet's span; no additional continuity gate
# on top -- Stage 2's own linker already established these as coherent.
ANCHOR_STATUSES = ("anchor",)
HOLD_MAX_SAMPLES = 4  # ~2s: how long to hold the last anchor position before falling to centroid

# --- Flow look-ahead offset --------------------------------------------------
# Bounded magnitude, within Johnson's specified 5-15deg range -- picked
# near the low-middle of that range as a conservative first-pass default.
LOOKAHEAD_OFFSET_DEG = 10.0
# Per-sample blend toward the current target offset (0 when flow isn't
# sustained, +/-LOOKAHEAD_OFFSET_DEG when it is). At ~2Hz, alpha=0.35 fades
# the offset to <10% of its value within about 3s of flow going quiet --
# "fades out quickly" without a hard on/off snap. Same alpha reused from
# this project's prior EMA smoothing precedent, not newly invented.
FLOW_OFFSET_ALPHA = 0.35

# --- Hard venue yaw clamp ----------------------------------------------------
# CORRECTED after smoke-testing: an earlier pass of this script reused
# wide_safety_camera.py's WIDE_YAW_RANGE_DEG=45 (venue wide_yaw +/- 45) as
# the clamp. That constant is specific to the real renderer's WIDE-mode
# idle-drift pursuit (a conservative subset used only when there is no
# confident target) -- NOT the physical pitch boundary. Applying it here
# would have clamped genuine anchor positions (e.g. the known real goal at
# yaw~-55deg, and 57/240 samples in this run) to an artificially narrow
# window, actively fighting the tracker's own valid evidence. The actual
# "cannot pan into fences, empty corners, or off-pitch areas" boundary is
# the venue profile's own human-calibrated play_area polygon
# (playcam/venue_profiles/st_margarets.json, "human-calibrated by Johnson
# for MOG2 masking" per docs/ai-project-state.md) -- its x-extent converted
# to yaw is the real usable range, computed directly below rather than
# hardcoded, so it stays correct if a different venue profile is used.
VENUE_PROFILE_PATH = REPO_ROOT / "playcam" / "venue_profiles" / "st_margarets.json"

# --- Rate limit (replaces the previous EMA smoothing pass) ------------------
# Copied verbatim from playcam/smooth_camera_path.py (DEFAULT_MAX_PAN_SPEED_DEG_S)
# -- not imported directly, to avoid pulling crop_utils/cv2 into an offline
# yaw-only script. Mirrors wide_safety_camera.py's own pursuit pattern
# (max_step = max_pan_speed * dt) so every source of yaw change (fresh
# anchor, held anchor, flow ramp, or centroid fallback) is rate-limited
# identically -- the single mechanism guaranteeing no hard snaps.
MAX_PAN_SPEED_DEG_S = 25.0


def circular_delta(a, b):
    return ((a - b + 180) % 360) - 180


def load_venue_bounds(path):
    """Returns (wide_yaw, venue_lo, venue_hi). wide_yaw is still read for
    reference/reporting; venue_lo/hi are the play_area polygon's actual
    x-extent converted to yaw -- the real physical pitch/venue boundary,
    not the narrower wide-mode-only subset."""
    profile = json.loads(Path(path).read_text(encoding="utf-8"))
    wide_yaw = profile.get("wide_fallback", {}).get("yaw", 0.0)
    play_area = profile.get("play_area")
    if not play_area:
        return wide_yaw, -180.0, 180.0
    frame_width = play_area["frame_width"]
    xs = [pt[0] for pt in play_area["polygon"]]
    yaws = [(x / frame_width - 0.5) * 360.0 for x in xs]
    return wide_yaw, min(yaws), max(yaws)


def load_anchor_lookup(tracklets_path, anchor_statuses=ANCHOR_STATUSES):
    """Frame-indexed lookup of directly-observed anchor-tracklet positions.
    Returns dict: frame_idx -> list of (yaw, weighted_conf). No
    interpolation/extrapolation within a tracklet's span."""
    data = json.loads(Path(tracklets_path).read_text(encoding="utf-8"))
    lookup = {}
    for t in data["tracklets"]:
        if t["status"] not in anchor_statuses:
            continue
        for obs in t["frames"]:
            f = obs["frame"]
            lookup.setdefault(f, []).append((obs["yaw"], obs.get("weighted_conf", 0.0)))
    return lookup


def sample_anchor_for_window(anchor_lookup, frame_lo, frame_hi):
    """Highest-confidence anchor observation within [frame_lo, frame_hi], or
    None. No additional continuity/persistence gating -- Stage 2's own
    linker already established these as coherent."""
    best = None
    for f in range(frame_lo, frame_hi + 1):
        for (yaw, conf) in anchor_lookup.get(f, []):
            if best is None or conf > best[1]:
                best = (yaw, conf)
    return best


def run_fusion(tracklets_path, play_location_path, venue_profile_path, args):
    anchor_lookup = load_anchor_lookup(tracklets_path)
    wide_yaw, venue_lo, venue_hi = load_venue_bounds(venue_profile_path)

    records = load_play_location(play_location_path)
    flow_rows = compute_flow_signal(
        records, args.min_moving_vel, args.min_agreeing_tracks, args.min_sustain_samples
    )
    flow_by_ts = {round(r["timestamp"], 3): r for r in flow_rows}

    rows = []
    last_anchor_yaw = None
    samples_since_anchor = None  # None until the first anchor is ever seen
    flow_offset = 0.0
    camera_yaw_state = None  # rate-limited output state
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
        anchor_hit = sample_anchor_for_window(anchor_lookup, frame_lo, frame_hi)

        ball_conf = ""
        if anchor_hit is not None:
            anchor_source = "anchor"
            anchor_yaw, conf = anchor_hit
            ball_conf = round(conf, 3)
            last_anchor_yaw = anchor_yaw
            samples_since_anchor = 0
            anchor_position = anchor_yaw
        elif (
            samples_since_anchor is not None
            and samples_since_anchor < args.hold_max_samples
            and last_anchor_yaw is not None
        ):
            anchor_source = "held_anchor"
            samples_since_anchor += 1
            anchor_position = last_anchor_yaw
        else:
            anchor_source = "centroid"
            if samples_since_anchor is not None:
                samples_since_anchor += 1
            anchor_position = centroid_yaw

        # Flow look-ahead offset: ramp toward the bounded target, in either
        # direction, at the same rate -- ramps UP when sustained (no
        # instant jump to full offset) and fades back toward 0 otherwise.
        target_offset = args.lookahead_offset_deg * flow_dir if (flow_sustained and flow_dir != 0) else 0.0
        flow_offset += (target_offset - flow_offset) * args.flow_offset_alpha

        combined_raw_yaw = None
        clamped_yaw = None
        if anchor_position is not None:
            combined_raw_yaw = ((anchor_position + flow_offset + 180) % 360) - 180
            clamped_yaw = max(venue_lo, min(venue_hi, combined_raw_yaw))

        # Rate-limited pursuit of the clamped target -- mirrors
        # wide_safety_camera.py's own wide-mode pursuit exactly.
        dt = (t - prev_t) if i > 0 else 0.0
        if clamped_yaw is not None:
            if camera_yaw_state is None:
                camera_yaw_state = clamped_yaw
            else:
                diff = circular_delta(clamped_yaw, camera_yaw_state)
                max_step = args.max_pan_speed_deg_s * dt
                step = max(-max_step, min(max_step, diff))
                camera_yaw_state = ((camera_yaw_state + step + 180) % 360) - 180

        rows.append(
            {
                "timestamp": t,
                "anchor_source": anchor_source,
                "anchor_position": round(anchor_position, 2) if anchor_position is not None else "",
                "flow_direction": flow_dir,
                "flow_sustained": flow_sustained,
                "flow_offset_deg": round(flow_offset, 2),
                "combined_raw_yaw": round(combined_raw_yaw, 2) if combined_raw_yaw is not None else "",
                "clamped_yaw": round(clamped_yaw, 2) if clamped_yaw is not None else "",
                "target_yaw": round(camera_yaw_state, 2) if camera_yaw_state is not None else "",
                "ball_conf": ball_conf,
                "samples_since_anchor": samples_since_anchor if samples_since_anchor is not None else "",
            }
        )
        prev_t = t

    return rows, wide_yaw, venue_lo, venue_hi


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tracklets", help="Stage 2 tracklets.json from the established ball tracker")
    p.add_argument("play_location", help="play_location.jsonl (from play_location.py)")
    p.add_argument("--venue-profile", default=str(VENUE_PROFILE_PATH),
                    help="Venue profile JSON to read wide_fallback.yaw from")
    p.add_argument("--output", default="fusion_target_yaw.csv")
    p.add_argument("--hold-max-samples", type=int, default=HOLD_MAX_SAMPLES)
    p.add_argument("--lookahead-offset-deg", type=float, default=LOOKAHEAD_OFFSET_DEG)
    p.add_argument("--flow-offset-alpha", type=float, default=FLOW_OFFSET_ALPHA)
    p.add_argument("--max-pan-speed-deg-s", type=float, default=MAX_PAN_SPEED_DEG_S)
    p.add_argument("--min-moving-vel", type=float, default=2.0)
    p.add_argument("--min-agreeing-tracks", type=int, default=3)
    p.add_argument("--min-sustain-samples", type=int, default=3)
    return p.parse_args()


def main():
    args = parse_args()
    rows, wide_yaw, venue_lo, venue_hi = run_fusion(
        args.tracklets, args.play_location, args.venue_profile, args
    )
    fieldnames = [
        "timestamp", "anchor_source", "anchor_position", "flow_direction", "flow_sustained",
        "flow_offset_deg", "combined_raw_yaw", "clamped_yaw", "target_yaw", "ball_conf",
        "samples_since_anchor",
    ]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    source_counts = {"anchor": 0, "held_anchor": 0, "centroid": 0}
    clamp_hits = 0
    for r in rows:
        source_counts[r["anchor_source"]] += 1
        if r["combined_raw_yaw"] != "" and r["clamped_yaw"] != "" and r["combined_raw_yaw"] != r["clamped_yaw"]:
            clamp_hits += 1
    print(f"Wrote {args.output}: {len(rows)} rows")
    print(f"Venue: wide_yaw={wide_yaw}, usable range=[{venue_lo}, {venue_hi}]")
    print(
        f"Anchor source distribution: anchor={source_counts['anchor']} "
        f"held_anchor={source_counts['held_anchor']} centroid={source_counts['centroid']}"
    )
    print(f"Venue clamp actually triggered on {clamp_hits} samples")


if __name__ == "__main__":
    main()
