#!/usr/bin/env python3
"""Compare two OEV CPU replay reports (from video-stitcher cpu_replay).

Produces a better/worse/unchanged table for one baseline report and one
candidate report. Lower is better for decoy-location occupancy, releases,
blackouts and full losses; parity mismatches are reported, not scored.

The decoy metric is a fixed-location PROXY (radius 0.03 rad around
(0.967, 0.0075)), not identity truth. No correct-ball accuracy is claimed:
there are no human identity annotations for these runs.

Usage:
  python3 scripts/oev_replay_compare.py BASE.json CANDIDATE.json [--markdown-out F]
  python3 scripts/oev_replay_compare.py --self-test
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

# (label, path into report, direction) direction: -1 lower is better,
# +1 higher is better, 0 informational only.
METRICS = [
    ("decoy proxy: tracker Tracking episodes", ("decoy_location_proxy", "tracker_stage_tracking", "episodes"), -1),
    ("decoy proxy: tracker Tracking total s", ("decoy_location_proxy", "tracker_stage_tracking", "total_s"), -1),
    ("decoy proxy: longest tracker lock s", ("decoy_location_proxy", "tracker_stage_tracking", "longest_s"), -1),
    ("dormant transitions", ("sites", "dormant_transitions"), 0),
    ("release calls attempted", ("sites", "release_calls_attempted"), 0),
    ("releases actual", ("sites", "releases_actual"), -1),
    ("decoy release calls (attempts)", ("sites", "decoy_release_calls"), 0),
    ("full losses", ("sites", "full_losses"), -1),
    ("tracker Tracking frames", ("tracker_stage_states", "Tracking"), 0),
    ("tracker None frames", ("tracker_stage_states", "None"), 0),
    ("parity mismatches vs recorded", ("parity_vs_recorded", "mismatches"), 0),
]

EPS = 1e-9


def get(report: dict, path: tuple) -> float | None:
    cur = report
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur if isinstance(cur, (int, float)) else None


def compare(base: dict, cand: dict) -> list[dict]:
    rows = []
    for label, path, direction in METRICS:
        b, c = get(base, path), get(cand, path)
        if b is None and c is None:
            continue
        b0 = 0 if b is None else b
        c0 = 0 if c is None else c
        if abs(c0 - b0) <= EPS:
            verdict = "unchanged"
        elif direction == 0:
            verdict = "changed"
        elif (c0 - b0) * direction > 0:
            verdict = "better"
        else:
            verdict = "worse"
        rows.append({"metric": label, "base": b0, "candidate": c0, "delta": c0 - b0, "verdict": verdict})
    return rows


def render(base: dict, cand: dict, rows: list[dict]) -> str:
    def fmt(v):
        return f"{v:.3f}" if isinstance(v, float) else str(v)

    lines = [
        f"Base: `{base.get('label')}` clock={base.get('clock')} hash={base.get('decision_trace_fnv1a')}",
        f"Candidate: `{cand.get('label')}` clock={cand.get('clock')} hash={cand.get('decision_trace_fnv1a')}",
        "",
        "| Metric | Base | Candidate | Delta | Verdict |",
        "|---|---:|---:|---:|---|",
    ]
    for r in rows:
        lines.append(f"| {r['metric']} | {fmt(r['base'])} | {fmt(r['candidate'])} | {fmt(r['delta'])} | {r['verdict']} |")
    if base.get("decision_trace_fnv1a") == cand.get("decision_trace_fnv1a"):
        lines.append("\nDecision traces are identical.")
    lines.append("\nDecoy metrics are a location proxy, not identity truth.")
    return "\n".join(lines) + "\n"


def self_test() -> int:
    base = {
        "label": "b", "clock": "historical", "decision_trace_fnv1a": "1",
        "decoy_location_proxy": {"tracker_stage_tracking": {"episodes": 10, "total_s": 5.0, "longest_s": 1.0}},
        "sites": {"releases_actual": 4, "full_losses": 3, "dormant_transitions": 2},
        "parity_vs_recorded": {"mismatches": 0},
    }
    cand = json.loads(json.dumps(base))
    cand["label"] = "c"
    cand["decision_trace_fnv1a"] = "2"
    cand["decoy_location_proxy"]["tracker_stage_tracking"]["total_s"] = 2.0
    cand["sites"]["releases_actual"] = 6
    rows = {r["metric"]: r["verdict"] for r in compare(base, cand)}
    assert rows["decoy proxy: tracker Tracking total s"] == "better", rows
    assert rows["releases actual"] == "worse", rows
    assert rows["decoy proxy: tracker Tracking episodes"] == "unchanged", rows
    assert rows["dormant transitions"] == "unchanged", rows
    same = compare(base, base)
    assert all(r["verdict"] == "unchanged" for r in same)
    assert "identical" in render(base, base, same)
    print("OEV_REPLAY_COMPARE_SELF_TEST=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", type=pathlib.Path)
    parser.add_argument("candidate", nargs="?", type=pathlib.Path)
    parser.add_argument("--markdown-out", type=pathlib.Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.base or not args.candidate:
        parser.error("base and candidate reports are required")
    base = json.loads(args.base.read_text())
    cand = json.loads(args.candidate.read_text())
    text = render(base, cand, compare(base, cand))
    if args.markdown_out:
        args.markdown_out.write_text(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
