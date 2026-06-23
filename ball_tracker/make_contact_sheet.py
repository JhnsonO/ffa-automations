#!/usr/bin/env python3
"""
FFA 360° Ball Tracker — Stage 2 Visual Verification Contact Sheet
Generates perspective crops from equirect MP4 for W1/W2/W3 windows.
Usage: python make_contact_sheet.py <clip.mp4> <output.png>
"""

import sys, os, json, subprocess, tempfile, math
import numpy as np
from PIL import Image, ImageDraw, ImageFont

CROP_W, CROP_H = 320, 320  # perspective crop size
FONT_SIZE = 14

# ── Hardcoded sample observations (frame, yaw, pitch, conf) ─────────────────
# T0001: anchor, net_disp=9.7°, anchor_str=0.981
T0001_SAMPLES = [
    (0,   -55.7,  8.4, 0.85),
    (35,  -65.8, -16.8, 0.86),
    (71,  -65.1, -19.4, 0.80),
    (107, -57.3,  -7.8, 0.77),
]
# T0017: anchor, net_disp=15.4°, anchor_str=0.736
T0017_SAMPLES = [
    (133, -50.6,  4.2, 0.54),
    (140, -43.6,  6.1, 0.73),
    (151, -43.3,  8.2, 0.12),
    (161, -38.2, 13.6, 0.25),
]
# Fence reference at approx yaw=-77, pitch=-4
FENCE_SAMPLE = (100, -77.0, -4.0, None)   # no conf — this is the known hotspot

# T0066: anchor, net_disp=0.04°, anchor_str=0.674  (SUSPECT)
T0066_SAMPLES = [
    (604, -39.4, 16.9, 0.25),
    (607, -39.4, 16.9, 0.49),
    (610, -39.5, 16.9, 0.56),
    (615, -39.5, 16.9, 0.40),
    (620, -39.4, 16.8, 0.20),
    (624, -39.4, 17.0, 0.19),
]

# T0088: anchor, net_disp=36.2°, anchor_str=0.941  (STRONG CANDIDATE)
T0088_SAMPLES = [
    (886,  -40.8, -20.8, 0.31),
    (905,  -53.5,  12.0, 0.60),
    (921,  -57.7,   3.3, 0.67),
    (937,  -56.2,   2.6, 0.68),
    (953,  -57.9,  -2.8, 0.72),
    (969,  -58.3,  -6.3, 0.62),
    (986,  -52.6,  -0.5, 0.75),
    (1007, -37.8,  15.3, 0.74),
]

FPS = 29.97


def frame_to_time(frame_num):
    return frame_num / FPS


def extract_frame(video_path, frame_num, tmpdir):
    """Extract a single frame from the video using ffmpeg."""
    t = frame_to_time(frame_num)
    out_path = os.path.join(tmpdir, f"frame_{frame_num:05d}.jpg")
    if os.path.exists(out_path):
        return out_path
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{t:.4f}",
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        out_path
    ]
    result = subprocess.run(cmd, capture_output=True)
    if not os.path.exists(out_path):
        raise RuntimeError(f"Failed to extract frame {frame_num}: {result.stderr.decode()[:200]}")
    return out_path


def equirect_to_pixel(yaw_deg, pitch_deg, img_w, img_h):
    """Convert spherical yaw/pitch to equirectangular pixel coordinates."""
    # yaw: -180..180, pitch: -90..90
    x = ((yaw_deg + 180.0) / 360.0) * img_w
    y = ((90.0 - pitch_deg) / 180.0) * img_h
    return int(x) % img_w, int(y)


