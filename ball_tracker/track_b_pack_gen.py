#!/usr/bin/env python3
"""
FFA Track B — Review Pack Generator                       track_b_pack_gen_v1
==============================================================================
Inputs  : equirect_trim.mp4, stage1_candidates.json
Outputs : candidate_precision_review_pack.png     (60 candidate-centred tiles)
          zero_candidate_coverage_review_pack.png  (15 rows x 4 fixed crops)
          track_b_manifest.json                    (deterministic manifest)
          track_b_report.txt                       (stratum counts only)
          run_summary.json

No YOLO. No tracking.json.
"""

import argparse, json, math, os, random, sys
from datetime import datetime, timezone

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

VERSION        = "track_b_pack_gen_v1"
CAND_SAMPLES   = 60
ZERO_SAMPLES   = 15
CAND_TILE_SIZE = 256       # square tile px
CAND_TILE_COLS = 10
CAND_TILE_FOV  = 45.0      # degrees – tight zoom centred on candidate
CAND_HDR_H     = 24        # text header height px
ZERO_CROP_W    = 320       # zero-pack per-crop width
ZERO_CROP_H    = 180       # zero-pack per-crop height (16:9)
ZERO_CROP_YAWS = [0, 90, 180, 270]
ZERO_FULL_FOV  = 110.0     # matches Stage 1
ZERO_ROW_HDR_H = 20        # zero-pack row label height px
SEED           = 42
HOT_THRESH     = 0.5       # penalty < this → hotspot_adjacent
NEU_THRESH     = 0.9       # all penalties >= this → hotspot_neutral


# ─── Geometry ──────────────────────────────────────────────────────────────────

def _world_ray(yaw_deg, pitch_deg):
    y, p = math.radians(yaw_deg), math.radians(pitch_deg)
    return np.array([math.sin(y) * math.cos(p),
                     math.sin(p),
                     math.cos(y) * math.cos(p)])


