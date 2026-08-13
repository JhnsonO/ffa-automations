#!/usr/bin/env python3
"""OEV video sample-dataset generator.

Given a full-source stereo pair (left.mp4 / right.mp4) already on local
disk, picks 5 randomized, non-clustered start timestamps spread across the
usable footage and cuts, for each location and each camera side, a 180s
segment via stream copy (no re-encode). The 60s and 30s versions are then
derived from that same 180s cut (also stream copy) rather than re-cut from
the source, so every duration at a given location shares an identical
start point and no location is decoded more than once.

Writes a manifest.json describing exactly how the set was generated so it
can be reproduced or audited later.

Usage:
    python3 oev_generate_sample_dataset.py \
        --left left_full.mp4 --right right_full.mp4 \
        --left-name GX010197.MP4 --right-name GX010173.MP4 \
        --outdir /tmp/oev_samples --seed 12345

If --seed is omitted, a fresh random seed is generated and recorded in the
manifest so the run can still be reproduced later.
"""

import argparse
import json
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DURATIONS = (30, 60, 180)
MAX_DURATION = max(DURATIONS)
NUM_SAMPLES = 5
# Minimum gap enforced between chosen start timestamps, as a fraction of
# usable video length -- keeps the 5 locations from clustering together.
# Relaxed automatically (halved, capped at a few retries) if the usable
# window is too short to fit 5 samples at the initial gap.
MIN_GAP_FRACTION = 0.12


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def pick_start_timestamps(usable_end: float, num_samples: int, seed: int) -> list:
    """Pick num_samples random start timestamps in [0, usable_end], spread
    apart by at least MIN_GAP_FRACTION * usable_end, relaxing the gap if
    the window is too short. Deterministic given the same seed+usable_end.
    """
    rng = random.Random(seed)
    if usable_end <= 0:
        raise ValueError("Source video too short: no room for a single 180s sample")

    gap_fraction = MIN_GAP_FRACTION
    for relax_attempt in range(6):
        min_gap = usable_end * gap_fraction
        for attempt in range(500):
            candidates = sorted(rng.uniform(0, usable_end) for _ in range(num_samples))
            if all(candidates[i + 1] - candidates[i] >= min_gap for i in range(len(candidates) - 1)):
                return [round(c, 2) for c in candidates]
        gap_fraction /= 2  # relax and try again
    # Fall back to an even spread if random rejection sampling never
    # converges (only happens on pathologically short usable windows).
    if num_samples == 1:
        return [round(usable_end / 2, 2)]
    step = usable_end / (num_samples - 1) if num_samples > 1 else 0
    return [round(i * step, 2) for i in range(num_samples)]


def run_ffmpeg_cut(src: Path, start_s: float, duration_s: int, dest: Path, log_lines: list):
    cmd = ["ffmpeg", "-y", "-ss", str(start_s), "-i", str(src), "-t", str(duration_s), "-c", "copy", str(dest)]
    log_lines.append(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    log_lines.append(result.stderr[-2000:])
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg cut failed for {dest.name} (rc={result.returncode})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--left", required=True, type=Path, help="Path to full-source left camera file")
    ap.add_argument("--right", required=True, type=Path, help="Path to full-source right camera file")
    ap.add_argument("--left-name", required=True, help="Source left filename as identifier (e.g. GX010197.MP4)")
    ap.add_argument("--right-name", required=True, help="Source right filename as identifier (e.g. GX010173.MP4)")
    ap.add_argument("--outdir", required=True, type=Path)
    ap.add_argument("--seed", type=int, default=None, help="Random seed; omit for a fresh random seed")
    ap.add_argument("--num-samples", type=int, default=NUM_SAMPLES)
    args = ap.parse_args()

    seed = args.seed if args.seed is not None else random.SystemRandom().randint(0, 2**31 - 1)

    left_dur = ffprobe_duration(args.left)
    right_dur = ffprobe_duration(args.right)
    source_duration = min(left_dur, right_dur)
    usable_end = source_duration - MAX_DURATION
    if usable_end <= 0:
        print(f"FATAL: usable window <= 0 (source_duration={source_duration}s, "
              f"need >= {MAX_DURATION}s of footage after any start point)", file=sys.stderr)
        sys.exit(1)

    starts = pick_start_timestamps(usable_end, args.num_samples, seed)

    args.outdir.mkdir(parents=True, exist_ok=True)
    log_lines = [
        f"left_source={args.left} ({left_dur}s)",
        f"right_source={args.right} ({right_dur}s)",
        f"source_duration_used={source_duration}s (min of left/right)",
        f"usable_end={usable_end}s (source_duration - {MAX_DURATION}s)",
        f"seed={seed}",
        f"starts={starts}",
    ]

    samples_manifest = []
    for idx, start_s in enumerate(starts, start=1):
        tag = f"sample_{idx:02d}"
        sample_dir = args.outdir / tag
        sample_dir.mkdir(parents=True, exist_ok=True)
        for side, src in (("left", args.left), ("right", args.right)):
            # Cut the longest duration once, then derive the shorter
            # durations from that cut (still stream copy, no re-decode of
            # the full source for each duration).
            longest = max(DURATIONS)
            longest_path = sample_dir / f"{tag}_{side}_{longest}s.mp4"
            run_ffmpeg_cut(src, start_s, longest, longest_path, log_lines)
            for duration_s in sorted(d for d in DURATIONS if d != longest):
                dest = sample_dir / f"{tag}_{side}_{duration_s}s.mp4"
                run_ffmpeg_cut(longest_path, 0, duration_s, dest, log_lines)
        samples_manifest.append({"index": idx, "tag": tag, "start_s": start_s})

    sample_set_id = f"{Path(args.left_name).stem}-seed{seed}"
    manifest = {
        "source_left": args.left_name,
        "source_right": args.right_name,
        "sample_set_id": sample_set_id,
        "seed": seed,
        "source_duration_s": round(source_duration, 2),
        "samples": samples_manifest,
        "durations_s": list(DURATIONS),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    manifest_path = args.outdir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    log_path = args.outdir / "generate.log"
    log_path.write_text("\n".join(log_lines))

    print(f"sample_set_id={sample_set_id}")
    print(f"seed={seed}")
    print(f"Wrote {len(samples_manifest)} samples x 2 sides x {len(DURATIONS)} durations to {args.outdir}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
