#!/usr/bin/env python3
"""Detector backend contract for the additive Phase B recovery pipeline."""

from __future__ import annotations

import base64
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence, TypedDict

ALLOWED_SOURCES = frozenset({"yolo", "vlm", "yolo_finetuned"})
DEFAULT_ANTHROPIC_MODEL = os.environ.get(
    "ANTHROPIC_MODEL",
    "claude-sonnet-4-5",
)


class DetectionGeometry(TypedDict):
    bbox_xyxy: list[float]
    bbox_area_px: float
    bbox_aspect_ratio: float


class DetectionSchema(TypedDict):
    frame: int
    yaw: float
    pitch: float
    conf: float
    source: Literal["yolo", "vlm", "yolo_finetuned"]
    crop_yaw: float
    detection_geometry: DetectionGeometry


class VLMResult(list[DetectionSchema]):
    """List-compatible VLM result enriched with review metadata."""

    decision: Literal["ball", "not_ball", "uncertain"]
    confidence: float
    reasoning: str
    dry_run: bool

    def __init__(
        self,
        detections: Iterable[DetectionSchema] = (),
        *,
        decision: Literal["ball", "not_ball", "uncertain"] = "uncertain",
        confidence: float = 0.0,
        reasoning: str = "",
        dry_run: bool = False,
    ) -> None:
        super().__init__(detections)
        self.decision = decision
        self.confidence = confidence
        self.reasoning = reasoning
        self.dry_run = dry_run


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number, not bool.")

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number.") from exc

    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite.")

    return number


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object.")
    return value


def validate_detection(detection: Mapping[str, Any]) -> DetectionSchema:
    """Validate and normalise one shared detector-interface observation."""
    value = _require_mapping(detection, "detection")
    required = {
        "frame",
        "yaw",
        "pitch",
        "conf",
        "source",
        "crop_yaw",
        "detection_geometry",
    }
    missing = sorted(required.difference(value.keys()))

    if missing:
        raise ValueError(
            f"Detection missing required field(s): {', '.join(missing)}"
        )

    frame = value["frame"]
    if isinstance(frame, bool) or not isinstance(frame, int):
        raise ValueError("frame must be an int.")

    source = value["source"]
    if source not in ALLOWED_SOURCES:
        raise ValueError(
            "source must be one of: yolo, vlm, yolo_finetuned."
        )

    geometry = _require_mapping(
        value["detection_geometry"],
        "detection_geometry",
    )
    geometry_required = {
        "bbox_xyxy",
        "bbox_area_px",
        "bbox_aspect_ratio",
    }
    missing_geometry = sorted(
        geometry_required.difference(geometry.keys())
    )

    if missing_geometry:
        raise ValueError(
            "detection_geometry missing required field(s): "
            + ", ".join(missing_geometry)
        )

    bbox = geometry["bbox_xyxy"]
    if (
        not isinstance(bbox, Sequence)
        or isinstance(bbox, (str, bytes))
        or len(bbox) != 4
    ):
        raise ValueError(
            "detection_geometry.bbox_xyxy must be a four-number list."
        )

    bbox_values = [
        _finite_number(component, "detection_geometry.bbox_xyxy")
        for component in bbox
    ]
    x1, y1, x2, y2 = bbox_values

    if x2 < x1 or y2 < y1:
        raise ValueError(
            "detection_geometry.bbox_xyxy must satisfy x2>=x1 and y2>=y1."
        )

    area = _finite_number(
        geometry["bbox_area_px"],
        "detection_geometry.bbox_area_px",
    )
    aspect = _finite_number(
        geometry["bbox_aspect_ratio"],
        "detection_geometry.bbox_aspect_ratio",
    )

    if area < 0.0:
        raise ValueError("detection_geometry.bbox_area_px must be >= 0.")
    if aspect <= 0.0:
        raise ValueError("detection_geometry.bbox_aspect_ratio must be > 0.")

    confidence = _finite_number(value["conf"], "conf")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("conf must be in [0, 1].")

    return {
        "frame": frame,
        "yaw": _finite_number(value["yaw"], "yaw"),
        "pitch": _finite_number(value["pitch"], "pitch"),
        "conf": confidence,
        "source": source,
        "crop_yaw": _finite_number(value["crop_yaw"], "crop_yaw"),
        "detection_geometry": {
            "bbox_xyxy": bbox_values,
            "bbox_area_px": area,
            "bbox_aspect_ratio": aspect,
        },
    }


