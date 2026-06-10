#!/usr/bin/env python3
"""
FFA GoPro MAX .360 → equirectangular converter.

Uses the proper dual-fisheye stitching filter chain from
devclef/gopro-max-video-tools (itself derived from slackspace-io).

This module is called by gopro_uploader.py when a .360 file is detected.
It streams the source directly from GoPro CDN via FFmpeg — the raw .360
file is never written to disk.

Dependencies (installed in the Actions workflow):
  - ffmpeg
  - exiftool  (apt: libimage-exiftool-perl)
"""

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# ─── FFmpeg filter constants ───────────────────────────────────────────────────
# Blend width at the seam between the two fisheye lenses.
# 65 is the value used by devclef/gopro-max-video-tools.
_DIV = 65

# The seam-blending geq expression. Blends a 64px strip at the join edge
# between the left half and the right half of each lens tile.
def _geq(div: int = _DIV) -> str:
    expr = (
        f"if(between(X, 0, 64), "
        f"(p((X+64),Y)*(((X+1))/{div}))+(p(X,Y)*(({div}-((X+1)))/{div})), "
        f"p(X,Y))"
    )
    return (
        f"geq=lum='{expr}':cb='{expr}':cr='{expr}':a='{expr}'"
    )


def _build_filter_chain() -> str:
    """
    Build the full FFmpeg filter_complex string for GoPro MAX .360 → equirectangular.

    The GoPro MAX .360 file has 6 video streams:
      - 0:0 through 0:4 — bottom fisheye tiles
      - 0:5             — top fisheye tile (stream index 5 for normal video, 4 for timelapse)

    We always use stream 0:5 (normal video). The filter:
      1. Crops and blends the seam tiles from both halves
      2. Assembles the bottom and top EAC (Equi-Angular Cubemap) faces
      3. Stacks them vertically into the full EAC frame
      4. Passes through v360=eac:e:interp=cubic to produce equirectangular
      5. Crops the output to 4032x2388 (native MAX equirectangular resolution)
    """
    g = _geq()
    gi = _geq()  # same expression, just named separately for top half for clarity

    bottom = (
        # ── bottom half seam blend left ──
        f"[0:0]crop=128:1344:x=624:y=0,format=yuvj420p,{g}:interpolation=b,"
        f"crop=64:1344:x=0:y=0,format=yuvj420p,scale=96:1344[crop],"
        # ── bottom half left / right tiles ──
        f"[0:0]crop=624:1344:x=0:y=0,format=yuvj420p[left],"
        f"[0:0]crop=624:1344:x=752:y=0,format=yuvj420p[right],"
        f"[left][crop]hstack[leftAll],[leftAll][right]hstack[leftDone],"
        # ── bottom half middle ──
        f"[0:0]crop=1344:1344:1376:0[middle],"
        # ── bottom half seam blend right ──
        f"[0:0]crop=128:1344:x=3344:y=0,format=yuvj420p,{g}:interpolation=b,"
        f"crop=64:1344:x=0:y=0,format=yuvj420p,scale=96:1344[cropRightBottom],"
        f"[0:0]crop=624:1344:x=2720:y=0,format=yuvj420p[leftRightBottom],"
        f"[0:0]crop=624:1344:x=3472:y=0,format=yuvj420p[rightRightBottom],"
        f"[leftRightBottom][cropRightBottom]hstack[rightAll],"
        f"[rightAll][rightRightBottom]hstack[rightBottomDone],"
        f"[leftDone][middle]hstack[leftMiddle],"
        f"[leftMiddle][rightBottomDone]hstack[bottomComplete]"
    )

    top = (
        # ── top half seam blend left ──
        f"[0:5]crop=128:1344:x=624:y=0,format=yuvj420p,{gi}:interpolation=n,"
        f"crop=64:1344:x=0:y=0,format=yuvj420p,scale=96:1344[leftTopCrop],"
        # ── top half left / right tiles ──
        f"[0:5]crop=624:1344:x=0:y=0,format=yuvj420p[firstLeftTop],"
        f"[0:5]crop=624:1344:x=752:y=0,format=yuvj420p[firstRightTop],"
        f"[firstLeftTop][leftTopCrop]hstack[topLeftHalf],"
        f"[topLeftHalf][firstRightTop]hstack[topLeftDone],"
        # ── top half middle ──
        f"[0:5]crop=1344:1344:1376:0[TopMiddle],"
        # ── top half seam blend right ──
        f"[0:5]crop=128:1344:x=3344:y=0,format=yuvj420p,{gi}:interpolation=n,"
        f"crop=64:1344:x=0:y=0,format=yuvj420p,scale=96:1344[TopcropRightBottom],"
        f"[0:5]crop=624:1344:x=2720:y=0,format=yuvj420p[TopleftRightBottom],"
        f"[0:5]crop=624:1344:x=3472:y=0,format=yuvj420p[ToprightRightBottom],"
        f"[TopleftRightBottom][TopcropRightBottom]hstack[ToprightAll],"
        f"[ToprightAll][ToprightRightBottom]hstack[ToprightBottomDone],"
        f"[topLeftDone][TopMiddle]hstack[TopleftMiddle],"
        f"[TopleftMiddle][ToprightBottomDone]hstack[topComplete]"
    )

    assemble = (
        f"[bottomComplete][topComplete]vstack[complete],"
        f"[complete]v360=eac:e:interp=cubic,crop=4032:2388:x=0:y=0[v]"
    )

    return f"{bottom},{top},{assemble}"


