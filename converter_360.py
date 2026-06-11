#!/usr/bin/env python3
"""
FFA GoPro MAX .360 → flat 16:9 wide-angle converter.

Approach:
  1. Combine both EAC streams (front + rear) into a full equirectangular frame
  2. Crop the front-facing 180° to a flat 16:9 output
  3. No spatial/360° metadata — plays as a standard wide-angle video everywhere

Why flat crop instead of full 360°/VR180:
  - Camera is mounted on a bungee behind the goal facing the pitch
  - Rear lens shows fence/sky — no useful content
  - Full EAC→equirect stitch of both streams at native res (5376×2688) takes
    >60 mins on GitHub runner, exceeding GoPro CDN signed URL expiry (~60 mins)
  - Flat 16:9 crop is ~4× faster to transcode, ~half the output size
  - Plays on all devices/platforms with no viewer plugin needed
  - Gives a genuinely wide-angle pitch view that standard GoPro footage can't

Stream layout of GoPro MAX .360 concat variant:
  0:0  Video  HEVC  4096×1344  front EAC strip (3 cube faces)
  0:1  Video  HEVC  4096×1344  rear EAC strip  (3 cube faces)
  0:2  Audio  AAC   stereo
  0:3  Audio  PCM   ambisonic
  0:4  Data   timecode
  0:5  Data   GPMD telemetry

FFmpeg pipeline:
  [0:0][0:1]vstack → full 4096×2688 EAC frame
  v360=eac:equirect → 5376×2688 equirectangular
  crop=5376:3024:0:0 → front 16:9 strip (top ~56% of equirect = horizon + pitch)
  scale=3840:2160 → 4K output

Dependencies (installed in the Actions workflow):
  - ffmpeg
"""

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def is_360_file(filename: str) -> bool:
    """Return True if the filename has a .360 extension (GoPro MAX)."""
    return Path(filename).suffix.lower() == ".360"


def convert_360_to_flat(source_url: str, dest_path: Path) -> bool:
    """
    Stream-transcode a GoPro MAX .360 file to a flat 16:9 wide-angle MP4.

    Both EAC streams are stacked into the full EAC canvas, converted to
    equirectangular, then the front-facing pitch view is cropped to 16:9.

    Source is streamed directly from GoPro CDN — no raw file written to disk.
    Output is written to dest_path.

    Returns True on success, False on failure.
    """
    bitrate = os.environ.get("TRANSCODE_360_BITRATE", "20M")

    # EAC full canvas: stack front (0:0) on top of rear (0:1) → 4096×2688
    # v360=eac:equirect → 5376×2688 full equirectangular
    # crop: take top 3024 rows (56% of height) = horizon + pitch, discard sky/ground
    # scale to 3840×2160 (4K 16:9)
    vf = (
        "[0:0][0:1]vstack=inputs=2[eac];"
        "[eac]v360=eac:equirect:interp=cubic:w=5376:h=2688[eq];"
        "[eq]crop=5376:3024:0:0[crop];"
        "[crop]scale=3840:2160[v]"
    )

    cmd = [
        "ffmpeg",
        "-i", source_url,
        "-y",
        "-filter_complex", vf,
        "-map", "[v]",
        "-map", "0:2",           # AAC stereo audio
        "-c:v", "libx264",
        "-b:v", bitrate,
        "-preset", "ultrafast",
        "-c:a", "aac",
        "-f", "mp4",
        str(dest_path),
    ]

    log.info(f"Starting FFmpeg flat crop extraction → {dest_path.name} (bitrate={bitrate})")
    log.info(f"Filter: {vf}")

    try:
        process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        for line in process.stderr:
            line = line.rstrip()
            if line:
                if line.startswith("frame=") or line.startswith("size="):
                    log.debug(f"[ffmpeg] {line}")
                else:
                    log.info(f"[ffmpeg] {line}")

        process.wait()

        if process.returncode != 0:
            log.error(f"FFmpeg exited with code {process.returncode} for {dest_path.name}")
            return False

        size_mb = dest_path.stat().st_size / 1e6 if dest_path.exists() else 0
        log.info(f"FFmpeg flat crop complete: {dest_path.name} ({size_mb:.1f} MB)")
        return True

    except FileNotFoundError:
        log.error("ffmpeg not found — ensure ffmpeg is installed in the Actions runner")
        return False
    except Exception as e:
        log.error(f"FFmpeg extraction failed for {dest_path.name}: {e}")
        return False


def process_360_file(source_url: str, dest_path: Path) -> bool:
    """
    Full pipeline: EAC → equirect → flat 16:9 crop.

    No metadata injection needed — output is a standard MP4.
    Returns True on success, False on failure.
    dest_path is cleaned up on failure.
    """
    if not convert_360_to_flat(source_url, dest_path):
        dest_path.unlink(missing_ok=True)
        return False

    return True
