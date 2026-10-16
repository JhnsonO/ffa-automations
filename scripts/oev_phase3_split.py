#!/usr/bin/env python3
"""Split tracker-stage Tracking frames into real-play / decoy / other-static.

Compares a baseline and candidate cpu_replay trace for one run. Zones (same
for both): `decoy` within 0.035 rad of (0.967, 0.0075); `static` within
1 deg of any site the candidate tracker made dormant; otherwise `play`.
These are LOCATION PROXIES, not ball identity.

Usage: oev_phase3_split.py BASE.csv CAND.csv CAND_REPORT.json [--windows PROFILES.json]
"""
import argparse, collections, csv, json, math

FPS = 60000 / 1001


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_csv"); ap.add_argument("cand_csv"); ap.add_argument("cand_report")
    ap.add_argument("--windows")
    a = ap.parse_args()
    A = list(csv.DictReader(open(a.base_csv))); B = list(csv.DictReader(open(a.cand_csv)))
    rep = json.load(open(a.cand_report))
    sites = {(t["yaw"], t["pitch"]) for t in rep["sites"]["transitions"] if t["kind"] == "dormant"}

    def zone(r):
        if r["replay_state"] != "Tracking":
            return "none"
        y, p = float(r["replay_yaw"]), float(r["replay_pitch"])
        if math.hypot(y - 0.967, p - 0.0075) <= 0.035:
            return "decoy"
        if any(math.hypot(y - sy, p - sp) <= 0.0175 for sy, sp in sites):
            return "static"
        return "play"

    pairs = collections.Counter((zone(x), zone(y)) for x, y in zip(A, B))
    print("| base -> candidate | frames |\n|---|---:|")
    for k, v in sorted(pairs.items(), key=lambda kv: -kv[1]):
        print(f"| {k[0]} -> {k[1]} | {v} |")
    base_play = sum(v for k, v in pairs.items() if k[0] == "play")
    cand_play = sum(v for k, v in pairs.items() if k[1] == "play")
    print(f"\nplay-zone Tracking frames: base {base_play}, candidate {cand_play}")
    print(f"real-play lost (play -> none): {pairs[('play', 'none')]} frames")
    if a.windows:
        prof = json.load(open(a.windows))
        prof = prof.get("profiles", prof)["sample_02_180_quality"]
        wins = prof.get("review_windows") or prof.get("human_review_windows")
        print("\n| window | base play/decoy/static/none % | candidate play/decoy/static/none % |\n|---|---|---|")
        for w in wins:
            s, e = int(w["start_s"] * FPS), int(w["end_s"] * FPS)
            def pct(rows):
                z = [zone(r) for r in rows[s:e]]
                return "/".join(f"{100 * z.count(k) / len(z):.0f}" for k in ("play", "decoy", "static", "none"))
            print(f"| {w['label']} {w['start_s']}-{w['end_s']} s | {pct(A)} | {pct(B)} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
