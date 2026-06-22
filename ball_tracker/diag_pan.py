#!/usr/bin/env python3
"""
Diagnostic: pan yaw from -90° to +90° at fixed pitch/FOV, output montage.
Tests that the horizon/halfway-line stays level throughout the pan.
Two rows: old (broken) rotation order vs new (world-up) rotation order.
"""
import argparse, math, os
import cv2, numpy as np

THUMB_W = 640
THUMB_H = 360
LABEL_H = 32
FONT = cv2.FONT_HERSHEY_SIMPLEX


# ── OLD (broken): roll → yaw → pitch sequential ─────────────────────────────
def extract_old(eq, yaw_deg, pitch_deg, fov_deg, roll_deg, out_w, out_h):
    h_eq, w_eq = eq.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(0, out_w-1, out_w)
    ys = np.linspace(0, out_h-1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_w/2.0) / f
    ry = -(yv - out_h/2.0) / f
    rz = np.ones_like(rx)
    cr, sr = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))
    rx, ry = cr*rx - sr*ry, sr*rx + cr*ry
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx/norm, ry/norm, rz/norm
    cy = math.radians(yaw_deg)
    wx =  math.cos(cy)*rx + math.sin(cy)*rz
    wy =  ry
    wz = -math.sin(cy)*rx + math.cos(cy)*rz
    cp = math.radians(pitch_deg)
    wy2 = math.cos(cp)*wy - math.sin(cp)*wz
    wz2 = math.sin(cp)*wy + math.cos(cp)*wz
    yaw_map   = np.arctan2(wx, wz2)
    pitch_map = np.arcsin(np.clip(wy2, -1, 1))
    mx = ((yaw_map/(2*math.pi))+0.5)*w_eq
    my = (0.5 - pitch_map/math.pi)*h_eq
    return cv2.remap(eq, mx.astype(np.float32), my.astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


# ── NEW (fixed): world-up look-at camera ────────────────────────────────────
def extract_new(eq, yaw_deg, pitch_deg, fov_deg, roll_deg, out_w, out_h):
    h_eq, w_eq = eq.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))

    # Forward vector from yaw + pitch (standard spherical, Y-up)
    yaw  = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    fwd = np.array([
        math.sin(yaw) * math.cos(pitch),
        math.sin(pitch),
        math.cos(yaw) * math.cos(pitch),
    ])

    world_up = np.array([0.0, 1.0, 0.0])

    # Build orthonormal camera frame
    right = np.cross(fwd, world_up)
    right_norm = np.linalg.norm(right)
    if right_norm < 1e-6:
        # Looking straight up/down — use fallback up
        right = np.array([1.0, 0.0, 0.0])
    else:
        right /= right_norm
    up = np.cross(right, fwd)
    up /= np.linalg.norm(up)

    # Apply roll within camera frame (rotate right/up)
    cr, sr = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))
    right2 = cr * right - sr * up
    up2    = sr * right + cr * up

    # Build rotation matrix: columns = right2, up2, fwd
    R = np.stack([right2, up2, fwd], axis=1)  # (3,3)

    # Output pixel grid → camera-space rays
    xs = np.linspace(0, out_w-1, out_w)
    ys = np.linspace(0, out_h-1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx_c = (xv - out_w/2.0) / f
    ry_c = (yv - out_h/2.0) / f   # note: positive y = down in image = negative up
    rz_c = np.ones_like(rx_c)

    # Rotate to world space: world_ray = R @ [rx_c, -ry_c, rz_c]
    # (flip ry_c sign so +y_image = -world_up)
    rx_c_flat = rx_c.ravel()
    ry_c_flat = (-ry_c).ravel()   # image y flipped
    rz_c_flat = rz_c.ravel()

    cam_rays = np.stack([rx_c_flat, ry_c_flat, rz_c_flat], axis=0)  # (3, N)
    world_rays = R @ cam_rays  # (3, N)

    wx, wy, wz = world_rays[0], world_rays[1], world_rays[2]
    norm = np.sqrt(wx**2 + wy**2 + wz**2)
    wx, wy, wz = wx/norm, wy/norm, wz/norm

    yaw_map   = np.arctan2(wx, wz).reshape(out_h, out_w)
    pitch_map = np.arcsin(np.clip(wy, -1, 1)).reshape(out_h, out_w)

    mx = ((yaw_map/(2*math.pi))+0.5)*w_eq
    my = (0.5 - pitch_map/math.pi)*h_eq
    return cv2.remap(eq, mx.astype(np.float32), my.astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def label(text, w):
    bar = np.zeros((LABEL_H, w, 3), dtype=np.uint8)
    tw, th = cv2.getTextSize(text, FONT, 0.6, 1)[0]
    cv2.putText(bar, text, ((w-tw)//2, (LABEL_H+th)//2), FONT, 0.6, (255,255,255), 1, cv2.LINE_AA)
    return bar


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input",   required=True)
    ap.add_argument("--frame",   type=int,   default=800)
    ap.add_argument("--pitch",   type=float, default=5.0)
    ap.add_argument("--fov",     type=float, default=120.0)
    ap.add_argument("--roll",    type=float, default=4.0)
    ap.add_argument("--yaws",    default="-90,-60,-30,0,30,60,90")
    ap.add_argument("--output",  default="pan_diagnostic.jpg")
    args = ap.parse_args()

    yaws = [float(y) for y in args.yaws.split(",")]

    cap = cv2.VideoCapture(args.input)
    cap.set(cv2.CAP_PROP_POS_FRAMES, args.frame)
    ret, eq = cap.read()
    cap.release()
    if not ret:
        raise RuntimeError(f"Could not read frame {args.frame}")
    print(f"[diag] Frame {args.frame} extracted")

    old_panels, new_panels = [], []
    for yaw in yaws:
        o = extract_old(eq, yaw, args.pitch, args.fov, args.roll, THUMB_W, THUMB_H)
        n = extract_new(eq, yaw, args.pitch, args.fov, args.roll, THUMB_W, THUMB_H)
        old_panels.append(np.vstack([label(f"OLD yaw={yaw:+.0f}°", THUMB_W), o]))
        new_panels.append(np.vstack([label(f"NEW yaw={yaw:+.0f}°", THUMB_W), n]))
        print(f"[diag] Rendered yaw={yaw:+.0f}°")

    row_old = np.hstack(old_panels)
    row_new = np.hstack(new_panels)

    divider = np.full((8, row_old.shape[1], 3), 80, dtype=np.uint8)
    old_hdr = label("── OLD rotation order (banking at non-zero yaw) ──", row_old.shape[1])
    new_hdr = label("── NEW world-up look-at (horizon stable) ──", row_new.shape[1])

    montage = np.vstack([old_hdr, row_old, divider, new_hdr, row_new])
    cv2.imwrite(args.output, montage, [cv2.IMWRITE_JPEG_QUALITY, 90])
    print(f"[diag] Saved {args.output}  shape={montage.shape}  "
          f"size={os.path.getsize(args.output)//1024}KB")


if __name__ == "__main__":
    main()
