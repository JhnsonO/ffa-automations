"""Standalone suppression-zone geometry for FFA 360 ball tracking."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class PitchGeometry:
    """Load configured suppression zones and test candidate yaw/pitch positions."""

    def __init__(self, config_path: str):
        with Path(config_path).open("r", encoding="utf-8") as config_file:
            config: dict[str, Any] = json.load(config_file)
        self.suppression_zones: list[dict[str, Any]] = config.get(
            "suppression_zones", []
        )

    def is_suppressed(self, yaw: float, pitch: float) -> bool:
        """Return True when a candidate falls inside any configured zone."""
        for zone in self.suppression_zones:
            yaw_match = abs(yaw - zone["yaw_centre"]) <= zone["yaw_radius"]
            pitch_match = abs(pitch - zone["pitch_centre"]) <= zone["pitch_radius"]
            if yaw_match and pitch_match:
                return True
        return False
