from __future__ import annotations

from ball_tracker.bidirectional_resolver import resolve_loss_windows


GEOMETRY = {
    "bbox_xyxy": [10.0, 10.0, 18.0, 18.0],
    "bbox_area_px": 64.0,
    "bbox_aspect_ratio": 1.0,
}


def candidate(
    yaw: float,
    pitch: float = 0.0,
    conf: float = 0.30,
) -> dict:
    return {
        "yaw": yaw,
        "pitch": pitch,
        "raw_conf": conf,
        "weighted_conf": conf,
        "source": "yolo",
        "crop_yaw": yaw,
        "region": "test",
        "detection_geometry": dict(GEOMETRY),
    }


def bridgeable_window(start: int, end: int) -> dict:
    return {
        "window_id": "W0001",
        "start_frame": start,
        "end_frame": end,
        "duration_frames": end - start + 1,
        "last_trusted_frame": start - 1,
        "last_trusted_yaw": 0.0,
        "last_trusted_pitch": 0.0,
        "first_reacquisition_frame": end + 1,
        "first_reacquisition_yaw": float(end + 1),
        "first_reacquisition_pitch": 0.0,
        "status": "bridgeable",
    }


def test_short_gap_is_resolved_without_vlm_when_traces_agree() -> None:
    window = bridgeable_window(1, 3)
    frames = {
        -1: [candidate(-1.0)],
        0: [candidate(0.0)],
        1: [candidate(1.0, conf=0.10)],
        2: [candidate(2.0, conf=0.10)],
        3: [candidate(3.0, conf=0.10)],
        4: [candidate(4.0)],
        5: [candidate(5.0)],
    }

    repairs, queue = resolve_loss_windows([window], frames)

    assert [repair["frame"] for repair in repairs["repairs"]] == [1, 2, 3]
    assert all(
        repair["candidate"]["source"] == "bidirectional"
        for repair in repairs["repairs"]
    )
    assert queue["queue"] == []


def test_long_gap_is_queued_without_attempting_a_repair() -> None:
    window = bridgeable_window(1, 30)
    frames = {
        frame: [candidate(float(frame), conf=0.10)]
        for frame in range(1, 31)
    }

    repairs, queue = resolve_loss_windows([window], frames)

    assert repairs["repairs"] == []
    assert len(queue["queue"]) == 1
    assert queue["queue"][0]["reason"] == "long_window"


def test_anchor_quality_gate_rejects_fence_zone_anchor() -> None:
    window = bridgeable_window(1, 1)
    window.update(
        {
            "last_trusted_frame": 0,
            "last_trusted_yaw": -77.4,
            "last_trusted_pitch": -3.9,
            "first_reacquisition_frame": 2,
            "first_reacquisition_yaw": 2.0,
        }
    )
    frames = {
        -1: [candidate(-77.4, -3.9)],
        0: [candidate(-77.4, -3.9)],
        1: [candidate(1.0, conf=0.10)],
        2: [candidate(2.0)],
        3: [candidate(3.0)],
    }

    repairs, queue = resolve_loss_windows([window], frames)

    assert repairs["repairs"] == []
    assert len(queue["queue"]) == 1
    assert "left_anchor_failed_quality_gate" in queue["queue"][0]["reason"]


def test_physics_corridor_rejects_implausible_jump() -> None:
    window = bridgeable_window(1, 1)
    window.update(
        {
            "last_trusted_frame": 0,
            "last_trusted_yaw": 0.0,
            "first_reacquisition_frame": 2,
            "first_reacquisition_yaw": 0.0,
        }
    )
    frames = {
        -1: [candidate(0.0)],
        0: [candidate(0.0)],
        1: [candidate(20.0, conf=0.12)],
        2: [candidate(0.0)],
        3: [candidate(0.0)],
    }

    repairs, queue = resolve_loss_windows([window], frames)

    assert repairs["repairs"] == []
    assert len(queue["queue"]) == 1
    assert queue["queue"][0]["reason"] == "no_corridor_supported_candidates"
