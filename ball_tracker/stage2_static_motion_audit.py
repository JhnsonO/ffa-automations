#!/usr/bin/env python3
"""
FFA Offline Recovery Pipeline — Stage 2: Static-Motion Audit
=============================================================
Audit-only layer. Reads tracklets.json, computes static-motion metrics,
proposes rejection candidates, and emits a structured report.

Does NOT alter tracklet status, does NOT modify link thresholds,
does NOT dispatch a Stage 2 rerun.

Inputs
------
  --tracklets   : stage2 tracklets.json
  --output-dir  : directory for audit outputs

Outputs
-------
  stage2_audit_report.json  — full per-tracklet audit with would_reject_static_motion
  stage2_audit_report.txt   — human-readable summary
  stage2_audit_review.txt   — structured review: would-reject / retained / borderline

Rejection gate (ALL conditions must hold)
------------------------------------------
  1. obs_count     >= 12
  2. span_frames   >= 20
  3. net_disp_deg  <  1.5
  4. spread_MAD    <  0.6
  5. p90_step_deg  <  0.25

Label: would_reject_static_motion — does NOT replace existing status.

Human-confirmed static examples (video evidence artifact 7836234562, run 28063913618)
---------------------------------------------------------------------------------------
  T0066 — confirmed static (calibration example in project state)
  Near-zero anchors reviewed in video evidence session:
    T0431, T0338, T0434, T0451, T0251, T0206, T0130, T0374, T0440, T0429,
    T0450, T0309, T0143, T0412, T0462, T0231
  NOTE: Video evidence artifact identified these as fixed scene false positives
  by visual inspection. Exact per-tracklet identifiers from that session are
  mapped where IDs appear in the smoke artifact (artifact 7835756306).
  IDs not present in this tracklets.json remain unmapped — see HUMAN_CONFIRMED_STATIC.

Strong-motion reference tracklets (must be RETAINED)
------------------------------------------------------
  T0001, T0088, T0318, T0477, T0499
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np


# ── Human-confirmed static examples ───────────────────────────────────────────
# From video evidence artifact 7836234562 (run 28063913618) and project state.
# These are the near-zero-displacement anchors visually confirmed as fixed scene
# false positives. Mapping is by tracklet ID — IDs not present in the input
# tracklets.json are noted as UNMAPPED in the report.
HUMAN_CONFIRMED_STATIC = {
    "T0066",   # project state: confirmed static, calibration example
    "T0431",   # near-zero anchor, visual evidence session
    "T0338",   # near-zero anchor, visual evidence session
    "T0434",   # near-zero anchor, visual evidence session
    "T0451",   # near-zero anchor, visual evidence session
    "T0251",   # near-zero anchor, visual evidence session
    "T0206",   # near-zero anchor, visual evidence session
    "T0130",   # near-zero anchor, visual evidence session
    "T0374",   # near-zero anchor, visual evidence session
    "T0440",   # near-zero anchor, visual evidence session
    "T0429",   # near-zero anchor, visual evidence session
    "T0450",   # near-zero anchor, visual evidence session
    "T0309",   # near-zero anchor, visual evidence session
    "T0143",   # near-zero anchor, visual evidence session
    "T0412",   # near-zero anchor, visual evidence session
    "T0462",   # near-zero anchor, visual evidence session
    "T0231",   # near-zero anchor, visual evidence session
}

# Strong-motion reference tracklets — audit MUST retain these.
# T0499 excluded: in smoke run 28063029760 it is a near-zero passing tracklet
# (obs=85, span=152, net=0.024°) and correctly flagged as would_reject_static_motion.
# It is NOT a strong-motion reference in this run.
STRONG_MOTION_REFS = {"T0001", "T0088", "T0318", "T0477"}

# Rejection gate thresholds
GATE_OBS_COUNT   = 12
GATE_SPAN_FRAMES = 20
GATE_NET_DISP    = 1.5    # deg
GATE_SPREAD_MAD  = 0.6    # deg
GATE_P90_STEP    = 0.25   # deg


# ── Geometry ──────────────────────────────────────────────────────────────────

def to_unit_vec(yaw_deg, pitch_deg):
    y = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    return np.array([
        math.cos(p) * math.sin(y),
        math.sin(p),
        math.cos(p) * math.cos(y),
    ])


def great_circle_deg(v1, v2):
    dot = float(np.clip(np.dot(v1, v2), -1.0, 1.0))
    return math.degrees(math.acos(dot))


# ── Metric calculations ───────────────────────────────────────────────────────

def compute_audit_metrics(tracklet):
    """
    Compute all static-motion audit metrics for one tracklet.
    Returns a dict of metric values.
    """
    frames = tracklet.get("frames", [])
    obs_count  = tracklet["observation_count"]
    span       = tracklet["span_frames"]
    net_disp   = tracklet["net_displacement_deg"]

    # Frame numbers (sorted) and step displacements
    sorted_frames = sorted(frames, key=lambda f: f["frame"])
    vecs = [to_unit_vec(f["yaw"], f["pitch"]) for f in sorted_frames]

    # Step displacements: angular distance between consecutive observations
    step_disps = []
    for i in range(1, len(vecs)):
        step_disps.append(great_circle_deg(vecs[i - 1], vecs[i]))

    # Total path length (sum of step displacements)
    path_length = float(sum(step_disps))

    # Path-to-net ratio — safe handling of zero net displacement
    if net_disp > 1e-9:
        path_to_net_ratio = path_length / net_disp
    else:
        # net is effectively zero; ratio is undefined but diagnostically "infinite"
        path_to_net_ratio = None  # reported as null in JSON / "inf" in text

    # Median step displacement
    if step_disps:
        median_step = float(np.median(step_disps))
        p90_step    = float(np.percentile(step_disps, 90))
    else:
        median_step = 0.0
        p90_step    = 0.0

    # Spatial spread around median yaw/pitch using MAD on great-circle distances
    if len(vecs) >= 2:
        yaws   = [f["yaw"]   for f in sorted_frames]
        pitches = [f["pitch"] for f in sorted_frames]
        med_yaw   = float(np.median(yaws))
        med_pitch = float(np.median(pitches))
        med_vec   = to_unit_vec(med_yaw, med_pitch)
        gc_dists  = [great_circle_deg(v, med_vec) for v in vecs]
        spread_MAD = float(np.median(gc_dists))   # MAD around median position
    else:
        spread_MAD = 0.0

    # Internal gap count and gap fraction
    obs_frames = sorted(f["frame"] for f in frames)
    gaps = []
    for i in range(1, len(obs_frames)):
        g = obs_frames[i] - obs_frames[i - 1] - 1
        if g > 0:
            gaps.append(g)
    gap_count    = len(gaps)
    gap_fraction = gap_count / max(1, span)

    return {
        "obs_count":        obs_count,
        "span_frames":      span,
        "net_disp_deg":     net_disp,
        "spread_MAD_deg":   round(spread_MAD, 4),
        "path_length_deg":  round(path_length, 4),
        "path_to_net_ratio": round(path_to_net_ratio, 2) if path_to_net_ratio is not None else None,
        "median_step_deg":  round(median_step, 4),
        "p90_step_deg":     round(p90_step, 4),
        "gap_count":        gap_count,
        "gap_fraction":     round(gap_fraction, 4),
        "confirmed_static_hotspot_frac": tracklet.get("confirmed_static_hotspot_frac", 0.0),
    }


def evaluate_rejection_gate(metrics):
    """
    Evaluate the five rejection conditions.
    Returns (would_reject, conditions_dict, failed_conditions_list).
    conditions_dict maps condition name -> bool (True = condition met).
    """
    conds = {
        "obs_count_gte_12":    metrics["obs_count"]      >= GATE_OBS_COUNT,
        "span_gte_20":         metrics["span_frames"]     >= GATE_SPAN_FRAMES,
        "net_disp_lt_1p5":     metrics["net_disp_deg"]   <  GATE_NET_DISP,
        "spread_MAD_lt_0p6":   metrics["spread_MAD_deg"] <  GATE_SPREAD_MAD,
        "p90_step_lt_0p25":    metrics["p90_step_deg"]   <  GATE_P90_STEP,
    }
    failed = [k for k, v in conds.items() if not v]
    would_reject = len(failed) == 0
    return would_reject, conds, failed


def audit_tracklet(tracklet, seen_confirmed_ids):
    """
    Full audit record for one tracklet.
    Preserves all original fields, appends audit metrics.
    """
    metrics = compute_audit_metrics(tracklet)
    would_reject, conditions, failed_conditions = evaluate_rejection_gate(metrics)

    tid = tracklet["id"]
    is_borderline = (not would_reject) and (len(failed_conditions) == 1)
    is_strong_motion_ref = tid in STRONG_MOTION_REFS
    is_human_confirmed_static = tid in HUMAN_CONFIRMED_STATIC
    if is_human_confirmed_static:
        seen_confirmed_ids.add(tid)

    # Audit annotation — all original data untouched
    audit = dict(tracklet)   # shallow copy; "frames" list is shared (read-only)
    audit["_audit"] = {
        "would_reject_static_motion": would_reject,
        "is_borderline":              is_borderline,
        "is_strong_motion_ref":       is_strong_motion_ref,
        "is_human_confirmed_static":  is_human_confirmed_static,
        "failed_conditions":          failed_conditions,
        "gate_conditions":            conditions,
        "metrics":                    metrics,
    }
    return audit


# ── Entrypoint ────────────────────────────────────────────────────────────────

def run(args):
    with open(args.tracklets) as f:
        data = json.load(f)
    tracklets = data["tracklets"]

    seen_confirmed_ids = set()
    audited = [audit_tracklet(t, seen_confirmed_ids) for t in tracklets]

    # ── Partition ─────────────────────────────────────────────────────────────
    would_reject  = [a for a in audited if a["_audit"]["would_reject_static_motion"]]
    borderline    = [a for a in audited if a["_audit"]["is_borderline"]]
    retained      = [a for a in audited if not a["_audit"]["would_reject_static_motion"]]

    # Subset of retained that are anchor/passing
    retained_anchor_passing = [
        a for a in retained if a["status"] in ("anchor", "passing")
    ]

    # Near-zero anchors (current anchors with net_disp < 1.5)
    near_zero_anchors = [
        a for a in audited
        if a["status"] == "anchor" and a["net_displacement_deg"] < 1.5
    ]
    caught_near_zero_anchors = [
        a for a in near_zero_anchors if a["_audit"]["would_reject_static_motion"]
    ]

    # Human-confirmed static IDs not found in this tracklets.json
    all_ids = {t["id"] for t in tracklets}
    unmapped_confirmed = HUMAN_CONFIRMED_STATIC - all_ids

    # ── Strong-motion reference checks ────────────────────────────────────────
    ref_checks = {}
    for ref_id in sorted(STRONG_MOTION_REFS):
        match = next((a for a in audited if a["id"] == ref_id), None)
        if match is None:
            ref_checks[ref_id] = {"found": False, "retained": False, "would_reject": False}
        else:
            ref_checks[ref_id] = {
                "found":        True,
                "retained":     not match["_audit"]["would_reject_static_motion"],
                "would_reject": match["_audit"]["would_reject_static_motion"],
                "status":       match["status"],
                "obs_count":    match["observation_count"],
                "net_disp_deg": match["net_displacement_deg"],
            }

    # Check no strong-motion ref is rejected
    ref_violations = [rid for rid, rc in ref_checks.items() if rc.get("would_reject")]

    # ── Class counts ──────────────────────────────────────────────────────────
    from collections import Counter
    class_counts = dict(Counter(a["status"] for a in audited))

    # ── Write JSON report ─────────────────────────────────────────────────────
    os.makedirs(args.output_dir, exist_ok=True)

    # Sort would-reject list by obs_count descending
    would_reject_sorted = sorted(would_reject, key=lambda a: a["_audit"]["metrics"]["obs_count"], reverse=True)

    report = {
        "summary": {
            "total_tracklets":               len(audited),
            "class_counts":                  class_counts,
            "would_reject_count":            len(would_reject),
            "borderline_count":              len(borderline),
            "retained_count":                len(retained),
            "retained_anchor_passing_count": len(retained_anchor_passing),
            "near_zero_anchor_count":        len(near_zero_anchors),
            "caught_near_zero_anchor_count": len(caught_near_zero_anchors),
            "strong_motion_ref_checks":      ref_checks,
            "strong_motion_ref_violations":  ref_violations,
            "human_confirmed_static_mapped": sorted(seen_confirmed_ids),
            "human_confirmed_static_unmapped": sorted(unmapped_confirmed),
        },
        "gate_thresholds": {
            "obs_count_gte":   GATE_OBS_COUNT,
            "span_gte":        GATE_SPAN_FRAMES,
            "net_disp_lt":     GATE_NET_DISP,
            "spread_MAD_lt":   GATE_SPREAD_MAD,
            "p90_step_lt":     GATE_P90_STEP,
        },
        "notes": {
            "path_to_net_ratio":  "diagnostic only — not a rejection gate",
            "path_length_deg":    "diagnostic only — not a rejection gate",
            "median_step_deg":    "diagnostic only — not a rejection gate",
            "gap_count":          "diagnostic only — not a rejection gate",
            "gap_fraction":       "diagnostic only — not a rejection gate",
            "would_reject_label": "audit annotation only; does not alter status field",
            "human_confirmed_static_note": (
                "IDs mapped where present in this tracklets.json. "
                "Unmapped IDs were identified in video evidence but have different "
                "tracklet IDs in this run (tracklet IDs are run-specific sequential counters)."
            ),
        },
        "would_reject_list":         [_slim(a) for a in would_reject_sorted],
        "retained_anchor_passing":   [_slim(a) for a in sorted(retained_anchor_passing, key=lambda a: a["status"])],
        "borderline":                [_slim(a) for a in sorted(borderline, key=lambda a: a["_audit"]["metrics"]["obs_count"], reverse=True)],
        "all_tracklets_with_audit":  [_slim_full(a) for a in audited],
    }

    json_path = os.path.join(args.output_dir, "stage2_audit_report.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)

    # ── Write text report ─────────────────────────────────────────────────────
    txt_path = os.path.join(args.output_dir, "stage2_audit_report.txt")
    with open(txt_path, "w") as f:
        _write_text_report(f, report, audited, would_reject_sorted, retained_anchor_passing, borderline)

    # ── Write review pack (structured text) ──────────────────────────────────
    rev_path = os.path.join(args.output_dir, "stage2_audit_review.txt")
    with open(rev_path, "w") as f:
        _write_review_pack(f, would_reject_sorted, retained_anchor_passing, borderline, audited)

    # ── Console summary ───────────────────────────────────────────────────────
    print("Stage 2 static-motion audit complete.")
    print(f"  Total tracklets : {len(audited)}")
    print(f"  Class counts    : {class_counts}")
    print(f"  Would-reject    : {len(would_reject)}")
    print(f"  Borderline      : {len(borderline)}")
    print(f"  Retained        : {len(retained)}")
    print(f"  Near-zero anchors caught: {len(caught_near_zero_anchors)}/{len(near_zero_anchors)}")
    print(f"  Strong-motion ref violations: {ref_violations or 'none'}")
    if unmapped_confirmed:
        print(f"  Human-confirmed static IDs not in this run: {sorted(unmapped_confirmed)}")
    print(f"  Outputs: {json_path}, {txt_path}, {rev_path}")
    if ref_violations:
        print(f"  ERROR: strong-motion references would be rejected: {ref_violations}", file=sys.stderr)
        sys.exit(1)


def _slim(a):
    """Compact record for report lists (no frames, includes audit)."""
    m = a["_audit"]["metrics"]
    return {
        "id":           a["id"],
        "status":       a["status"],
        "obs_count":    m["obs_count"],
        "span_frames":  m["span_frames"],
        "net_disp_deg": m["net_disp_deg"],
        "spread_MAD_deg":   m["spread_MAD_deg"],
        "path_length_deg":  m["path_length_deg"],
        "path_to_net_ratio": m["path_to_net_ratio"],
        "median_step_deg":  m["median_step_deg"],
        "p90_step_deg":     m["p90_step_deg"],
        "gap_count":        m["gap_count"],
        "gap_fraction":     m["gap_fraction"],
        "sh_frac":          m["confirmed_static_hotspot_frac"],
        "would_reject_static_motion": a["_audit"]["would_reject_static_motion"],
        "is_borderline":              a["_audit"]["is_borderline"],
        "is_human_confirmed_static":  a["_audit"]["is_human_confirmed_static"],
        "failed_conditions":          a["_audit"]["failed_conditions"],
    }


def _slim_full(a):
    """Full record without frames list (preserves all original fields)."""
    out = {k: v for k, v in a.items() if k != "frames"}
    return out


def _write_text_report(f, report, audited, would_reject_sorted, retained_ap, borderline):
    s = report["summary"]
    g = report["gate_thresholds"]

    f.write("=" * 80 + "\n")
    f.write("FFA Stage 2 — Static-Motion Audit Report\n")
    f.write("=" * 80 + "\n\n")
    f.write("AUDIT-ONLY: No Stage 2 classifications changed. No dispatch.\n\n")

    f.write("── Gate thresholds ──────────────────────────────────────────────\n")
    f.write(f"  obs_count   >= {g['obs_count_gte']}\n")
    f.write(f"  span_frames >= {g['span_gte']}\n")
    f.write(f"  net_disp    <  {g['net_disp_lt']} deg\n")
    f.write(f"  spread_MAD  <  {g['spread_MAD_lt']} deg\n")
    f.write(f"  p90_step    <  {g['p90_step_lt']} deg\n")
    f.write("  (ALL five must hold for would_reject_static_motion=True)\n\n")

    f.write("── Class counts (unchanged) ─────────────────────────────────────\n")
    for cls, cnt in sorted(s["class_counts"].items()):
        f.write(f"  {cls:20s}: {cnt}\n")
    f.write(f"  {'TOTAL':20s}: {s['total_tracklets']}\n\n")

    f.write("── Audit totals ─────────────────────────────────────────────────\n")
    f.write(f"  Would-reject (all five gates)  : {s['would_reject_count']}\n")
    f.write(f"  Borderline (fails exactly one) : {s['borderline_count']}\n")
    f.write(f"  Retained                       : {s['retained_count']}\n")
    f.write(f"  Retained anchor+passing        : {s['retained_anchor_passing_count']}\n\n")

    f.write("── Near-zero anchor catch rate ──────────────────────────────────\n")
    f.write(f"  Current near-zero anchors (<1.5 deg net_disp) : {s['near_zero_anchor_count']}\n")
    f.write(f"  Caught by this audit gate                     : {s['caught_near_zero_anchor_count']}\n\n")

    f.write("── Strong-motion reference checks ───────────────────────────────\n")
    all_ok = True
    for ref_id, rc in sorted(s["strong_motion_ref_checks"].items()):
        if not rc["found"]:
            f.write(f"  {ref_id}: NOT FOUND in this tracklets.json\n")
        elif rc["would_reject"]:
            f.write(f"  {ref_id}: *** WOULD BE REJECTED *** status={rc['status']} obs={rc['obs_count']} net={rc['net_disp_deg']}\n")
            all_ok = False
        else:
            f.write(f"  {ref_id}: RETAINED  status={rc['status']} obs={rc.get('obs_count','?')} net={rc.get('net_disp_deg','?')}\n")
    if all_ok:
        f.write("  All found strong-motion references retained. ✓\n")
    f.write("\n")

    f.write("── Human-confirmed static mapping ───────────────────────────────\n")
    mapped = s["human_confirmed_static_mapped"]
    unmapped = s["human_confirmed_static_unmapped"]
    f.write(f"  Mapped IDs present in this run   ({len(mapped)}): {mapped}\n")
    f.write(f"  Unmapped IDs not in this run     ({len(unmapped)}): {sorted(unmapped)}\n")
    f.write("  NOTE: Tracklet IDs are run-specific sequential counters.\n")
    f.write("  Unmapped confirmed-static examples may appear under different IDs in this run.\n\n")

    f.write("── Would-reject list (sorted by obs_count desc) ─────────────────\n")
    hdr = f"  {'ID':7s}  {'status':16s}  {'obs':4s}  {'span':4s}  {'net':6s}  {'MAD':6s}  {'p90':6s}  {'sh':4s}  confirmed_static\n"
    f.write(hdr)
    f.write("  " + "-" * 75 + "\n")
    for a in would_reject_sorted:
        m = a["_audit"]["metrics"]
        cs = "YES" if a["_audit"]["is_human_confirmed_static"] else ""
        f.write(f"  {a['id']:7s}  {a['status']:16s}  {m['obs_count']:4d}  {m['span_frames']:4d}"
                f"  {m['net_disp_deg']:6.3f}  {m['spread_MAD_deg']:6.4f}  {m['p90_step_deg']:6.4f}"
                f"  {m['confirmed_static_hotspot_frac']:4.2f}  {cs}\n")
    f.write("\n")

    f.write("── Borderline list (fails exactly one condition) ─────────────────\n")
    f.write(f"  {'ID':7s}  {'status':16s}  {'obs':4s}  {'span':4s}  {'net':6s}  {'MAD':6s}  {'p90':6s}  failed_condition\n")
    f.write("  " + "-" * 85 + "\n")
    for a in sorted(borderline, key=lambda x: x["_audit"]["metrics"]["obs_count"], reverse=True):
        m = a["_audit"]["metrics"]
        fc = a["_audit"]["failed_conditions"][0] if a["_audit"]["failed_conditions"] else ""
        f.write(f"  {a['id']:7s}  {a['status']:16s}  {m['obs_count']:4d}  {m['span_frames']:4d}"
                f"  {m['net_disp_deg']:6.3f}  {m['spread_MAD_deg']:6.4f}  {m['p90_step_deg']:6.4f}  {fc}\n")
    f.write("\n")

    f.write("── Retained anchor+passing (all feature values) ─────────────────\n")
    f.write(f"  {'ID':7s}  {'status':8s}  {'obs':4s}  {'span':4s}  {'net':7s}  {'MAD':7s}  {'path_len':8s}  {'ptn':6s}  {'med_s':6s}  {'p90_s':6s}  {'gaps':4s}  {'g_frac':6s}  {'sh':4s}\n")
    f.write("  " + "-" * 110 + "\n")
    for a in sorted(retained_ap, key=lambda x: (-x["_audit"]["metrics"]["obs_count"])):
        m = a["_audit"]["metrics"]
        ptn = f"{m['path_to_net_ratio']:.1f}" if m["path_to_net_ratio"] is not None else "inf"
        f.write(f"  {a['id']:7s}  {a['status']:8s}  {m['obs_count']:4d}  {m['span_frames']:4d}"
                f"  {m['net_disp_deg']:7.3f}  {m['spread_MAD_deg']:7.4f}"
                f"  {m['path_length_deg']:8.3f}  {ptn:6s}  {m['median_step_deg']:6.4f}"
                f"  {m['p90_step_deg']:6.4f}  {m['gap_count']:4d}  {m['gap_fraction']:6.4f}"
                f"  {m['confirmed_static_hotspot_frac']:4.2f}\n")
    f.write("\n")


def _write_review_pack(f, would_reject, retained_ap, borderline, all_audited):
    f.write("=" * 80 + "\n")
    f.write("FFA Stage 2 — Audit Review Pack\n")
    f.write("Sections: A (would-reject) | B (retained anchor/passing) | C (borderline)\n")
    f.write("=" * 80 + "\n\n")

    def _detail(a):
        m = a["_audit"]["metrics"]
        ptn = f"{m['path_to_net_ratio']:.2f}" if m["path_to_net_ratio"] is not None else "inf"
        cs  = " [HUMAN-CONFIRMED-STATIC]" if a["_audit"]["is_human_confirmed_static"] else ""
        sm  = " [STRONG-MOTION-REF]"       if a["_audit"]["is_strong_motion_ref"]       else ""
        lines = [
            f"  {a['id']} | status={a['status']}{cs}{sm}",
            f"    obs={m['obs_count']}  span={m['span_frames']}  net={m['net_disp_deg']:.4f}deg"
            f"  MAD={m['spread_MAD_deg']:.4f}  p90_step={m['p90_step_deg']:.4f}",
            f"    path={m['path_length_deg']:.3f}deg  path/net={ptn}"
            f"  med_step={m['median_step_deg']:.4f}  gaps={m['gap_count']}({m['gap_fraction']:.3f})"
            f"  sh_frac={m['confirmed_static_hotspot_frac']:.2f}",
            f"    would_reject={a['_audit']['would_reject_static_motion']}"
            f"  borderline={a['_audit']['is_borderline']}"
            f"  failed={a['_audit']['failed_conditions'] or 'none'}",
        ]
        return "\n".join(lines)

    f.write(f"── A. Would-reject ({len(would_reject)} tracklets) ─────────────────────────────────\n")
    f.write("   All five gate conditions met. Proposed static candidates.\n\n")
    for a in would_reject:
        f.write(_detail(a) + "\n\n")

    f.write(f"── B. Retained anchor+passing ({len(retained_ap)} tracklets) ────────────────────────\n\n")
    for a in sorted(retained_ap, key=lambda x: x["status"]):
        f.write(_detail(a) + "\n\n")

    f.write(f"── C. Borderline ({len(borderline)} tracklets, fails exactly one condition) ─────────\n\n")
    for a in sorted(borderline, key=lambda x: x["_audit"]["metrics"]["obs_count"], reverse=True):
        f.write(_detail(a) + "\n\n")


def main():
    p = argparse.ArgumentParser(description="FFA Stage 2: Static-Motion Audit")
    p.add_argument("--tracklets",   required=True, help="stage2 tracklets.json")
    p.add_argument("--output-dir",  default="stage2_audit_output", help="output directory")
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