def _frame_number_from_path(frame_path: str | Path) -> int:
    stem = Path(frame_path).stem
    match = re.search(r"(\d+)(?!.*\d)", stem)
    return int(match.group(1)) if match else 0


def _class_ids_for_ball(model: Any) -> list[int]:
    explicit = os.environ.get("YOLO_BALL_CLASS_IDS")
    if explicit:
        return [
            int(value.strip())
            for value in explicit.split(",")
            if value.strip()
        ]

    names = getattr(model, "names", {})
    if isinstance(names, Mapping):
        iterable: Iterable[tuple[Any, Any]] = names.items()
    elif isinstance(names, Sequence):
        iterable = enumerate(names)
    else:
        iterable = ()

    return [
        int(class_id)
        for class_id, class_name in iterable
        if "ball" in str(class_name).lower()
    ]


def yolo_backend(
    frame_path: str | Path,
    model_path: str | Path,
) -> list[DetectionSchema]:
    """Run a YOLO model against a 2D frame and emit shared-schema detections.

    The backend is intentionally additive. It does not alter the frozen Stage 1
    detector, its crop geometry, or its output schema.
    """
    try:
        import cv2
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError(
            "yolo_backend requires opencv-python and ultralytics at runtime."
        ) from exc

    frame = cv2.imread(str(frame_path))
    if frame is None:
        raise FileNotFoundError(f"Could not read image frame: {frame_path}")

    height, width = frame.shape[:2]
    if width <= 0 or height <= 0:
        raise ValueError("Frame must have non-zero width and height.")

    model = YOLO(str(model_path))
    class_ids = _class_ids_for_ball(model)
    if not class_ids:
        return []

    results = model.predict(
        frame,
        classes=class_ids,
        conf=float(os.environ.get("YOLO_BACKEND_CONF", "0.01")),
        verbose=False,
    )

    frame_number = _frame_number_from_path(frame_path)
    detections: list[DetectionSchema] = []

    for result in results:
        if result.boxes is None:
            continue
        for box in result.boxes:
            x1, y1, x2, y2 = [
                float(value)
                for value in box.xyxy[0].tolist()
            ]
            confidence = float(box.conf[0])
            centre_x = (x1 + x2) / 2.0
            centre_y = (y1 + y2) / 2.0
            yaw = (centre_x / width) * 360.0 - 180.0
            pitch = 90.0 - (centre_y / height) * 180.0
            box_width = max(0.0, x2 - x1)
            box_height = max(0.0, y2 - y1)

            if box_height == 0.0:
                continue

            detection: DetectionSchema = {
                "frame": frame_number,
                "yaw": yaw,
                "pitch": pitch,
                "conf": confidence,
                "source": "yolo",
                "crop_yaw": yaw,
                "detection_geometry": {
                    "bbox_xyxy": [x1, y1, x2, y2],
                    "bbox_area_px": box_width * box_height,
                    "bbox_aspect_ratio": box_width / box_height,
                },
            }
            detections.append(validate_detection(detection))

    return detections