def perspective_crop(img, cx, cy, crop_w=CROP_W, crop_h=CROP_H):
    """Extract crop centred on (cx, cy) with wrap-around on x."""
    W, H = img.size
    half_w = crop_w // 2
    half_h = crop_h // 2

    left = cx - half_w
    right = cx + half_w
    top = max(0, cy - half_h)
    bottom = min(H, cy + half_h)

    if left >= 0 and right <= W:
        crop = img.crop((left, top, right, bottom))
    else:
        # Wrap horizontally
        crop = Image.new("RGB", (crop_w, bottom - top), (0, 0, 0))
        if left < 0:
            # Part from end of image, part from start
            part1 = img.crop((W + left, top, W, bottom))
            part2 = img.crop((0, top, right, bottom))
            crop.paste(part1, (0, 0))
            crop.paste(part2, (part1.width, 0))
        else:
            part1 = img.crop((left, top, W, bottom))
            part2 = img.crop((0, top, right - W, bottom))
            crop.paste(part1, (0, 0))
            crop.paste(part2, (part1.width, 0))

    # Pad top/bottom if clamped
    if crop.size != (crop_w, crop_h):
        padded = Image.new("RGB", (crop_w, crop_h), (0, 0, 0))
        pady = half_h - (cy - top)
        padded.paste(crop, (0, max(0, pady)))
        crop = padded

    return crop


def draw_crosshair(img, color=(255, 0, 0), size=20, thickness=2):
    """Draw crosshair at centre of image."""
    draw = ImageDraw.Draw(img)
    cx, cy = img.width // 2, img.height // 2
    draw.line([(cx - size, cy), (cx + size, cy)], fill=color, width=thickness)
    draw.line([(cx, cy - size), (cx, cy + size)], fill=color, width=thickness)
    return img


def add_label(img, lines, bg_color=(0, 0, 0), text_color=(255, 255, 255)):
    """Add text label block at bottom of image."""
    label_h = 14 * len(lines) + 6
    new_img = Image.new("RGB", (img.width, img.height + label_h), bg_color)
    new_img.paste(img, (0, 0))
    draw = ImageDraw.Draw(new_img)
    for i, line in enumerate(lines):
        draw.text((4, img.height + 3 + i * 14), line, fill=text_color)
    return new_img


def make_crop_panel(video_path, frame_num, yaw, pitch, conf, label_lines,
                    tmpdir, crosshair_color=(255, 0, 0)):
    frame_path = extract_frame(video_path, frame_num, tmpdir)
    img = Image.open(frame_path)
    W, H = img.size
    cx, cy = equirect_to_pixel(yaw, pitch, W, H)
    crop = perspective_crop(img, cx, cy)
    crop = draw_crosshair(crop, color=crosshair_color)
    crop = add_label(crop, label_lines)
    return crop


def make_section_header(text, width, height=30):
    img = Image.new("RGB", (width, height), (40, 40, 40))
    draw = ImageDraw.Draw(img)
    draw.text((10, 8), text, fill=(255, 220, 0))
    return img


def build_row(panels, pad=4, bg=(20, 20, 20)):
    if not panels:
        return Image.new("RGB", (100, 100), bg)
    total_w = sum(p.width for p in panels) + pad * (len(panels) - 1)
    max_h = max(p.height for p in panels)
    row = Image.new("RGB", (total_w, max_h), bg)
    x = 0
    for p in panels:
        row.paste(p, (x, max_h - p.height))
        x += p.width + pad
    return row


