import json

from ball_tracker.pitch_geometry import PitchGeometry


def make_config(tmp_path, zones):
    path = tmp_path / "geometry.json"
    path.write_text(json.dumps({"setup": "test", "suppression_zones": zones}))
    return PitchGeometry(str(path))


def zone_one():
    return {
        "id": "one",
        "description": "test",
        "yaw_centre": -77.4,
        "yaw_radius": 6.0,
        "pitch_centre": -3.9,
        "pitch_radius": 5.0,
    }


def require(value):
    if not value:
        raise RuntimeError("geometry expectation failed")


def test_centre(tmp_path):
    require(make_config(tmp_path, [zone_one()]).is_suppressed(-77.4, -3.9))


def test_outside(tmp_path):
    require(not make_config(tmp_path, [zone_one()]).is_suppressed(-60.0, 10.0))


def test_yaw_edges(tmp_path):
    geometry = make_config(tmp_path, [zone_one()])
    require(geometry.is_suppressed(-71.4, -3.9))
    require(not geometry.is_suppressed(-71.3, -3.9))


def test_pitch_edges(tmp_path):
    geometry = make_config(tmp_path, [zone_one()])
    require(geometry.is_suppressed(-77.4, 1.1))
    require(not geometry.is_suppressed(-77.4, 1.2))


def test_second_zone(tmp_path):
    zone_two = {
        "id": "two",
        "description": "test",
        "yaw_centre": 20.0,
        "yaw_radius": 2.0,
        "pitch_centre": 10.0,
        "pitch_radius": 3.0,
    }
    geometry = make_config(tmp_path, [zone_one(), zone_two])
    require(geometry.is_suppressed(20.0, 10.0))