def _extract_json_object(text: str) -> Mapping[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(
            r"^```(?:json)?\s*",
            "",
            stripped,
            flags=re.IGNORECASE,
        )
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError("VLM response did not contain valid JSON.") from exc

    if not isinstance(value, Mapping):
        raise ValueError("VLM response JSON must be an object.")
    return value


def _image_media_type(image_path: Path) -> str:
    suffix = image_path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    raise ValueError(f"Unsupported VLM image type: {image_path.suffix}")


def _vlm_prompt(
    frame: int,
    candidates: Sequence[Mapping[str, Any]],
) -> str:
    compact_candidates: list[dict[str, float]] = []
    for candidate in candidates:
        try:
            compact_candidates.append(
                {
                    "yaw": float(candidate["yaw"]),
                    "pitch": float(candidate["pitch"]),
                    "conf": float(
                        candidate.get(
                            "weighted_conf",
                            candidate.get(
                                "conf",
                                candidate.get("raw_conf", 0.0),
                            ),
                        )
                    ),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue

    return (
        "You are reviewing one football-tracking recovery crop. Decide whether a "
        "football is visible at one of the candidate locations. Return JSON only, "
        "with no markdown: "
        '{"decision":"ball|not_ball|uncertain","confidence":0.0,'
        '"reasoning":"short evidence statement","detection":null or '
        '{"yaw":number,"pitch":number,"conf":number,"crop_yaw":number,'
        '"bbox_xyxy":[x1,y1,x2,y2],"bbox_area_px":number,'
        '"bbox_aspect_ratio":number}}. '
        f"Frame={frame}. Candidate context={json.dumps(compact_candidates)}. "
        "Use decision=ball only when the ball itself is visually credible."
    )


def vlm_backend(
    image_path: str | Path | None = None,
    *,
    frame: int = 0,
    candidates: Sequence[Mapping[str, Any]] | None = None,
    model: str | None = None,
) -> VLMResult:
    """Review one pre-generated pack image, or return dry-run without a key."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return VLMResult(
            decision="uncertain",
            confidence=0.0,
            reasoning="dry-run: ANTHROPIC_API_KEY is not set",
            dry_run=True,
        )

    if image_path is None:
        raise ValueError("image_path is required when ANTHROPIC_API_KEY is set.")

    image = Path(image_path)
    if not image.is_file():
        raise FileNotFoundError(f"VLM review image not found: {image}")

    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise RuntimeError(
            "vlm_backend requires the optional 'anthropic' package when an API key is set."
        ) from exc

    encoded_image = base64.b64encode(image.read_bytes()).decode("ascii")
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model or DEFAULT_ANTHROPIC_MODEL,
        max_tokens=700,
        temperature=0,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _image_media_type(image),
                            "data": encoded_image,
                        },
                    },
                    {
                        "type": "text",
                        "text": _vlm_prompt(frame, candidates or []),
                    },
                ],
            }
        ],
    )

    text_parts = [
        block.text
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
        and hasattr(block, "text")
    ]
    payload = _extract_json_object("\n".join(text_parts))
    decision = payload.get("decision")
    if decision not in {"ball", "not_ball", "uncertain"}:
        raise ValueError("VLM response decision must be ball, not_ball, or uncertain.")

    confidence = _finite_number(payload.get("confidence"), "VLM confidence")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("VLM confidence must be in [0, 1].")

    reasoning = str(payload.get("reasoning", "")).strip()
    detections: list[DetectionSchema] = []
    raw_detection = payload.get("detection")

    if decision == "ball" and isinstance(raw_detection, Mapping):
        raw_geometry = raw_detection.get(
            "detection_geometry",
            raw_detection,
        )
        detection: dict[str, Any] = {
            "frame": frame,
            "yaw": raw_detection.get("yaw"),
            "pitch": raw_detection.get("pitch"),
            "conf": raw_detection.get("conf", confidence),
            "source": "vlm",
            "crop_yaw": raw_detection.get(
                "crop_yaw",
                raw_detection.get("yaw"),
            ),
            "detection_geometry": {
                "bbox_xyxy": raw_geometry.get("bbox_xyxy"),
                "bbox_area_px": raw_geometry.get("bbox_area_px"),
                "bbox_aspect_ratio": raw_geometry.get(
                    "bbox_aspect_ratio"
                ),
            },
        }
        detections.append(validate_detection(detection))

    return VLMResult(
        detections,
        decision=decision,
        confidence=confidence,
        reasoning=reasoning,
        dry_run=False,
    )
