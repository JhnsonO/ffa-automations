from __future__ import annotations
import pytest
from ball_tracker.loss_window_detector import detect_loss_windows

def make_frame(*scores: float, frame_index: int | None = None) -> dict:
    frame = {
        "candidates": [
            {"yaw": 10.0 + index, "pitch": -2.0 - index, "weighted_conf": score}
            for index, score in enumerate(scores)
        ]
    }
    if frame_index is not None:
        frame["frame_index"] = frame_index
    return frame

def test_no_loss_windows_when_every_frame_has_a_trusted_candidate() -> None:
    report = detect_loss_windows([make_frame(0.15), make_frame(0.42), make_frame(0.98)])
    assert report["total_frames"] == 3
    assert report["total_candidates"] == 3
    assert report["loss_windows"] == []
    assert report["summary"] == {"total_windows": 0, "bridgeable": 0, "open": 0, "isolated": 0}

def test_single_bridgeable_loss_window_records_both_boundary_observations() -> None:
    frames = [
        {"frame_index": 20, "candidates": [{"yaw": 42.5, "pitch": -7.0, "weighted_conf": 0.81}]},
        make_frame(0.14, frame_index=21),
        make_frame(0.02, frame_index=22),
        {"frame_index": 23, "candidates": [{"yaw": 51.5, "pitch": -4.0, "weighted_conf": 0.66}]},
    ]
    report = detect_loss_windows(frames)
    assert report["summary"] == {"total_windows": 1, "bridgeable": 1, "open": 0, "isolated": 0}
    assert report["loss_windows"] == [
        {"window_id": "W0001", "start_frame": 21, "end_frame": 22, "duration_frames": 2,
         "last_trusted_yaw": 42.5, "last_trusted_pitch": -7.0, "last_trusted_frame": 20,
         "first_reacquisition_frame": 23, "first_reacquisition_yaw": 51.5,
         "first_reacquisition_pitch": -4.0, "status": "bridgeable"}
    ]

def test_loss_window_at_start_is_isolated() -> None:
    report = detect_loss_windows([
        make_frame(0.14, frame_index=100),
        make_frame(0.0, frame_index=101),
        {"frame_index": 102, "candidates": [{"yaw": -11.0, "pitch": 3.5, "weighted_conf": 0.90}]},
    ])
    window = report["loss_windows"][0]
    assert window["start_frame"] == 100
    assert window["end_frame"] == 101
    assert window["duration_frames"] == 2
    assert window["last_trusted_frame"] is None
    assert window["last_trusted_yaw"] is None
    assert window["last_trusted_pitch"] is None
    assert window["first_reacquisition_frame"] == 102
    assert window["first_reacquisition_yaw"] == pytest.approx(-11.0)
    assert window["first_reacquisition_pitch"] == pytest.approx(3.5)
    assert window["status"] == "isolated"
    assert report["summary"]["isolated"] == 1

def test_loss_window_at_end_is_open() -> None:
    report = detect_loss_windows([
        {"frame_index": 7, "candidates": [{"yaw": 2.0, "pitch": 1.0, "weighted_conf": 0.50}]},
        make_frame(0.149, frame_index=8),
        make_frame(0.01, frame_index=9),
    ])
    window = report["loss_windows"][0]
    assert window["start_frame"] == 8
    assert window["end_frame"] == 9
    assert window["duration_frames"] == 2
    assert window["last_trusted_frame"] == 7
    assert window["last_trusted_yaw"] == pytest.approx(2.0)
    assert window["last_trusted_pitch"] == pytest.approx(1.0)
    assert window["first_reacquisition_frame"] is None
    assert window["first_reacquisition_yaw"] is None
    assert window["first_reacquisition_pitch"] is None
    assert window["status"] == "open"
    assert report["summary"]["open"] == 1

def test_consecutive_untrusted_frames_are_merged_into_one_window() -> None:
    report = detect_loss_windows([
        make_frame(0.70, frame_index=0),
        make_frame(0.14, frame_index=1),
        {"frame_index": 2, "candidates": []},
        make_frame(0.149, frame_index=3),
        make_frame(0.20, frame_index=4),
    ])
    assert len(report["loss_windows"]) == 1
    window = report["loss_windows"][0]
    assert window["window_id"] == "W0001"
    assert window["start_frame"] == 1
    assert window["end_frame"] == 3
    assert window["duration_frames"] == 3
    assert window["status"] == "bridgeable"
