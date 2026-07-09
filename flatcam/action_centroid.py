"""MOG2 action centroid for flatcam venue polygons."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# UNVALIDATED for this lens: starting MOG2 parameters.
VAR_THRESHOLD = 16
HISTORY = 500
# initial guess — re-tune on real footage
DISPERSION_SCALE_PX = 1 / 8
MIN_FOREGROUND_AREA_PX = 50

@dataclass
class ActionStats:
    area: float
    cx: float | None
    cy: float | None
    dispersion: float
    concentration_score: float

class ActionCentroid:
    def __init__(self, frame_shape: tuple[int, int, int] | tuple[int, int], polygon: list[list[int]]):
        h, w = frame_shape[:2]
        self.width = w
        self.mask = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(self.mask, [np.asarray(polygon, dtype=np.int32)], 255)
        self.bg = cv2.createBackgroundSubtractorMOG2(history=HISTORY, varThreshold=VAR_THRESHOLD, detectShadows=False)

    def process(self, frame: np.ndarray) -> ActionStats:
        fg = self.bg.apply(cv2.bitwise_and(frame, frame, mask=self.mask))
        fg = cv2.bitwise_and(fg, fg, mask=self.mask)
        fg = cv2.medianBlur(fg, 5)
        _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)
        num, labels, stats, cents = cv2.connectedComponentsWithStats(fg, 8)
        areas = []
        centers = []
        for i in range(1, num):
            area = float(stats[i, cv2.CC_STAT_AREA])
            if area >= MIN_FOREGROUND_AREA_PX:
                areas.append(area)
                centers.append(cents[i])
        if not areas:
            return ActionStats(0.0, None, None, 0.0, 0.0)
        a = np.asarray(areas, dtype=np.float64)
        c = np.asarray(centers, dtype=np.float64)
        total = float(a.sum())
        centroid = (c * a[:, None]).sum(axis=0) / total
        d2 = ((c[:, 0] - centroid[0]) ** 2 + (c[:, 1] - centroid[1]) ** 2)
        dispersion = float(np.sqrt((d2 * a).sum() / total))
        scale = self.width * DISPERSION_SCALE_PX
        compact = float(np.exp(-dispersion / max(scale * 2.0, 1.0)))
        presence = float(np.clip(total / (self.width * self.width * 0.001), 0.0, 1.0))
        return ActionStats(total, float(centroid[0]), float(centroid[1]), dispersion, presence * compact)
