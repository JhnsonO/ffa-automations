#!/usr/bin/env python3
"""
Clip Extractor — downloads a YouTube video at the best available quality
(4K preferred, 1080p minimum) and cuts it into clips at the timestamps
provided. Re-encodes to H.264/yuv420p with faststart so the output plays
cleanly in CapCut, iOS Photos, and the YouTube/Instagram editors.

Usage:
    python clip_extractor.py \
        --url "https://youtu.be/XXXX" \
        --timestamps "00:01:23 00:01:45 goal1\n00:02:10 00:02:30"

Timestamp format (one clip per line OR separated by semicolons):
    START END [name]
    e.g.  00:01:23 00:01:45 goal_pitch_a
          1:23-1:45 quick_clip
          83 105 short_form
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


def parse_timestamp(ts: str) -> float:
    """Parse HH:MM:SS, MM:SS, or raw seconds into float seconds."""
    ts = ts.strip()
    if not ts:
        raise ValueError("Empty timestamp")
    if ":" not in ts:
        return float(ts)
    parts = [float(p) for p in ts.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Invalid timestamp: {ts}")


def parse_clips(raw: str):
    """Parse timestamp spec into [(start_s, end_s, name)]."""
    clips = []
    # Accept newlines OR semicolons as line separators
    lines = re.split(r"[\n;]+", raw)
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Tokens separated by whitespace OR a dash between the two times
        tokens = re.split(r"\s+|(?<=\d)-(?=\d)", line, maxsplit=2)
        tokens = [t for t in tokens if t]
        if len(tokens) < 2:
            print(f"⚠️  Skipping invalid line {i}: {line!r}", file=sys.stderr)
            continue
        try:
            start = parse_timestamp(tokens[0])
            end = parse_timestamp(tokens[1])
        except ValueError as e:
            print(f"⚠️  Skipping line {i}: {e}", file=sys.stderr)
            continue
        if end <= start:
            print(f"⚠️  Skipping line {i}: end <= start", file=sys.stderr)
            continue
        name = tokens[2] if len(tokens) > 2 else f"clip_{i:02d}"
        name = re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_") or f"clip_{i:02d}"
        clips.append((start, end, name))
    return clips


def run(cmd):
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def get_video_title(url: str) -> str:
    res = subprocess.run(
        ["yt-dlp", "--get-title", "--no-playlist", url],
        capture_output=True, text=True, check=True,
    )
    title = res.stdout.strip()
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", title).strip("_")
    return safe[:80] or "video"


def download_source(url: str, work_dir: Path) -> Path:
    """Download the best available quality, preferring 4K, requiring ≥1080p."""
    out_template = str(work_dir / "source.%(ext)s")
    # Format ladder: 4K mp4 → 4K any → 1080p mp4 → 1080p any → best available
    fmt = (
        "bestvideo[height>=2160][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height>=2160]+bestaudio/"
        "bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height>=1080]+bestaudio/"
        "bestvideo+bestaudio/best"
    )
    run([
        "yt-dlp",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", out_template,
        "--no-playlist",
        "--no-progress",
        url,
    ])
    matches = list(work_dir.glob("source.*"))
    if not matches:
        raise RuntimeError("Source download produced no file")
    return matches[0]


def cut_clip(source: Path, start: float, end: float, name: str, out_dir: Path) -> Path:
    """Cut a clip with re-encode for CapCut/iOS compatibility."""
    out = out_dir / f"{name}.mp4"
    duration = end - start
    run([
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "18",          # visually lossless-ish, preserves 4K quality
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-loglevel", "warning",
        "-stats",
        str(out),
    ])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--timestamps", required=True)
    ap.add_argument("--out", default="clips")
    args = ap.parse_args()

    spec = parse_clips(args.timestamps)
    if not spec:
        print("❌ No valid clips parsed from --timestamps", file=sys.stderr)
        sys.exit(1)

    work = Path("work"); work.mkdir(exist_ok=True)
    print(f"🔎 Resolving title for {args.url}")
    title = get_video_title(args.url)
    out_dir = Path(args.out) / title
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"📥 Downloading source ({len(spec)} clip(s) queued)")
    source = download_source(args.url, work)
    print(f"✅ Source: {source}")

    for idx, (start, end, name) in enumerate(spec, 1):
        print(f"\n✂️  [{idx}/{len(spec)}] {name}  ({start:.2f}s → {end:.2f}s)")
        cut_clip(source, start, end, name, out_dir)

    print(f"\n✅ Done. {len(spec)} clip(s) in: {out_dir}")
    # Write a small manifest for the workflow to use
    manifest = out_dir / "_manifest.txt"
    manifest.write_text(
        f"Source: {args.url}\nTitle: {title}\nClips: {len(spec)}\n"
        + "\n".join(f"  {n}  {s:.2f}-{e:.2f}" for s, e, n in spec)
    )


if __name__ == "__main__":
    main()
