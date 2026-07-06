#!/usr/bin/env python3
"""
FFA Camera Labeling Tool  (Issue #5)
====================================
Browser tool a labeler uses to mark where a virtual camera *should* have
pointed while watching a 360-degree equirectangular football preview clip.
Clicking on the video frame derives a yaw angle from the click position and
saves a label record immediately, server-side.

Label schema (persisted, one JSON object per line in labels/<clip_id>.jsonl):

    {"id": "<8-char hex>", "timestamp": <float sec>,
     "desired_yaw": <float deg, wrapped to [-180, 180)>,
     "confidence": <float 0-1>, "event": <str>, "notes": <str>}

`id` is an addressing key this tool adds for edit/delete; it is not part of
the conceptual label schema consumed downstream. There is NO `desired_fov`
field anywhere in this tool -- this project deliberately dropped zoom from
the label schema. Do not add it back "for completeness".

Click -> yaw convention (the one and only convention used anywhere in this
repo -- see docs/ai-project-state.md, Issue #5 section):

    desired_yaw = ((click_x / preview_width) - 0.5) * 360

wrapped defensively into [-180, 180). The browser does the pixel-position
math (accounting for CSS letterboxing) in static/index.html; this file
re-validates/re-wraps server-side so a bad client can never persist an
out-of-range value.

Clip source, in priority order:
  1. Local folder ./clips/*.mp4 -- the test fallback. Always available, no
     credentials required. Build/verify against this first.
  2. Google Drive folder, using this repo's existing service-account
     pattern (see sheet_manager.py / cleanup_and_sort.py --
     GOOGLE_SERVICE_ACCOUNT_JSON env var). Folder id from
     FFA_LABELING_DRIVE_FOLDER_ID env var or .ffa_labeling_drive_folder_id
     file, matching the existing .ffa_drive_folder_id convention. Files are
     downloaded into drive_cache/ and served the same way as local clips.
     Entirely skipped (no error) if GOOGLE_SERVICE_ACCOUNT_JSON isn't set.

This tool does not import, execute, or otherwise touch ball_tracker/ or
playcam/. Keep it that way -- it is intentionally standalone.
"""
import json
import os
import re
import threading
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
CLIPS_DIR = BASE_DIR / "clips"
DRIVE_CACHE_DIR = BASE_DIR / "drive_cache"
LABELS_DIR = BASE_DIR / "labels"
STATIC_DIR = BASE_DIR / "static"

for d in (CLIPS_DIR, DRIVE_CACHE_DIR, LABELS_DIR):
    d.mkdir(exist_ok=True)

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
CLIP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

app = Flask(__name__, static_folder=None)

# One lock per clip id so concurrent rapid-click saves for the same clip
# serialize their read-modify-write / append instead of racing each other.
_label_locks = {}
_locks_guard = threading.Lock()


def _lock_for(clip_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _label_locks.get(clip_id)
        if lock is None:
            lock = threading.Lock()
            _label_locks[clip_id] = lock
        return lock


def _safe_clip_id(clip_id: str) -> str:
    if not clip_id or not CLIP_ID_RE.match(clip_id):
        abort(400, "invalid clip id")
    return clip_id


def _labels_path(clip_id: str) -> Path:
    return LABELS_DIR / f"{_safe_clip_id(clip_id)}.jsonl"


def _read_labels(clip_id: str):
    path = _labels_path(clip_id)
    if not path.exists():
        return []
    records = []
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _write_labels_atomic(clip_id: str, records) -> None:
    """Rewrite the whole JSONL file (used by edit/delete). Atomic via
    write-to-temp + rename so a reader never sees a half-written file."""
    path = _labels_path(clip_id)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    tmp.replace(path)


def _append_label(clip_id: str, record) -> None:
    path = _labels_path(clip_id)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _wrap_yaw(deg: float) -> float:
    """Wrap into [-180, 180). Same convention as playcam/play_location.py."""
    return ((deg + 180.0) % 360.0) - 180.0


def _validate_label(body):
    if not isinstance(body, dict):
        abort(400, "expected a JSON object")
    try:
        timestamp = float(body["timestamp"])
        desired_yaw = float(body["desired_yaw"])
    except (KeyError, TypeError, ValueError):
        abort(400, "timestamp and desired_yaw are required numeric fields")
    try:
        confidence = float(body.get("confidence", 0.8))
    except (TypeError, ValueError):
        abort(400, "confidence must be numeric")
    if timestamp < 0:
        abort(400, "timestamp must be >= 0")
    if not (0.0 <= confidence <= 1.0):
        abort(400, "confidence must be in [0, 1]")
    event = str(body.get("event", "normal")).strip() or "normal"
    notes = str(body.get("notes", ""))
    return {
        "timestamp": round(timestamp, 3),
        "desired_yaw": round(_wrap_yaw(desired_yaw), 2),
        "confidence": confidence,
        "event": event,
        "notes": notes,
    }


# --- clip discovery ---------------------------------------------------------

def _dir_clips(directory: Path, source: str):
    if not directory.exists():
        return []
    return [
        {"id": p.name, "source": source}
        for p in sorted(directory.iterdir())
        if p.suffix.lower() in VIDEO_EXTS
    ]


def list_clips():
    clips = {c["id"]: c for c in _dir_clips(CLIPS_DIR, "local")}
    for c in _dir_clips(DRIVE_CACHE_DIR, "drive_cache"):
        clips.setdefault(c["id"], c)
    return sorted(clips.values(), key=lambda c: c["id"])


def _clip_path(clip_id: str) -> Path:
    _safe_clip_id(clip_id)
    local = CLIPS_DIR / clip_id
    if local.exists():
        return local
    cached = DRIVE_CACHE_DIR / clip_id
    if cached.exists():
        return cached
    abort(404, "clip not found")


# --- Google Drive (optional, secondary path) --------------------------------

def _drive_enabled() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))