def extract_perspective(eqr, look_yaw, look_pitch, fov_deg, out_w, out_h):
    """
    Perspective crop centred on (look_yaw, look_pitch) with proper pitch handling.
    R = cross(world_up, L) to match Stage-1 horizontal orientation.
    """
    h_eq, w_eq = eqr.shape[:2]
    L = _world_ray(look_yaw, look_pitch)
    world_up = np.array([0.0, 1.0, 0.0])

    R = np.cross(world_up, L)
    if np.linalg.norm(R) < 1e-6:
        R = np.array([1.0, 0.0, 0.0])
    else:
        R = R / np.linalg.norm(R)
    U = np.cross(L, R)
    U = U / np.linalg.norm(U)

    f  = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)

    cx = (xv - out_w / 2.0) / f
    cy = -(yv - out_h / 2.0) / f

    wx = cx * R[0] + cy * U[0] + L[0]
    wy = cx * R[1] + cy * U[1] + L[1]
    wz = cx * R[2] + cy * U[2] + L[2]
    n  = np.sqrt(wx ** 2 + wy ** 2 + wz ** 2)
    wx, wy, wz = wx / n, wy / n, wz / n

    mx = ((np.arctan2(wx, wz) / (2 * math.pi)) + 0.5) * w_eq
    my = (0.5 - np.arcsin(np.clip(wy, -1, 1)) / math.pi) * h_eq
    return cv2.remap(eqr, mx.astype(np.float32), my.astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


# ─── Stratified sampling ───────────────────────────────────────────────────────

def _temporal_stratum(fi, total_frames):
    t = total_frames // 3
    if fi < t:       return "temporal_early"
    elif fi < 2 * t: return "temporal_mid"
    else:            return "temporal_late"


def _label_cand_frame(fi, cands, total_frames, high_t, low_t):
    labels = {_temporal_stratum(fi, total_frames)}
    n = len(cands)
    if n == 1:  labels.add("single_candidate")
    elif n >= 2: labels.add("multi_candidate")
    if n >= 3:  labels.add("cluttered")
    top_wc = max(c["weighted_conf"] for c in cands)
    if top_wc >= high_t: labels.add("high_conf")
    if top_wc <= low_t:  labels.add("low_conf")
    penalties = [c.get("penalty", 1.0) for c in cands]
    if any(p < HOT_THRESH for p in penalties):  labels.add("hotspot_adjacent")
    if all(p >= NEU_THRESH for p in penalties):  labels.add("hotspot_neutral")
    return labels


def build_cand_samples(frames_raw, total_frames, fps):
    """Return (records, stratum_counts) for CAND_SAMPLES candidate frames."""
    random.seed(SEED)
    cf = {fi: cands for fi, cands in frames_raw.items() if cands}
    all_wc = sorted(max(c["weighted_conf"] for c in v) for v in cf.values())
    n = len(all_wc)
    high_t = all_wc[int(0.75 * n)] if n else 1.0
    low_t  = all_wc[int(0.25 * n)] if n else 0.0

    labeled = {fi: _label_cand_frame(fi, cands, total_frames, high_t, low_t)
               for fi, cands in cf.items()}

    budget = [
        ("temporal_early",    10), ("temporal_mid",     10), ("temporal_late",    10),
        ("high_conf",          8), ("low_conf",           8), ("multi_candidate",   7),
        ("hotspot_adjacent",   6), ("single_candidate",   6), ("hotspot_neutral",   5),
        ("cluttered",          5),
    ]  # total budget 75 > 60; dedup trims

    pools = {}
    for fi, lbls in labeled.items():
        for l in lbls: pools.setdefault(l, []).append(fi)

    selected = set()
    for stratum, target in budget:
        pool = pools.get(stratum, [])
        novel = [fi for fi in pool if fi not in selected]
        random.shuffle(novel)
        picks = novel[:target]
        if len(picks) < target:
            extra = [fi for fi in pool if fi in selected]
            random.shuffle(extra)
            picks += extra[:target - len(picks)]
        selected.update(picks)

    sel = sorted(selected)
    if len(sel) > CAND_SAMPLES:
        def rarity(fi):
            return sum(len(pools.get(l, [])) for l in labeled[fi])
        sel.sort(key=rarity)
        sel = sorted(sel[:CAND_SAMPLES])
    elif len(sel) < CAND_SAMPLES:
        rem = [fi for fi in sorted(cf.keys()) if fi not in selected]
        random.shuffle(rem)
        sel = sorted(sel + rem[:CAND_SAMPLES - len(sel)])

    stratum_counts = {}
    records = []
    for fi in sel:
        cands = cf[fi]
        top   = max(cands, key=lambda c: c["weighted_conf"])
        lbls  = sorted(labeled[fi])
        for l in lbls:
            stratum_counts[l] = stratum_counts.get(l, 0) + 1
        records.append({
            "frame_idx":         fi,
            "timestamp_s":       round(fi / fps, 3) if fps else None,
            "strata":            lbls,
            "candidate_count":   len(cands),
            "top_yaw":           round(top["yaw"],   3),
            "top_pitch":         round(top["pitch"], 3),
            "top_crop_yaw":      top.get("crop_yaw"),
            "top_raw_conf":      round(top["raw_conf"], 4),
            "top_penalty":       round(top.get("penalty", 1.0), 4),
            "top_weighted_conf": round(top["weighted_conf"], 4),
        })
    return records, stratum_counts


def build_zero_samples(frames_raw, total_frames, fps):
    """Return (records, stratum_counts) for ZERO_SAMPLES zero-candidate frames."""
    random.seed(SEED + 1)
    zero_fis = sorted(
        fi for fi in range(total_frames)
        if not frames_raw.get(fi)
    )
    t = total_frames // 3
    pools = {"temporal_early": [], "temporal_mid": [], "temporal_late": []}
    for fi in zero_fis:
        pools[_temporal_stratum(fi, total_frames)].append(fi)

    per = ZERO_SAMPLES // 3  # 5 per temporal third
    selected = []
    for pool in pools.values():
        random.shuffle(pool)
        selected.extend(pool[:per])
    rem = [fi for fi in zero_fis if fi not in set(selected)]
    random.shuffle(rem)
    selected.extend(rem[:ZERO_SAMPLES - len(selected)])
    selected = sorted(selected[:ZERO_SAMPLES])

    stratum_counts = {}
    records = []
    for fi in selected:
        s = _temporal_stratum(fi, total_frames)
        stratum_counts[s] = stratum_counts.get(s, 0) + 1
        records.append({
            "frame_idx":   fi,
            "timestamp_s": round(fi / fps, 3) if fps else None,
            "stratum":     s,
        })
    return records, stratum_counts


# ─── Tile / row rendering ──────────────────────────────────────────────────────

def _try_font(size):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
    ]:
        if os.path.isfile(path):
            try: return ImageFont.truetype(path, size)
            except Exception: pass
    return ImageFont.load_default()


def _to_pil(bgr):
    return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))