def build_contact_sheet(video_path, output_path):
    tmpdir = tempfile.mkdtemp()
    sections = []

    # ── W1: T0001 + fence ref + T0017 ────────────────────────────────────────
    print("Building W1 (T0001 / fence / T0017)...")
    header_w1 = make_section_header(
        "W1 — T0001 (anchor, disp=9.7°) | fence ref @(-77°,-4°) | T0017 (anchor, disp=15.4°)",
        CROP_W * 9 + 4 * 8 + 30
    )

    t0001_panels = []
    for f, yaw, pitch, conf in T0001_SAMPLES:
        t = f"f{f}"
        c = f"conf={conf:.2f}"
        panel = make_crop_panel(video_path, f, yaw, pitch, conf,
                                [t, f"T0001 yaw={yaw:.1f}°", f"pitch={pitch:.1f}°", c, "anchor_str=0.981"],
                                tmpdir, crosshair_color=(0, 200, 0))
        t0001_panels.append(panel)

    # Fence reference
    f_frame, f_yaw, f_pitch, _ = FENCE_SAMPLE
    fence_panel = make_crop_panel(video_path, f_frame, f_yaw, f_pitch, None,
                                  [f"f{f_frame}", f"FENCE REF", f"yaw={f_yaw}°", f"pitch={f_pitch}°", "hotspot"],
                                  tmpdir, crosshair_color=(255, 80, 0))

    t0017_panels = []
    for f, yaw, pitch, conf in T0017_SAMPLES:
        panel = make_crop_panel(video_path, f, yaw, pitch, conf,
                                [f"f{f}", f"T0017 yaw={yaw:.1f}°", f"pitch={pitch:.1f}°", f"conf={conf:.2f}", "anchor_str=0.736"],
                                tmpdir, crosshair_color=(0, 180, 255))
        t0017_panels.append(panel)

    row_w1 = build_row(t0001_panels + [fence_panel] + t0017_panels)
    sections.append(header_w1)
    sections.append(row_w1)

    # ── Spacer ────────────────────────────────────────────────────────────────
    sections.append(Image.new("RGB", (row_w1.width, 10), (0, 0, 0)))

    # ── W2: T0066 (zero-disp suspect) ────────────────────────────────────────
    print("Building W2 (T0066 zero-disp suspect)...")
    header_w2 = make_section_header(
        "W2 — T0066 (anchor, net_disp=0.04° ← SUSPECT) yaw≈-39.4°, pitch≈17°",
        CROP_W * 6 + 4 * 5 + 30
    )
    t0066_panels = []
    for f, yaw, pitch, conf in T0066_SAMPLES:
        panel = make_crop_panel(video_path, f, yaw, pitch, conf,
                                [f"f{f}", f"T0066 yaw={yaw:.1f}°", f"pitch={pitch:.1f}°", f"conf={conf:.2f}", "anchor_str=0.674 SUSPECT"],
                                tmpdir, crosshair_color=(255, 165, 0))
        t0066_panels.append(panel)
    row_w2 = build_row(t0066_panels)
    sections.append(header_w2)
    sections.append(row_w2)

    sections.append(Image.new("RGB", (row_w2.width, 10), (0, 0, 0)))

    # ── W3: T0088 (strong motion candidate) ──────────────────────────────────
    print("Building W3 (T0088 strong motion, disp=36.2°)...")
    header_w3 = make_section_header(
        "W3 — T0088 (anchor, net_disp=36.2°, anchor_str=0.941) — PRIMARY VERIFICATION TARGET",
        CROP_W * 8 + 4 * 7 + 30
    )
    t0088_panels = []
    for f, yaw, pitch, conf in T0088_SAMPLES:
        panel = make_crop_panel(video_path, f, yaw, pitch, conf,
                                [f"f{f}", f"T0088 yaw={yaw:.1f}°", f"pitch={pitch:.1f}°", f"conf={conf:.2f}", "anchor_str=0.941"],
                                tmpdir, crosshair_color=(0, 255, 80))
        t0088_panels.append(panel)

    # Two rows of 4
    row_w3a = build_row(t0088_panels[:4])
    row_w3b = build_row(t0088_panels[4:])
    sections.append(header_w3)
    sections.append(row_w3a)
    sections.append(Image.new("RGB", (row_w3a.width, 4), (0, 0, 0)))
    sections.append(row_w3b)

    # ── Stack all sections ────────────────────────────────────────────────────
    max_w = max(s.width for s in sections)
    total_h = sum(s.height for s in sections) + 8 * len(sections)

    final = Image.new("RGB", (max_w, total_h), (0, 0, 0))
    y = 0
    for s in sections:
        final.paste(s, (0, y))
        y += s.height + 8

    final.save(output_path, "PNG")
    print(f"Saved: {output_path}  ({final.width}x{final.height})")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python make_contact_sheet.py <clip.mp4> <output.png>")
        sys.exit(1)
    build_contact_sheet(sys.argv[1], sys.argv[2])