def _drive_folder_id():
    env = os.environ.get("FFA_LABELING_DRIVE_FOLDER_ID")
    if env:
        return env.strip()
    f = BASE_DIR / ".ffa_labeling_drive_folder_id"
    if f.exists():
        val = f.read_text().strip()
        return val or None
    return None


def sync_drive_clips():
    """Best-effort refresh of drive_cache/ from the configured Drive folder.
    No-op (not an error) if Drive isn't configured. Must never raise -- a
    Drive problem must never take down the local-clip path."""
    if not _drive_enabled():
        return
    folder_id = _drive_folder_id()
    if not folder_id:
        app.logger.warning(
            "Drive: GOOGLE_SERVICE_ACCOUNT_JSON is set but no folder id "
            "configured (FFA_LABELING_DRIVE_FOLDER_ID or "
            ".ffa_labeling_drive_folder_id) -- skipping Drive sync"
        )
        return
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload

        creds = service_account.Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="files(id, name, mimeType)",
            pageSize=200,
        ).execute()
        for f in resp.get("files", []):
            name = f.get("name", "")
            if Path(name).suffix.lower() not in VIDEO_EXTS:
                continue
            dest = DRIVE_CACHE_DIR / name
            if dest.exists():
                continue
            tmp = dest.with_suffix(dest.suffix + ".part")
            req = drive.files().get_media(fileId=f["id"])
            with open(tmp, "wb") as out:
                downloader = MediaIoBaseDownload(out, req)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            tmp.replace(dest)
            app.logger.info(f"Drive: cached {name}")
    except Exception as e:  # noqa: BLE001 -- deliberately broad, must stay non-fatal
        app.logger.warning(f"Drive sync failed (non-fatal, local clips unaffected): {e}")


# --- routes ------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/clips")
def api_clips():
    sync_drive_clips()
    return jsonify(list_clips())


@app.route("/clips/<path:clip_id>")
def serve_clip(clip_id):
    path = _clip_path(clip_id)
    # conditional=True gives Range-request / 206 partial-content support,
    # needed for browser scrubbing.
    return send_file(str(path), conditional=True)


@app.route("/api/labels/<clip_id>", methods=["GET"])
def get_labels(clip_id):
    _safe_clip_id(clip_id)
    return jsonify(_read_labels(clip_id))


@app.route("/api/labels/<clip_id>", methods=["POST"])
def post_label(clip_id):
    _safe_clip_id(clip_id)
    record = _validate_label(request.get_json(force=True, silent=False))
    record = {"id": uuid.uuid4().hex[:8], **record}
    with _lock_for(clip_id):
        _append_label(clip_id, record)
    return jsonify(record), 201


@app.route("/api/labels/<clip_id>/<label_id>", methods=["PUT"])
def put_label(clip_id, label_id):
    _safe_clip_id(clip_id)
    updated = _validate_label(request.get_json(force=True, silent=False))
    updated = {"id": label_id, **updated}
    with _lock_for(clip_id):
        records = _read_labels(clip_id)
        for i, r in enumerate(records):
            if r.get("id") == label_id:
                records[i] = updated
                _write_labels_atomic(clip_id, records)
                return jsonify(updated)
    abort(404, "label not found")


@app.route("/api/labels/<clip_id>/<label_id>", methods=["DELETE"])
def delete_label(clip_id, label_id):
    _safe_clip_id(clip_id)
    with _lock_for(clip_id):
        records = _read_labels(clip_id)
        remaining = [r for r in records if r.get("id") != label_id]
        if len(remaining) == len(records):
            abort(404, "label not found")
        _write_labels_atomic(clip_id, remaining)
    return jsonify({"deleted": label_id})


if __name__ == "__main__":
    port = int(os.environ.get("LABELING_TOOL_PORT", "8090"))
    sync_drive_clips()
    app.run(host="0.0.0.0", port=port, threaded=True)