def make_cand_tile(frame_bgr, rec):
    """256x(256+24) PIL tile centred on candidate. No confidence shown."""
    crop = extract_perspective(
        frame_bgr, rec["top_yaw"], rec["top_pitch"],
        CAND_TILE_FOV, CAND_TILE_SIZE, CAND_TILE_SIZE)

    strata_short = " ".join(
        s.replace("temporal_", "t:")
         .replace("candidate", "cand")
         .replace("hotspot_", "hs:")
        for s in rec["strata"])
    hdr_text = f"F{rec['frame_idx']}  {strata_short}"

    tile = Image.new("RGB", (CAND_TILE_SIZE, CAND_TILE_SIZE + CAND_HDR_H), (18, 18, 18))
    ImageDraw.Draw(tile).text((3, 4), hdr_text,
                              fill=(220, 220, 60), font=_try_font(9))
    tile.paste(_to_pil(crop), (0, CAND_HDR_H))
    return tile


def make_zero_row(frame_bgr, rec):
    """1280x200 PIL row with 4 fixed crops (0/90/180/270) + row header."""
    row_w = ZERO_CROP_W * 4
    row_h = ZERO_CROP_H + ZERO_ROW_HDR_H
    row   = Image.new("RGB", (row_w, row_h), (12, 12, 28))
    font  = _try_font(9)
    draw  = ImageDraw.Draw(row)

    hdr = f"F{rec['frame_idx']}  {rec['stratum']}  — no candidates"
    draw.text((4, 3), hdr, fill=(180, 180, 255), font=font)

    for col_i, cyaw in enumerate(ZERO_CROP_YAWS):
        # Render at 2× and downsample for quality
        crop = extract_perspective(
            frame_bgr, float(cyaw), 0.0,
            ZERO_FULL_FOV, ZERO_CROP_W * 2, ZERO_CROP_H * 2)
        crop = cv2.resize(crop, (ZERO_CROP_W, ZERO_CROP_H), interpolation=cv2.INTER_AREA)
        cp = _to_pil(crop)
        ImageDraw.Draw(cp).text((3, 3), f"{cyaw}°",
                                fill=(255, 255, 80), font=font)
        row.paste(cp, (col_i * ZERO_CROP_W, ZERO_ROW_HDR_H))
    return row


# ─── Pack assembly ─────────────────────────────────────────────────────────────

