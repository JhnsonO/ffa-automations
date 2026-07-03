#!/usr/bin/env python3
"""
playcam/chunked_pipeline.py

Chunk-native orchestrator: solves the disk and per-call timeout limits hit
running Phase 1/2 on a single large local file, and is the architecture
needed for full-match renders anyway.

Never holds more than ~1 chunk of raw source video on disk at a time.
No ball_tracker/ imports or edits.

Steps:
  1. Download each chunk from Drive with a trailing overlap (so track
     association has context right up to the boundary), run Phase 1
     (play_location.py) on it, adjust timestamps to global time, delete
     the raw chunk.
  2. Merge all chunks' jsonl into one continuous global timeline, trimming
     each chunk's overlap tail (except the last chunk) so there are no
     duplicate timestamps at the joins.
  3. Run Phase 2 smoothing (smooth_camera_path.py) ONCE over the full
     merged global timeline -- this is what makes the joins smooth; the
     kinematic ease doesn't know or care that the source was chunked.
  4. Re-download each chunk (exact span this time, no overlap needed) and
     render it clean (smooth_camera_path.py --render-clean) against the
     global camera timeline, delete the raw chunk again.
  5. Concatenate all clean chunk renders via ffmpeg concat demuxer with
     -c copy (no re-encode) into the final output.

Usage:
  python3 playcam/chunked_pipeline.py \
      --file-id 1z2p2FgLsjgvIIBw0HZXWEenckMLWpVNX \
      --start 30 --total-duration 90 --chunk-duration 20 --overlap 3 \
      --venue-profile playcam/venue_profiles/st_margarets.json \
      --output playcam/output/full_render.mp4
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PLAYCAM_DIR = Path(__file__).parent
WORK_DIR = PLAYCAM_DIR / "output" / "chunked"

CLIENT_ID = "1062583777620-th4s8tiqbv69neq9icdj8hcn9vvh9nns.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REFRESH_TOKEN = ("1//03eBp3pTvBn7lCgYIARAAGAMSNwF-L9Ir9lWZeZoQ2cHB1glI4uBW1oGwPDkewQ9Uq1"
                  "vENWMDmqlKpLZstEUcPNQ94PUjA2ntEdc")


def parse_args():
    p = argparse.ArgumentParser(description="Chunk-native Phase 1+2+render pipeline")
    p.add_argument("--file-id", required=True, help="Google Drive file ID of the equirect source")
    p.add_argument("--start", type=float, default=0.0, help="Global start offset in the source video")
    p.add_argument("--total-duration", type=float, required=True,
                    help="Total duration to process, seconds")
    p.add_argument("--chunk-duration", type=float, default=20.0)
    p.add_argument("--overlap", type=float, default=3.0,
                    help="Extra seconds downloaded at each chunk's tail for Phase 1 "
                         "detection context; trimmed off before merging")
    p.add_argument("--fps", type=float, default=2.0, help="Phase 1 sample rate")
    p.add_argument("--venue-profile", required=True, type=Path)
    p.add_argument("--output", type=Path, default=Path("playcam/output/full_render.mp4"))
    p.add_argument("--keep-work-dir", action="store_true",
                    help="Don't delete the intermediate chunk-render mp4s at the end")
    return p.parse_args()


def get_access_token():
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", "https://oauth2.googleapis.com/token",
         "-d", f"client_id={CLIENT_ID}", "-d", f"client_secret={CLIENT_SECRET}",
         "-d", f"refresh_token={REFRESH_TOKEN}", "-d", "grant_type=refresh_token"],
        capture_output=True, text=True)
    data = json.loads(result.stdout)
    if "access_token" not in data:
        print(f"ERROR: token refresh failed: {result.stdout}", file=sys.stderr)
        sys.exit(1)
    return data["access_token"]


def download_span(access_token, file_id, start, duration, out_path, label):
    """Stream-copy a span from the Drive source. Fast (no re-encode)."""
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    result = subprocess.run(
        ["ffmpeg", "-y", "-headers", f"Authorization: Bearer {access_token}",
         "-ss", str(start), "-i", url, "-t", str(duration), "-c", "copy", str(out_path)],
        capture_output=True, text=True, timeout=180)
    if result.returncode != 0 or not out_path.exists():
        print(f"ERROR: download failed for {label}: {result.stderr[-1500:]}", file=sys.stderr)
        sys.exit(1)
    print(f"  [{label}] downloaded {out_path.stat().st_size / 1e6:.0f}MB")


def run_phase1_chunk(video_path, venue_profile, fps, out_jsonl, label):
    result = subprocess.run(
        [sys.executable, str(PLAYCAM_DIR / "play_location.py"),
         "--input", str(video_path), "--venue-profile", str(venue_profile),
         "--output", str(out_jsonl), "--fps", str(fps), "--no-debug"],
        capture_output=True, text=True, timeout=280)
    if result.returncode != 0:
        print(f"ERROR: Phase 1 failed for {label}:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}",
              file=sys.stderr)
        sys.exit(1)
    print(f"  [{label}] Phase 1 done")


def build_chunk_plan(start, total_duration, chunk_duration, overlap):
    """List of (global_chunk_start, nominal_duration, download_duration) --
    download_duration includes the trailing overlap except for the last chunk."""
    plan = []
    t = 0.0
    while t < total_duration:
        nominal = min(chunk_duration, total_duration - t)
        is_last = (t + nominal) >= total_duration - 1e-6
        download_dur = nominal if is_last else nominal + overlap
        plan.append((start + t, nominal, download_dur, is_last))
        t += nominal
    return plan


def main():
    args = parse_args()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    access_token = get_access_token()

    plan = build_chunk_plan(args.start, args.total_duration, args.chunk_duration, args.overlap)
    print(f"[chunked_pipeline] {len(plan)} chunks covering {args.total_duration}s "
          f"(chunk={args.chunk_duration}s, overlap={args.overlap}s)")

    # --- Pass 1: download + Phase 1 per chunk, timestamps shifted to global ---
    chunk_jsonls = []
    for i, (global_start, nominal, download_dur, is_last) in enumerate(plan):
        raw_path = WORK_DIR / f"raw_p1_{i}.mp4"
        download_span(access_token, args.file_id, global_start, download_dur,
                       raw_path, f"chunk {i} p1-dl")

        local_jsonl = WORK_DIR / f"local_{i}.jsonl"
        run_phase1_chunk(raw_path, args.venue_profile, args.fps, local_jsonl, f"chunk {i}")

        # Shift local timestamps to global, trim overlap tail (keep only the
        # nominal span except on the last chunk, which has no overlap anyway).
        shifted_path = WORK_DIR / f"shifted_{i}.jsonl"
        kept = 0
        with open(local_jsonl) as fin, open(shifted_path, "w") as fout:
            for line in fin:
                rec = json.loads(line)
                rec["timestamp"] = round(rec["timestamp"] + global_start, 4)
                if rec["timestamp"] < global_start + nominal + 1e-6:
                    fout.write(json.dumps(rec) + "\n")
                    kept += 1
        chunk_jsonls.append(shifted_path)
        print(f"  [chunk {i}] {kept} records kept in global window "
              f"[{global_start:.1f}, {global_start + nominal:.1f})")

        raw_path.unlink(missing_ok=True)
        local_jsonl.unlink(missing_ok=True)

    # --- Merge into one continuous global timeline ---
    merged_path = WORK_DIR / "merged_play_location.jsonl"
    total_records = 0
    with open(merged_path, "w") as fout:
        for cj in chunk_jsonls:
            with open(cj) as fin:
                for line in fin:
                    fout.write(line)
                    total_records += 1
            cj.unlink(missing_ok=True)
    print(f"[chunked_pipeline] Merged {total_records} records -> {merged_path}")

    # --- Phase 2: smooth ONCE over the full merged timeline ---
    global_timeline_path = WORK_DIR / "global_camera_timeline.jsonl"
    result = subprocess.run(
        [sys.executable, str(PLAYCAM_DIR / "smooth_camera_path.py"),
         "--input", str(merged_path), "--output", str(global_timeline_path)],
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"ERROR: Phase 2 smoothing failed:\n{result.stdout}\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(result.stdout.strip())

    # --- Pass 2: re-download each chunk (exact span, no overlap), render clean ---
    render_paths = []
    for i, (global_start, nominal, _download_dur, _is_last) in enumerate(plan):
        raw_path = WORK_DIR / f"raw_render_{i}.mp4"
        download_span(access_token, args.file_id, global_start, nominal,
                       raw_path, f"chunk {i} render-dl")

        render_path = WORK_DIR / f"render_{i}.mp4"
        result = subprocess.run(
            [sys.executable, str(PLAYCAM_DIR / "smooth_camera_path.py"),
             "--input", str(merged_path), "--output", str(global_timeline_path),
             "--render-clean", "--source-video", str(raw_path),
             "--clean-start", str(global_start), "--clean-duration", str(nominal),
             "--clean-output", str(render_path)],
            capture_output=True, text=True, timeout=280)
        if result.returncode != 0:
            print(f"ERROR: clean render failed for chunk {i}:\n"
                  f"{result.stdout[-1500:]}\n{result.stderr[-1500:]}", file=sys.stderr)
            sys.exit(1)
        print(f"  [chunk {i}] rendered")
        render_paths.append(render_path)
        raw_path.unlink(missing_ok=True)

    # --- Concatenate all chunk renders, stream copy, no re-encode ---
    concat_list_path = WORK_DIR / "concat_list.txt"
    with open(concat_list_path, "w") as f:
        for rp in render_paths:
            f.write(f"file '{rp.resolve()}'\n")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list_path),
         "-c", "copy", str(args.output)],
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"ERROR: concat failed: {result.stderr[-1500:]}", file=sys.stderr)
        sys.exit(1)

    print(f"[chunked_pipeline] DONE -> {args.output}")

    if not args.keep_work_dir:
        for rp in render_paths:
            rp.unlink(missing_ok=True)
        concat_list_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
