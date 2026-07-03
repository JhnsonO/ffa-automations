"""Shared equirectangular -> flat perspective crop (gnomonic reprojection)."""
import math
import numpy as np
import cv2


def extract_crop_frame(src_frame, yaw_deg, pitch_deg=0.0, fov_deg=85.0, out_w=1280, out_h=720):
    h, w = src_frame.shape[:2]
    yaw, pitch, fov = math.radians(yaw_deg), math.radians(pitch_deg), math.radians(fov_deg)
    f = (out_w / 2) / math.tan(fov / 2)

    xs, ys = np.meshgrid(np.arange(out_w) - out_w / 2, np.arange(out_h) - out_h / 2)
    zs = np.full_like(xs, f, dtype=np.float64)
    norm = np.sqrt(xs**2 + ys**2 + zs**2)
    x, y, z = xs / norm, ys / norm, zs / norm

    # pitch (around x-axis), positive = up
    y2 = y * math.cos(pitch) + z * math.sin(pitch)
    z2 = -y * math.sin(pitch) + z * math.cos(pitch)
    # yaw (around y-axis)
    x3 = x * math.cos(yaw) + z2 * math.sin(yaw)
    z3 = -x * math.sin(yaw) + z2 * math.cos(yaw)

    lon = np.arctan2(x3, z3)
    lat = np.arcsin(np.clip(y2, -1, 1))
    map_x = ((lon / (2 * math.pi) + 0.5) * w).astype(np.float32)
    map_y = ((0.5 - lat / math.pi) * h).astype(np.float32)

    return cv2.remap(src_frame, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
