from __future__ import annotations

import pytest

from ball_tracker.detector_interface import validate_detection, vlm_backend


def valid_detection() -> dict:
    return {
        "frame": 12,
        "yaw": -10.5,
        "pitch": 4.0,
        "conf": 0.87,
        "source": "yolo",
        "crop_yaw": -15.0,
        "detection_geometry": {
            "bbox_xyxy": [10.0, 20.0, 18.0, 28.0],
            "bbox_area_px": 64.0,
            "bbox_aspect_ratio": 1.0,
        },
    }


def test_valid_detection_passes_validation() -> None:
    assert validate_detection(valid_detection()) == valid_detection()


def test_missing_required_field_raises_value_error() -> None:
    detection = valid_detection()
    del detection["pitch"]

    with pytest.raises(ValueError, match="missing required field"):
        validate_detection(detection)


def test_wrong_type_raises_value_error() -> None:
    detection = valid_detection()
    detection["frame"] = "12"

    with pytest.raises(ValueError, match="frame must be an int"):
        validate_detection(detection)


def test_vlm_backend_returns_list_compatible_dry_run_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = vlm_backend(None, frame=12, candidates=[valid_detection()])

    assert isinstance(result, list)
    assert result == []
    assert result.dry_run is True
    assert result.decision == "uncertain"
