"""Tests for camera-neutral YouTube title/description and FFA_MEDIA_ID preservation.

No network, no uploads: every external call in upload_item() is stubbed.
"""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gopro_uploader as u  # noqa: E402

TUESDAY = "2026-10-13T18:00:00Z"
MONDAY = "2026-10-12T18:00:00Z"
TITLE_TUE = "Football Session in Leicester | Tuesday 13 Oct 2026 | FFA Leicester"


def test_title_is_camera_neutral():
    titles = {u.make_title("GX010001.MP4", TUESDAY, lbl) for lbl in ("", "A", "B", "Manual", "POV")}
    assert titles == {TITLE_TUE}


def test_title_uses_london_date_not_utc():
    # 23:30Z on 12 Oct is 00:30 BST on 13 Oct
    assert u.make_title("x", "2026-10-12T23:30:00Z") == TITLE_TUE
    # winter (GMT): no offset
    assert u.make_title("x", "2026-12-01T20:00:00Z") == "Football Session in Leicester | Tuesday 1 Dec 2026 | FFA Leicester"


def test_title_fallback_on_bad_date():
    assert u.make_title("x", "garbage") == "Football Session in Leicester | garbage | FFA Leicester"


def test_description_content_and_order():
    d = u.make_description("x", TUESDAY, "A")
    expected_markers = [
        "Watch FFA's community football session in Leicester on Tuesday 13 Oct 2026.",
        "Want to play with FFA? See upcoming sessions and book your place:\nhttps://www.officialffa.co.uk/book",
        "FFA runs organised community football sessions and tournaments in Leicester. Players of different levels are welcome.",
        "Shop FFA merch: https://www.officialffa.co.uk/store",
        "Instagram: https://www.instagram.com/_official_ffa\nTikTok: https://www.tiktok.com/@official_ffa",
        "#FFA #FFALeicester #FootballForAll #Leicester #GrassrootsFootball #5aside #MondayLeague",
    ]
    positions = [d.index(m) for m in expected_markers]
    assert positions == sorted(positions)
    assert "Camera" not in d
    assert "Monday League" not in d  # Tuesday: no league section
    for removed in ("Competitive kickabouts", "Interested in tournaments", "Book your spot"):
        assert removed not in d


def test_description_monday_section_between_ffa_line_and_shop():
    d = u.make_description("x", MONDAY)
    assert "Monday, 12" not in d and "Monday 12 Oct 2026" in d
    league = "This is part of the FFA Monday League — our weekly competitive kickabout with individual player standings.\nCheck the league table: https://www.officialffa.co.uk/mnl"
    assert league in d
    assert d.index("Players of different levels are welcome.") < d.index(league) < d.index("Shop FFA merch")


def test_description_monday_uses_london_date():
    # 23:30Z Sunday 11 Oct is 00:30 BST Monday 12 Oct
    d = u.make_description("x", "2026-10-11T23:30:00Z")
    assert "Monday 12 Oct 2026" in d and "Monday League" in d


def test_description_fallback_on_bad_date():
    d = u.make_description("x", "garbage")
    assert "session in Leicester on garbage." in d and "Monday League" not in d.replace("#MondayLeague", "")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "DB_PATH", tmp_path / "uploaded.db")
    monkeypatch.setattr(u, "DOWNLOAD_DIR", tmp_path)
    captured = {}
    uploads = []

    def fake_download(url, dest, max_retries=3):
        Path(dest).write_bytes(b"x")
        return True

    def fake_upload(service, video_path, title, description, gopro_filename="", max_retries=3):
        uploads.append((title, description))
        return "FAKEYTID"

    monkeypatch.setattr(u, "get_concat_download_url", lambda *a, **k: "http://example.invalid/v")
    monkeypatch.setattr(u, "download_video", fake_download)
    monkeypatch.setattr(u, "upload_to_youtube", fake_upload)
    monkeypatch.setattr(u, "get_drive_service", lambda: None)
    monkeypatch.setattr(u, "upload_source_to_drive", lambda *a, **k: None)
    con = u.init_db()
    captured["uploads"] = uploads
    return con, uploads


ITEM = {
    "id": "MEDIAID123abc",
    "filename": "GX010165.MP4",
    "captured_at": "2026-10-13T18:00:00Z",
    "created_at": "2026-10-13T20:15:00Z",
    "file_size": 0,
}


def test_dry_run_standard_and_pov_same_title_and_ffa_block_unchanged(harness):
    con, uploads = harness
    u.upload_item(con, None, None, dict(ITEM), camera_label="A")
    pov = dict(ITEM, id="MEDIAID456def", filename="GX010166.MP4")
    u.upload_item(con, None, None, pov, camera_label="Manual")
    assert len(uploads) == 2  # captured only; nothing real uploaded
    (t1, d1), (t2, d2) = uploads
    assert t1 == t2 == TITLE_TUE
    assert "Tuesday 13 Oct 2026" in d1 and "Tuesday 13 Oct 2026" in d2

    # FFA block: byte-for-byte what the unchanged f-string in upload_item() produces
    expected_block = (
        "\n\nFFA_MEDIA_ID:MEDIAID123abc\nFFA_FILENAME:GX010165.MP4\n"
        "FFA_CAPTURED_AT:2026-10-13T18:00:00Z\nFFA_CREATED_AT:2026-10-13T20:15:00Z\n"
        "FFA_EFFECTIVE_AT:2026-10-13T18:00:00Z"
    )
    assert d1.endswith(expected_block)
    assert d1[: -len(expected_block)] == u.make_description("GX010165.MP4", "2026-10-13T18:00:00Z")

    # media ID is still recoverable from the description, and duplicate detection sees it
    assert re.search(r"^FFA_MEDIA_ID:(\S+)$", d1, re.M).group(1) == "MEDIAID123abc"
    assert re.search(r"^FFA_MEDIA_ID:(\S+)$", d2, re.M).group(1) == "MEDIAID456def"
    assert u.already_uploaded(con, "MEDIAID123abc")
    assert u.already_uploaded(con, "MEDIAID456def")
    assert not u.already_uploaded(con, "someotherid", "other.mp4")


def test_direct_upload_block_untouched():
    src = Path(u.__file__).read_text(encoding="utf-8")
    assert 'description += f"\\n\\nFFA_MEDIA_ID:{media_id}\\nFFA_FILENAME:{filename}\\nFFA_CAPTURED_AT:{original_captured_at}\\nFFA_CREATED_AT:{created_at}\\nFFA_EFFECTIVE_AT:{upload_date}"' in src
    assert 'description += f"\\n\\nFFA_MEDIA_ID:{media_id}\\nFFA_FILENAME:{filename}\\nFFA_DIRECT_UPLOAD_URL:{DIRECT_UPLOAD_URL}"' in src
