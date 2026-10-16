#!/usr/bin/env python3
"""Historical vs corrected media clock: tracker-stage metrics per run.

Inputs are cpu_replay outputs (report JSON + trace CSV) for the same run on
two clocks. Every metric uses ONE definition for both clocks:

- Durations are measured in source frames converted at the verified source
  rate (60000/1001), never in tracker input time, so both clocks are judged
  on the same video timeline.
- "Tracker time" metrics (grace-window reacquisition) use the clock the
  tracker actually received (`input_ms`), because that is what its rules saw.
- The decoy is a fixed-location PROXY (0.03 rad around (0.967, 0.0075)),
  not identity truth. No correct-ball accuracy is claimed.

Only tracker-stage output is evaluated (backward bridge and panner are not
replayed).

Usage:
  python3 scripts/oev_clock_compare.py --dir OUT --runs 34202680125 ... \
      [--base-clock historical] [--cand-clock production-media] [--markdown-out F]
  python3 scripts/oev_clock_compare.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import statistics
import sys

FPS = 60000 / 1001
DECOY = (0.967, 0.0075)
DECOY_R = 0.03
JUMP_RAD = 0.10  # adjacent-frame Tracking->Tracking jump counted as a switch
GRACE_MS = 750.0  # BallTracker DEFAULT_REACQUIRE_GRACE_MS


def episodes(flags):
    out, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append(i - start)
            start = None
    if start is not None:
        out.append(len(flags) - start)
    return out


def near(y, p, c, r):
    return math.hypot(y - c[0], p - c[1]) <= r


def metrics(report: dict, rows: list[dict]) -> dict:
    n = len(rows)
    st = [r["replay_state"] for r in rows]
    pos = [
        (float(r["replay_yaw"]), float(r["replay_pitch"])) if r["replay_yaw"] else None
        for r in rows
    ]
    t_in = [float(r["input_ms"]) for r in rows]
    tracking = [s == "Tracking" for s in st]
    available = [s in ("Tracking", "Coasting") for s in st]
    decoy = [tracking[i] and near(*pos[i], DECOY, DECOY_R) for i in range(n)]

    # Full-loss -> next fresh Tracking.
    gaps_frames, grace_reacq = [], 0
    lost_at = None
    for i, s in enumerate(st):
        if s == "Lost":
            lost_at = i
        elif s == "Tracking" and lost_at is not None:
            gaps_frames.append(i - lost_at)
            if t_in[i] - t_in[lost_at] < GRACE_MS:
                grace_reacq += 1
            lost_at = None

    jumps = sum(
        1
        for i in range(1, n)
        if tracking[i]
        and tracking[i - 1]
        and math.hypot(pos[i][0] - pos[i - 1][0], pos[i][1] - pos[i - 1][1]) > JUMP_RAD
    )
    seg = episodes(tracking)
    coast = episodes([s == "Coasting" for s in st])
    sites = report["sites"]
    dormant = [t for t in sites["transitions"] if t["kind"] == "dormant"]
    distinct_sites = {(round(t["yaw"], 2), round(t["pitch"], 2)) for t in dormant}
    first_decoy_dormant = next(
        (t["frame"] for t in dormant if t["at_decoy"]), None
    )
    after = (
        sum(decoy[first_decoy_dormant + 1 :]) if first_decoy_dormant is not None else None
    )
    dec_eps = episodes(decoy)
    sec = 1.0 / FPS
    return {
        # stationary detection / dormancy
        "dormant transitions": sites["dormant_transitions"],
        "distinct dormant sites": len(distinct_sites),
        "first dormancy (video s)": round(dormant[0]["frame"] * sec, 1) if dormant else None,
        "actual releases": sites["releases_actual"],
        "releases at decoy": sites["decoy_release_calls"],
        # spare-ball proxy
        "decoy Tracking episodes": len(dec_eps),
        "decoy Tracking total (s)": round(sum(dec_eps) * sec, 1),
        "decoy longest lock (s)": round(max(dec_eps, default=0) * sec, 2),
        "decoy Tracking frames after first decoy dormancy": after,
        # loss / reacquisition
        "full losses": sites["full_losses"],
        "acquisitions": sites["acquisitions"],
        "None frames": st.count("None"),
        "coast episodes": len(coast),
        "median Lost->reacquire (video s)": round(statistics.median(gaps_frames) * sec, 2)
        if gaps_frames
        else None,
        "reacquisitions inside 750 ms grace (tracker clock)": grace_reacq,
        # consistency
        "Tracking frames": sum(tracking),
        "available (Tracking+Coasting) %": round(100 * sum(available) / n, 1),
        "median Tracking segment (video s)": round(statistics.median(seg) * sec, 2) if seg else None,
        f"adjacent Tracking jumps > {JUMP_RAD} rad": jumps,
        "decision hash": report["decision_trace_fnv1a"],
    }


def load(out: pathlib.Path, clock: str, run: str):
    report = json.loads((out / f"p2_{clock}_{run}.json").read_text())
    rows = list(csv.DictReader((out / f"p2_{clock}_{run}.csv").open()))
    return metrics(report, rows)


def render(runs, base_clock, cand_clock, data) -> str:
    lines = [
        f"Base clock: `{base_clock}`; candidate clock: `{cand_clock}`. Tracker stage only.",
        "Decoy metrics are a location proxy, not identity truth.",
        "",
    ]
    for run in runs:
        b, c = data[run]
        lines += [f"### Run {run}", "", f"| Metric | {base_clock} | {cand_clock} |", "|---|---:|---:|"]
        for k in b:
            lines.append(f"| {k} | {b[k]} | {c[k]} |")
        lines.append("")
    return "\n".join(lines)


def self_test() -> int:
    rows = []
    states = ["Tracking"] * 3 + ["Coasting"] * 2 + ["Lost", "None", "Tracking", "Tracking"]
    for i, s in enumerate(states):
        yaw = 0.967 if i < 3 else (0.5 if i < 8 else 0.9)
        rows.append({
            "frame": str(i), "input_ms": str(i * 100.0), "replay_state": s,
            "replay_yaw": "" if s == "None" else str(yaw), "replay_pitch": "" if s == "None" else "0.0075",
        })
    report = {
        "sites": {"transitions": [{"kind": "dormant", "frame": 1, "yaw": 0.967, "pitch": 0.0075, "at_decoy": True}],
                  "dormant_transitions": 1, "releases_actual": 0, "decoy_release_calls": 0,
                  "full_losses": 1, "acquisitions": 2},
        "decision_trace_fnv1a": "x",
    }
    m = metrics(report, rows)
    assert m["decoy Tracking episodes"] == 1, m
    assert m["decoy Tracking frames after first decoy dormancy"] == 1, m
    assert m["reacquisitions inside 750 ms grace (tracker clock)"] == 1, m
    assert m[f"adjacent Tracking jumps > {JUMP_RAD} rad"] == 1, m  # 0.5 -> 0.9
    assert m["coast episodes"] == 1 and m["None frames"] == 1, m
    print("OEV_CLOCK_COMPARE_SELF_TEST=PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=pathlib.Path)
    ap.add_argument("--runs", nargs="*")
    ap.add_argument("--base-clock", default="historical")
    ap.add_argument("--cand-clock", default="production-media")
    ap.add_argument("--markdown-out", type=pathlib.Path)
    ap.add_argument("--json-out", type=pathlib.Path)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.dir or not a.runs:
        ap.error("--dir and --runs are required")
    data = {r: (load(a.dir, a.base_clock, r), load(a.dir, a.cand_clock, r)) for r in a.runs}
    text = render(a.runs, a.base_clock, a.cand_clock, data)
    if a.markdown_out:
        a.markdown_out.write_text(text)
    if a.json_out:
        a.json_out.write_text(json.dumps(data, indent=2))
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
