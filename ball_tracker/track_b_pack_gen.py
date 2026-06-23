#!/usr/bin/env python3
"""
FFA Track B — Review Pack Generator                       track_b_pack_gen_v2
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

VERSION        = "track_b_pack_gen_v2"
CAND_SAMPLES   = 60
NON_TOP_QUOTA  = 10                    # of CAND_SAMPLES, reserved for non-top-rank candidates
TOP_QUOTA      = CAND_SAMPLES - NON_TOP_QUOTA   # 50
CAND_TILE_SIZE = 256
CAND_TILE_COLS = 10
CAND_TILE_FOV  = 45.0                  # degrees – tight zoom centred on candidate
CAND_HDR_H     = 20                    # header: frame ID only
CAND_LBL_H     = 40                    # label slot area below crop
RETICLE_RADIUS = 12                    # crosshair arm half-length px
RETICLE_GAP    = 4                     # gap around centre px
LABELS_CAND    = [
    "ball_at_centre", "ball_nearby_but_offset",
    "not_ball",       "occluded_or_unclear",
]
ZERO_SAMPLES   = 15
ZERO_CROP_W    = 320
ZERO_CROP_H    = 180                   # 16:9
ZERO_CROP_YAWS = [0, 90, 180, 270]    # Stage 1 fixed yaws
ZERO_FULL_FOV  = 110.0                 # matches Stage 1
ZERO_ROW_HDR_H = 20
SEED           = 42
HOT_THRESH     = 0.5                   # penalty < this → hotspot_adjacent (manifest only)
NEU_THRESH     = 0.9                   # all penalties >= this → hotspot_neutral (manifest only)


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
    """Compute hidden manifest strata for a candidate frame."""
    labels = {_temporal_stratum(fi, total_frames)}
    n = len(cands)
    if n == 1:   labels.add("single_candidate")
    elif n >= 2: labels.add("multi_candidate")
    if n >= 3:   labels.add("cluttered")
    top_wc = max(c["weighted_conf"] for c in cands)
    if top_wc >= high_t: labels.add("high_conf")
    if top_wc <= low_t:  labels.add("low_conf")
    penalties = [c.get("penalty", 1.0) for c in cands]
    if any(p < HOT_THRESH for p in penalties):  labels.add("hotspot_adjacent")
    if all(p >= NEU_THRESH for p in penalties):  labels.add("hotspot_neutral")
    return labels


def _label_nontop_frame(fi, cands, total_frames):
    """Reduced manifest strata for non-top samples (no conf thresholds needed)."""
    labels = {_temporal_stratum(fi, total_frames)}
    n = len(cands)
    if n >= 3: labels.add("cluttered")
    penalties = [c.get("penalty", 1.0) for c in cands]
    if any(p < HOT_THRESH for p in penalties): labels.add("hotspot_adjacent")
    return labels


def _make_cand_record(fi, cands, lbls, cand, rank, fps, sample_type):
    """
    Build a candidate review record.
    strata are hidden from tiles — manifest/report provenance only.
    reviewed_rank: 0 = highest weighted_conf in frame; 1 = second-highest; etc.
    """
    return {
        "frame_idx":         fi,
        "timestamp_s":       round(fi / fps, 3) if fps else None,
        "strata":            sorted(lbls),    # hidden from tile
        "sample_type":       sample_type,     # "top" | "non_top"
        "candidate_count":   len(cands),
        "reviewed_rank":     rank,
        "reviewed_yaw":      round(cand["yaw"],   3),
        "reviewed_pitch":    round(cand["pitch"], 3),
        "reviewed_source":   cand.get("source"),
        "reviewed_crop_yaw": cand.get("crop_yaw"),
        # Stage 1c: pass detection_geometry through when present; None otherwise.
        # Do not filter or re-weight on this field here.
        "detection_geometry": cand.get("detection_geometry"),
    }


def build_cand_samples(frames_raw, total_frames, fps):
    """Return (records, stratum_counts) for TOP_QUOTA top-candidate tiles."""
    random.seed(SEED)
    cf = {fi: cands for fi, cands in frames_raw.items() if cands}
    all_wc = sorted(max(c["weighted_conf"] for c in v) for v in cf.values())
    n = len(all_wc)
    high_t = all_wc[int(0.75 * n)] if n else 1.0
    low_t  = all_wc[int(0.25 * n)] if n else 0.0

    labeled = {fi: _label_cand_frame(fi, cands, total_frames, high_t, low_t)
               for fi, cands in cf.items()}

    budget = [
        ("temporal_early",   10), ("temporal_mid",    10), ("temporal_late",   10),
        ("high_conf",         7), ("low_conf",          7), ("multi_candidate",  6),
        ("hotspot_adjacent",  5), ("single_candidate",  5), ("hotspot_neutral",  4),
        ("cluttered",         4),
    ]  # total budget 68 > 50; dedup trims to TOP_QUOTA

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
    if len(sel) > TOP_QUOTA:
        def rarity(fi):
            return sum(len(pools.get(l, [])) for l in labeled[fi])
        sel.sort(key=rarity)
        sel = sorted(sel[:TOP_QUOTA])
    elif len(sel) < TOP_QUOTA:
        rem = [fi for fi in sorted(cf.keys()) if fi not in selected]
        random.shuffle(rem)
        sel = sorted(sel + rem[:TOP_QUOTA - len(sel)])

    stratum_counts = {}
    records = []
    for fi in sel:
        cands = cf[fi]
        top   = max(cands, key=lambda c: c["weighted_conf"])
        lbls  = labeled[fi]
        for l in sorted(lbls):
            stratum_counts[l] = stratum_counts.get(l, 0) + 1
        records.append(_make_cand_record(fi, cands, lbls, top, 0, fps, "top"))
    return records, stratum_counts


def build_nontop_samples(frames_raw, total_frames, fps, excluded_fis, quota=NON_TOP_QUOTA):
    """
    Return records for rank-1 (second-highest weighted_conf) candidates from
    multi-candidate frames, spread across temporal thirds.
    Audits Stage 2-relevant false candidates that are not top-ranked per frame.
    """
    random.seed(SEED + 2)
    multi_fis = sorted(
        fi for fi, cands in frames_raw.items()
        if len(cands) >= 2 and fi not in excluded_fis
    )
    if not multi_fis:
        return []

    pools = {s: [] for s in ("temporal_early", "temporal_mid", "temporal_late")}
    for fi in multi_fis:
        pools[_temporal_stratum(fi, total_frames)].append(fi)

    per = quota // 3
    selected = []
    for pool in pools.values():
        random.shuffle(pool)
        selected.extend(pool[:per])
    rem = [fi for fi in multi_fis if fi not in set(selected)]
    random.shuffle(rem)
    selected.extend(rem[:quota - len(selected)])
    selected = sorted(selected[:quota])

    records = []
    for fi in selected:
        cands   = frames_raw[fi]
        sorted_c = sorted(cands, key=lambda c: c["weighted_conf"], reverse=True)
        cand    = sorted_c[1]   # rank 1 = second-highest weighted_conf
        lbls    = _label_nontop_frame(fi, cands, total_frames)
        records.append(_make_cand_record(fi, cands, lbls, cand, 1, fps, "non_top"))
    return records


def build_zero_samples(frames_raw, total_frames, fps):
    """Return (records, stratum_counts) for ZERO_SAMPLES zero-candidate frames."""
    random.seed(SEED + 1)
    zero_fis = sorted(
        fi for fi in range(total_frames)
        if not frames_raw.get(fi)
    )
    pools = {"temporal_early": [], "temporal_mid": [], "temporal_late": []}
    for fi in zero_fis:
        pools[_temporal_stratum(fi, total_frames)].append(fi)

    per = ZERO_SAMPLES // 3   # 5 per temporal third
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


def _draw_reticle(draw, cx, cy):
    """Red crosshair with centre gap so the candidate position is unambiguous."""
    col = (255, 50, 50)
    r, g = RETICLE_RADIUS, RETICLE_GAP
    draw.line([(cx - r, cy), (cx - g, cy)], fill=col, width=1)
    draw.line([(cx + g, cy), (cx + r, cy)], fill=col, width=1)
    draw.line([(cx, cy - r), (cx, cy - g)], fill=col, width=1)
    draw.line([(cx, cy + g), (cx, cy + r)], fill=col, width=1)
    draw.ellipse([(cx - 2, cy - 2), (cx + 2, cy + 2)], outline=col, width=1)


def make_cand_tile(frame_bgr, rec):
    """
    Tile layout (top→bottom):
      CAND_HDR_H  — frame ID only  (no strata, no conf info)
      CAND_TILE_SIZE — perspective crop + centre reticle
      CAND_LBL_H  — 4 label slots (2×2 grid) for manual review
    """
    crop = extract_perspective(
        frame_bgr, rec["reviewed_yaw"], rec["reviewed_pitch"],
        CAND_TILE_FOV, CAND_TILE_SIZE, CAND_TILE_SIZE)

    tile_h = CAND_HDR_H + CAND_TILE_SIZE + CAND_LBL_H
    tile   = Image.new("RGB", (CAND_TILE_SIZE, tile_h), (18, 18, 18))
    font   = _try_font(9)
    draw   = ImageDraw.Draw(tile)

    # Header — frame ID only
    draw.text((3, 3), f"F{rec['frame_idx']}", fill=(220, 220, 60), font=font)

    # Crop with centre reticle
    crop_img = _to_pil(crop)
    _draw_reticle(ImageDraw.Draw(crop_img), CAND_TILE_SIZE // 2, CAND_TILE_SIZE // 2)
    tile.paste(crop_img, (0, CAND_HDR_H))

    # Label slots — 2×2 grid below crop
    lbl_top = CAND_HDR_H + CAND_TILE_SIZE + 2
    col_w   = CAND_TILE_SIZE // 2
    box_h   = (CAND_LBL_H - 6) // 2
    for i, lbl in enumerate(LABELS_CAND):
        col_i, row_i = i % 2, i // 2
        x0 = col_i * col_w + 2
        y0 = lbl_top + row_i * (box_h + 2)
        x1 = x0 + col_w - 4
        y1 = y0 + box_h
        draw.rectangle([(x0, y0), (x1, y1)], outline=(55, 55, 55))
        draw.text((x0 + 3, y0 + 2), lbl, fill=(150, 150, 150), font=font)
    return tile


def make_zero_row(frame_bgr, rec):
    """
    1280×(ZERO_CROP_H+ZERO_ROW_HDR_H) row: header + 4 fixed crops at 0/90/180/270°.
    Temporal stratum label is valid provenance; no pitch-zone stratification claimed.
    """
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
        ImageDraw.Draw(cp).text((3, 3), f"{cyaw}°", fill=(255, 255, 80), font=font)
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
    th = CAND_HDR_H + CAND_TILE_SIZE + CAND_LBL_H
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

    top_recs, top_sc = build_cand_samples(frames_raw, total_frames, fps)
    nontop_recs      = build_nontop_samples(
                           frames_raw, total_frames, fps,
                           excluded_fis={r["frame_idx"] for r in top_recs})
    # Merge and sort by frame index
    cand_recs = sorted(top_recs + nontop_recs, key=lambda r: r["frame_idx"])

    zero_recs, zero_sc = build_zero_samples(frames_raw, total_frames, fps)
    print(f"[pack_gen] {len(cand_recs)} candidate samples "
          f"({len(top_recs)} top, {len(nontop_recs)} non-top), "
          f"{len(zero_recs)} zero samples")

    # Manifest — strata are hidden from tiles but recorded here for provenance
    manifest = {
        "version":       VERSION,
        "seed":          SEED,
        "total_frames":  total_frames,
        "fps":           fps,
        "generated_utc": t0.isoformat(),
        "candidate_samples": {
            "count":          len(cand_recs),
            "top_count":      len(top_recs),
            "non_top_count":  len(nontop_recs),
            "stratum_counts": top_sc,      # calibrated counts from top-sample pool
            "frames":         cand_recs,   # includes reviewed_rank, reviewed_yaw/pitch,
                                           # reviewed_source/crop_yaw, strata, sample_type
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

    p1, p2 = render_packs(args.equirect, cand_recs, zero_recs, args.output_dir)

    # Report — stratum counts only; no quality conclusion
    rp = os.path.join(args.output_dir, "track_b_report.txt")
    with open(rp, "w") as f:
        f.write(f"FFA Track B Audit Report\n")
        f.write(f"Version  : {VERSION}\n")
        f.write(f"Generated: {t0.isoformat()}\n")
        f.write(f"Seed     : {SEED}\n\n")
        f.write(f"Pack 1 — candidate_precision_review_pack.png "
                f"({len(cand_recs)} samples: {len(top_recs)} top, {len(nontop_recs)} non-top)\n")
        f.write(f"  Top-candidate stratum counts (calibrated):\n")
        for k, v in sorted(top_sc.items()):
            f.write(f"    {k:<28}: {v}\n")
        f.write(f"\nPack 2 — zero_candidate_coverage_review_pack.png ({len(zero_recs)} samples)\n")
        for k, v in sorted(zero_sc.items()):
            f.write(f"  {k:<28}: {v}\n")
        f.write("\nLabel options — candidate tiles:\n")
        f.write("  ball_at_centre | ball_nearby_but_offset | not_ball | occluded_or_unclear\n")
        f.write("\nLabel options — zero-candidate tiles:\n")
        f.write("  ball_visible_but_no_candidate | no_ball_visible | occluded_or_unclear\n")
        f.write("\n[No automatic quality conclusion — review labels first.]\n")
    print(f"[pack_gen] Report -> {rp}")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    summary = {
        "version":            VERSION,
        "generated_utc":      t0.isoformat(),
        "elapsed_s":          round(elapsed, 1),
        "commit_sha":         os.environ.get("GITHUB_SHA", "unknown"),
        "inputs": {
            "equirect_file_id":          args.equirect_file_id
                                         or os.environ.get("EQUIRECT_FILE_ID", ""),
            "stage1_candidates_file_id": args.stage1_id
                                         or os.environ.get("STAGE1_FILE_ID", ""),
            "hotspot_map_file_id":       args.hotspot_id
                                         or os.environ.get("HOTSPOT_FILE_ID", ""),
        },
        "outputs": {
            "track_b_manifest":             mp,
            "candidate_precision_pack":     p1,
            "zero_candidate_coverage_pack": p2,
            "track_b_report":              rp,
        },
        "candidate_sample_count": len(cand_recs),
        "top_sample_count":       len(top_recs),
        "non_top_sample_count":   len(nontop_recs),
        "zero_sample_count":      len(zero_recs),
        "pass_fail":  "pass",
        "conclusion": (
            f"Generated {len(cand_recs)} candidate tiles "
            f"({len(top_recs)} top, {len(nontop_recs)} non-top) and "
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
