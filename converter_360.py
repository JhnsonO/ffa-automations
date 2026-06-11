#!/usr/bin/env python3
"""
FFA GoPro MAX .360 → VR180 front-hemisphere converter.

Approach: extract only the front (pitch-facing) fisheye lens (stream 0:0),
convert it to equirectangular front-hemisphere, inject VR180 XMP metadata.

Why not full 360°:
  - Camera is mounted against a fence — rear lens shows fence, not pitch
  - Full equirectangular stitch takes >60 mins on GitHub runner, exceeding
    GoPro CDN signed URL expiry (~60 mins)
  - Single-lens extraction is ~3-4x faster, output ~half the size
  - VR180 gives full left/right/up/down interactivity on YouTube

Stream layout of GoPro MAX .360 concat variant:
  0:0  Video  HEVC  4096x1344  front (pitch-facing) fisheye
  0:1  Video  HEVC  4096x1344  rear (fence-facing) fisheye
  0:2  Audio  AAC   stereo
  0:3  Audio  PCM   ambisonic
  0:4  Data   timecode
  0:5  Data   GPMD telemetry

We use only 0:0 and 0:2.

Dependencies (installed in the Actions workflow):
  - ffmpeg
  - exiftool  (apt: libimage-exiftool-perl)
"""

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def is_360_file(filename: str) -> bool:
    """Return True if the filename has a .360 extension (GoPro MAX)."""
    return Path(filename).suffix.lower() == ".360"


def convert_360_to_vr180(source_url: str, dest_path: Path) -> bool:
    """
    Stream-transcode the front fisheye lens of a GoPro MAX .360 file
    to a front-hemisphere equirectangular MP4.

    Only stream 0:0 (front/pitch-facing lens) is processed.
    Stream 0:1 (rear/fence-facing lens) is ignored entirely.

    The v360 filter converts fisheye → equirectangular with:
      - ih_fov / iv_fov: GoPro MAX fisheye field of view (~180°)
      - interp=cubic: good quality resampling

    Source is streamed directly from GoPro CDN — no raw file written to disk.
    Output is written to dest_path.

    Returns True on success, False on failure.
    """
    bitrate = os.environ.get("TRANSCODE_360_BITRATE", "20M")

    cmd = [
        "ffmpeg",
        "-i", source_url,
        "-y",
        "-filter_complex",
        "[0:0]v360=fisheye:e:ih_fov=177:iv_fov=177:interp=cubic[v]",
        "-map", "[v]",
        "-map", "0:2",           # AAC stereo audio
        "-c:v", "libx264",
        "-b:v", bitrate,
        "-preset", "ultrafast",
        "-c:a", "aac",
        "-f", "mp4",
        str(dest_path),
    ]

    log.info(f"Starting FFmpeg VR180 front-lens extraction → {dest_path.name} (bitrate={bitrate})")

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
        log.info(f"FFmpeg VR180 extraction complete: {dest_path.name} ({size_mb:.1f} MB)")
        return True

    except FileNotFoundError:
        log.error("ffmpeg not found — ensure ffmpeg is installed in the Actions runner")
        return False
    except Exception as e:
        log.error(f"FFmpeg extraction failed for {dest_path.name}: {e}")
        return False


def inject_vr180_metadata(video_path: Path) -> bool:
    """
    Inject YouTube VR180 XMP metadata into the converted MP4 using exiftool.

    VR180 = equirectangular projection, mono (single eye), front hemisphere only.
    YouTube recognises this and enables the interactive 180° viewer.

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
        "-XMP-GSpherical:SourceCount=1",
        "-XMP-GSpherical:InitialViewHeadingDegrees=0",
        "-XMP-GSpherical:InitialViewPitchDegrees=0",
        "-XMP-GSpherical:InitialViewRollDegrees=0",
        "-XMP-GSpherical:CroppedAreaLeftPixels=0",
        "-XMP-GSpherical:CroppedAreaTopPixels=0",
        str(video_path),
    ]

    log.info(f"Injecting VR180 XMP metadata into {video_path.name}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error(f"exiftool failed (code {result.returncode}): {result.stderr.strip()}")
            return False
        log.info(f"exiftool: {result.stdout.strip() or 'metadata injected'}")
        return True

    except FileNotFoundError:
        log.error("exiftool not found — ensure libimage-exiftool-perl is installed")
        return False
    except Exception as e:
        log.error(f"exiftool metadata injection failed: {e}")
        return False


def process_360_file(source_url: str, dest_path: Path) -> bool:
    """
    Full pipeline: front-lens extraction + VR180 metadata injection.

    Returns True if both steps succeed, False otherwise.
    dest_path is cleaned up on failure.
    """
    if not convert_360_to_vr180(source_url, dest_path):
        dest_path.unlink(missing_ok=True)
        return False

    if not inject_vr180_metadata(dest_path):
        dest_path.unlink(missing_ok=True)
        return False

    return True