def render_packs(equirect_path, cand_recs, zero_recs, outdir):
    os.environ["OPENCV_FFMPEG_READ_ATTEMPTS"] = "65536"
    cap = cv2.VideoCapture(equirect_path, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open {equirect_path}")

    cand_by_fi = {r["frame_idx"]: r for r in cand_recs}
    zero_by_fi = {r["frame_idx"]: r for r in zero_recs}
    all_fis    = sorted(set(cand_by_fi) | set(zero_by_fi))

    cand_tiles = {}
    zero_rows  = {}
    prev = -1

    for fi in all_fis:
        if fi != prev + 1:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ret, frame = cap.read()
        if not ret:
            print(f"  WARN: frame {fi} read failed", file=sys.stderr)
            prev = fi; continue
        prev = fi

        if fi in cand_by_fi:
            cand_tiles[fi] = make_cand_tile(frame, cand_by_fi[fi])
        if fi in zero_by_fi:
            zero_rows[fi]  = make_zero_row(frame, zero_by_fi[fi])

        print(f"  [frame {fi}] {'cand' if fi in cand_by_fi else ''}{'zero' if fi in zero_by_fi else ''}")

    cap.release()

    # Pack 1 — candidate precision review (10 cols × 6 rows = 60 tiles)
    tw = CAND_TILE_SIZE
    th = CAND_TILE_SIZE + CAND_HDR_H
    ordered_c = [r["frame_idx"] for r in cand_recs if r["frame_idx"] in cand_tiles]
    n_rows    = math.ceil(len(ordered_c) / CAND_TILE_COLS)
    pack1     = Image.new("RGB", (tw * CAND_TILE_COLS, th * n_rows), (8, 8, 8))
    for i, fi in enumerate(ordered_c):
        r_i, c_i = divmod(i, CAND_TILE_COLS)
        pack1.paste(cand_tiles[fi], (c_i * tw, r_i * th))
    p1 = os.path.join(outdir, "candidate_precision_review_pack.png")
    pack1.save(p1)
    print(f"[pack1] {p1}  {pack1.size[0]}x{pack1.size[1]}")

    # Pack 2 — zero-candidate coverage review (15 rows × 4 crops)
    rh = ZERO_CROP_H + ZERO_ROW_HDR_H
    rw = ZERO_CROP_W * 4
    ordered_z = [r["frame_idx"] for r in zero_recs if r["frame_idx"] in zero_rows]
    pack2     = Image.new("RGB", (rw, rh * len(ordered_z)), (8, 8, 16))
    for i, fi in enumerate(ordered_z):
        pack2.paste(zero_rows[fi], (0, i * rh))
    p2 = os.path.join(outdir, "zero_candidate_coverage_review_pack.png")
    pack2.save(p2)
    print(f"[pack2] {p2}  {pack2.size[0]}x{pack2.size[1]}")

    return p1, p2


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--equirect",          required=True)
    ap.add_argument("--stage1-candidates", required=True)
    ap.add_argument("--output-dir",        default=".")
    # provenance args (populated by workflow env)
    ap.add_argument("--equirect-file-id",  default="")
    ap.add_argument("--stage1-id",         default="")
    ap.add_argument("--hotspot-id",        default="")
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    t0 = datetime.now(timezone.utc)

    print(f"[pack_gen] Loading {args.stage1_candidates}")
    with open(args.stage1_candidates) as f:
        s1 = json.load(f)
    total_frames = s1["total_frames"]
    fps          = s1["fps"]
    frames_raw   = {int(k): v for k, v in s1["frames"].items()}
    print(f"[pack_gen] {total_frames} frames total, {len(frames_raw)} with candidates")

    cand_recs, cand_sc = build_cand_samples(frames_raw, total_frames, fps)
    zero_recs, zero_sc = build_zero_samples(frames_raw, total_frames, fps)
    print(f"[pack_gen] {len(cand_recs)} candidate samples, {len(zero_recs)} zero samples")

    # Manifest
    manifest = {
        "version":        VERSION,
        "seed":           SEED,
        "total_frames":   total_frames,
        "fps":            fps,
        "generated_utc":  t0.isoformat(),
        "candidate_samples": {
            "count":          len(cand_recs),
            "stratum_counts": cand_sc,
            "frames":         cand_recs,
        },
        "zero_candidate_samples": {
            "count":          len(zero_recs),
            "stratum_counts": zero_sc,
            "frames":         zero_recs,
        },
    }
    mp = os.path.join(args.output_dir, "track_b_manifest.json")
    with open(mp, "w") as f: json.dump(manifest, f, indent=2)
    print(f"[pack_gen] Manifest -> {mp}")

    # Render packs
    p1, p2 = render_packs(args.equirect, cand_recs, zero_recs, args.output_dir)

    # Report — stratum counts only; no quality conclusion
    rp = os.path.join(args.output_dir, "track_b_report.txt")
    with open(rp, "w") as f:
        f.write(f"FFA Track B Audit Report\n")
        f.write(f"Version  : {VERSION}\n")
        f.write(f"Generated: {t0.isoformat()}\n")
        f.write(f"Seed     : {SEED}\n\n")
        f.write(f"Pack 1 — candidate_precision_review_pack.png ({len(cand_recs)} samples)\n")
        for k, v in sorted(cand_sc.items()):
            f.write(f"  {k:<28}: {v}\n")
        f.write(f"\nPack 2 — zero_candidate_coverage_review_pack.png ({len(zero_recs)} samples)\n")
        for k, v in sorted(zero_sc.items()):
            f.write(f"  {k:<28}: {v}\n")
        f.write("\nLabel options — candidate tiles:\n")
        f.write("  ball_at_centre | ball_nearby_but_offset | not_ball | occluded_or_unclear\n")
        f.write("\nLabel options — zero-candidate tiles:\n")
        f.write("  ball_visible_but_no_candidate | no_ball_visible | occluded_or_unclear\n")
        f.write("\n[No automatic quality conclusion — review labels first.]\n")
    print(f"[pack_gen] Report -> {rp}")

    # run_summary
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    summary = {
        "version":       VERSION,
        "generated_utc": t0.isoformat(),
        "elapsed_s":     round(elapsed, 1),
        "commit_sha":    os.environ.get("GITHUB_SHA", "unknown"),
        "inputs": {
            "equirect_file_id":          args.equirect_file_id
                                         or os.environ.get("EQUIRECT_FILE_ID", ""),
            "stage1_candidates_file_id": args.stage1_id
                                         or os.environ.get("STAGE1_FILE_ID", ""),
            "hotspot_map_file_id":       args.hotspot_id
                                         or os.environ.get("HOTSPOT_FILE_ID", ""),
        },
        "outputs": {
            "track_b_manifest":              mp,
            "candidate_precision_pack":      p1,
            "zero_candidate_coverage_pack":  p2,
            "track_b_report":               rp,
        },
        "candidate_sample_count": len(cand_recs),
        "zero_sample_count":      len(zero_recs),
        "pass_fail":   "pass",
        "conclusion":  (
            f"Generated {len(cand_recs)} candidate tiles and "
            f"{len(zero_recs)} zero-coverage rows. "
            "Status: COMPLETED — AWAITING REVIEW."
        ),
    }
    sp = os.path.join(args.output_dir, "run_summary.json")
    with open(sp, "w") as f: json.dump(summary, f, indent=2)
    print(f"[pack_gen] run_summary -> {sp}")
    print(f"[pack_gen] Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