def is_360_file(filename: str) -> bool:
    """Return True if the filename has a .360 extension (GoPro MAX)."""
    return Path(filename).suffix.lower() == ".360"


def convert_360_to_mp4(source_url: str, dest_path: Path) -> bool:
    """
    Stream-transcode a GoPro MAX .360 file from GoPro CDN to equirectangular MP4.

    The source is read directly from the CDN URL — no raw .360 file is written
    to disk. The output is written to dest_path.

    Returns True on success, False on failure.
    """
    filter_chain = _build_filter_chain()
    bitrate = os.environ.get("TRANSCODE_360_BITRATE", "20M")

    cmd = [
        "ffmpeg",
        "-i", source_url,
        "-y",                          # overwrite output without prompting
        "-filter_complex", filter_chain,
        "-map", "[v]",
        "-map", "0:a:0?",              # include audio if present
        "-c:v", "h264",
        "-b:v", bitrate,
        "-preset", "ultrafast",        # minimise GitHub runner disk/time
        "-c:a", "aac",
        "-f", "mp4",
        str(dest_path),
    ]

    log.info(f"Starting FFmpeg 360 stitch → {dest_path.name} (bitrate={bitrate})")
    log.info(f"FFmpeg command: {' '.join(cmd[:6])} ... [filter_complex omitted] ...")

    try:
        process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        # Stream FFmpeg stderr live to Actions logs so progress is visible
        for line in process.stderr:
            line = line.rstrip()
            if line:
                # Only log frame/fps/time progress lines at DEBUG to avoid log spam;
                # everything else (errors, stream info) at INFO
                if line.startswith("frame=") or line.startswith("size="):
                    log.debug(f"[ffmpeg] {line}")
                else:
                    log.info(f"[ffmpeg] {line}")

        process.wait()

        if process.returncode != 0:
            log.error(f"FFmpeg exited with code {process.returncode} for {dest_path.name}")
            return False

        size_mb = dest_path.stat().st_size / 1e6 if dest_path.exists() else 0
        log.info(f"FFmpeg stitch complete: {dest_path.name} ({size_mb:.1f} MB)")
        return True

    except FileNotFoundError:
        log.error("ffmpeg not found — ensure 'ffmpeg' is installed in the Actions runner")
        return False
    except Exception as e:
        log.error(f"FFmpeg stitch failed for {dest_path.name}: {e}")
        return False


def inject_360_metadata(video_path: Path) -> bool:
    """
    Inject YouTube 360° XMP metadata into the converted MP4 using exiftool.

    This marks the video as equirectangular so YouTube enables the 360° player.
    Equivalent to the GSpherical tags used by the devclef reference script.

    Returns True on success, False on failure.
    """
    cmd = [
        "exiftool",
        "-api", "LargeFileSupport=1",
        "-overwrite_original",
        "-XMP-GSpherical:Spherical=true",
        "-XMP-GSpherical:Stitched=true",
        "-XMP-GSpherical:StitchingSoftware=FFmpeg",
        "-XMP-GSpherical:ProjectionType=equirectangular",
        str(video_path),
    ]

    log.info(f"Injecting 360° XMP metadata into {video_path.name}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error(f"exiftool failed (code {result.returncode}): {result.stderr.strip()}")
            return False
        log.info(f"exiftool: {result.stdout.strip() or 'metadata injected'}")
        return True

    except FileNotFoundError:
        log.error("exiftool not found — ensure 'libimage-exiftool-perl' is installed in the Actions runner")
        return False
    except Exception as e:
        log.error(f"exiftool metadata injection failed: {e}")
        return False


def process_360_file(source_url: str, dest_path: Path) -> bool:
    """
    Full pipeline: stream-transcode + inject metadata.

    Returns True if both steps succeed, False otherwise.
    dest_path will be cleaned up on failure.
    """
    if not convert_360_to_mp4(source_url, dest_path):
        dest_path.unlink(missing_ok=True)
        return False

    if not inject_360_metadata(dest_path):
        dest_path.unlink(missing_ok=True)
        return False

    return True
