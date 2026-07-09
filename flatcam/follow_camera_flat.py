"""16:9 flat follow-camera finite-state machine."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Structure-only starting numbers. NOT calibrated for this score; the playcam values these mirror were tuned for a different, angular-space score. Expect re-tune on first real footage.
FOLLOW_T = 0.45
WIDE_T = 0.30
HYSTERESIS_S = 1.5

class Mode(str, Enum):
    FOLLOW = "FOLLOW"
    WIDE_FALLBACK = "WIDE_FALLBACK"
    REACQUIRE = "REACQUIRE"

@dataclass
class CropState:
    frame_idx: int
    mode: str
    cx: float
    cy: float
    crop_w: float
    crop_h: float
    score: float

class FollowCameraFlat:
    def __init__(self, width: int, height: int, fps: float):
        self.w, self.h, self.fps = width, height, max(float(fps), 1.0)
        self.mode = Mode.WIDE_FALLBACK
        self.follow_elapsed = 0.0
        self.wide_elapsed = 0.0
        self.cx, self.cy = width / 2.0, height / 2.0
        self.vx = self.vy = 0.0
        self.crop_w = float(width)
        self.crop_h = float(min(height, width * 9 / 16))

    def _wide_size(self) -> tuple[float, float]:
        if self.w / self.h >= 16 / 9:
            return self.h * 16 / 9, float(self.h)
        return float(self.w), self.w * 9 / 16

    def _clamp(self, cx: float, cy: float, cw: float, ch: float) -> tuple[float, float]:
        return min(max(cx, cw / 2), self.w - cw / 2), min(max(cy, ch / 2), self.h - ch / 2)

    def update(self, frame_idx: int, cx: float | None, cy: float | None, score: float) -> CropState:
        dt = 1.0 / self.fps
        self.follow_elapsed = self.follow_elapsed + dt if score >= FOLLOW_T else 0.0
        self.wide_elapsed = self.wide_elapsed + dt if score <= WIDE_T else 0.0
        if self.mode == Mode.WIDE_FALLBACK and self.follow_elapsed >= HYSTERESIS_S:
            self.mode = Mode.FOLLOW
        elif self.mode == Mode.FOLLOW and self.wide_elapsed >= HYSTERESIS_S:
            self.mode = Mode.WIDE_FALLBACK

        if self.mode == Mode.FOLLOW and cx is not None and cy is not None:
            target_cx, target_cy = cx, cy
            target_w = self.w * 0.55
            target_h = target_w * 9 / 16
            if target_h > self.h:
                target_h = self.h * 0.95
                target_w = target_h * 16 / 9
        else:
            target_cx, target_cy = self.w / 2.0, self.h / 2.0
            target_w, target_h = self._wide_size()

        alpha = min(1.0, dt * 2.0)
        self.crop_w += (target_w - self.crop_w) * alpha
        self.crop_h += (target_h - self.crop_h) * alpha
        desired_vx = (target_cx - self.cx) * min(1.0, dt * 3.0) / dt
        desired_vy = (target_cy - self.cy) * min(1.0, dt * 3.0) / dt
        max_accel = self.w * 2.0
        for axis in ("x", "y"):
            v = self.vx if axis == "x" else self.vy
            dv = (desired_vx if axis == "x" else desired_vy) - v
            dv = max(-max_accel * dt, min(max_accel * dt, dv))
            if axis == "x": self.vx = max(-self.w, min(self.w, v + dv))
            else: self.vy = max(-self.w, min(self.w, v + dv))
        self.cx += self.vx * dt
        self.cy += self.vy * dt
        self.cx, self.cy = self._clamp(self.cx, self.cy, self.crop_w, self.crop_h)
        return CropState(frame_idx, self.mode.value, self.cx, self.cy, self.crop_w, self.crop_h, float(score))
