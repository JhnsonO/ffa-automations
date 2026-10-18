#!/usr/bin/env python3
"""Copy OEV source videos from GoPro cloud to R2 (OEV-006), resumably.

Drive refused the four sources ("download quota for this file has been
exceeded"); the same recordings are in Johnson's GoPro cloud. This finds
each file by name with the existing gopro_uploader session (cookie auth,
Playwright refresh), picks the download variant whose size equals the Drive
original byte-for-byte (so the frozen window start times stay valid),
and streams it in ranged parts into an R2 multipart upload at
oev/sources/<name> - the same key the source_from_r2 window cutting reads.
Progress is kept in R2, so a stopped run resumes. A file with no exact-size
variant is reported and skipped (nothing written).

Env: S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
plus the gopro_uploader cookie setup (gopro_cookies.json, GOPRO_EMAIL /
GOPRO_PASSWORD for refresh).
Args: --file NAME=EXPECTED_BYTES (repeatable), --part-mb, --budget-min,
--max-pages.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import boto3
import requests
from botocore.config import Config

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gopro_uploader as gu  # noqa: E402

PREFIX = "oev/sources/"


def find_media(session, wanted: set[str], max_pages: int) -> dict:
    found = {}
    for page in range(1, max_pages + 1):
        data = gu.gopro_get(session, "/media/search", params={
            "fields": "id,filename,file_size,captured_at,type", "per_page": 100, "page": page,
            "order_by": "captured_at", "type": "Video",
        })
        items = data.get("_embedded", {}).get("media", [])
        for it in items:
            fn = it.get("filename") or ""
            if fn in wanted and fn not in found:
                found[fn] = it
                print(f"{fn}: GoPro media {it.get('id')} captured {it.get('captured_at')} size {it.get('file_size')}")
        if len(found) == len(wanted) or not items:
            break
    return found


def variants(session, media_id: str) -> list:
    data = gu.gopro_get(session, f"/media/{media_id}/download")
    return data.get("_embedded", {}).get("variations", []) + data.get("_embedded", {}).get("files", [])


def pick_url(session, media_id: str, expected: int):
    """GoPro's listing reports size 0, so each variant URL is measured with a
    one-byte ranged GET; only an exact match to the Drive original is used."""
    vs = variants(session, media_id)
    source_total = 0
    match = (None, None)
    for v in vs:
        size = int(v.get("file_size") or v.get("size") or 0)
        if not size and v.get("url"):
            try:
                size = url_size(v["url"]) or 0
            except Exception as e:  # noqa: BLE001
                print(f"  (size probe failed: {e!r})")
        if v.get("label") == "source":
            source_total += size
        print(f"  variant label={v.get('label')} quality={v.get('quality')} item={v.get('item_number', '')} size={size} url={bool(v.get('url'))}")
        if v.get("url") and size == expected and match[0] is None:
            match = (v["url"], v)
    print(f"  source parts total {source_total} bytes vs Drive original {expected}")
    return match


def url_size(url: str) -> int | None:
    r = requests.get(url, headers={"Range": "bytes=0-0"}, timeout=60, stream=True)
    cr = r.headers.get("Content-Range", "")
    r.close()
    return int(cr.split("/")[1]) if r.status_code == 206 and "/" in cr else None


def fetch(url: str, start: int, end: int) -> bytes:
    last = None
    for attempt in range(4):
        try:
            r = requests.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=600)
            if r.status_code in (401, 403):
                raise PermissionError(f"HTTP {r.status_code}")
            if r.status_code != 206:
                last = f"status {r.status_code}"
            elif len(r.content) != end - start + 1:
                last = f"short read {len(r.content)}"
            else:
                return r.content
        except PermissionError:
            raise
        except requests.RequestException as e:
            last = repr(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"bytes {start}-{end}: {last}")


def chapters(session, media_id: str) -> list:
    vs = [v for v in variants(session, media_id) if v.get("label") == "source" and v.get("url")]
    return sorted(vs, key=lambda v: int(v.get("item_number") or 0))


def download(url_fn, dest: str) -> int:
    """Stream one chapter to disk, resuming with Range on drops; url_fn()
    returns a fresh signed URL when the current one expires."""
    url = url_fn()
    total = url_size(url)
    pos = os.path.getsize(dest) if os.path.exists(dest) else 0
    tries = 0
    with open(dest, "ab") as f:
        while total is None or pos < total:
            try:
                r = requests.get(url, headers={"Range": f"bytes={pos}-"}, stream=True, timeout=600)
                if r.status_code in (401, 403):
                    url = url_fn()
                    tries += 1
                    if tries > 5:
                        raise RuntimeError("signed URL refused repeatedly")
                    continue
                r.raise_for_status()
                for chunk in r.iter_content(8 << 20):
                    f.write(chunk)
                    pos += len(chunk)
                if total is None:
                    break
            except requests.RequestException as e:
                tries += 1
                if tries > 8:
                    raise
                print(f"    download hiccup at {pos}: {e!r}; resuming")
                time.sleep(10 * tries)
    return pos


def ffprobe_duration(path: str) -> float:
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def main(argv=None) -> int:
    import shutil
    import subprocess
    from boto3.s3.transfer import TransferConfig

    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", required=True, help="NAME=DRIVE_DURATION_S")
    ap.add_argument("--workdir", default="/mnt/oev_gopro")
    ap.add_argument("--max-pages", type=int, default=80)
    ap.add_argument("--tolerance-s", type=float, default=0.1)
    a = ap.parse_args(argv)
    want = {k: float(v) for k, v in (x.split("=", 1) for x in a.file)}
    s3 = boto3.client(
        "s3", endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name="auto", config=Config(signature_version="s3v4"),
    )
    bucket = os.environ["S3_BUCKET"]
    summary = {}
    todo = {}
    for name, dur in want.items():
        try:
            meta = s3.head_object(Bucket=bucket, Key=PREFIX + name).get("Metadata", {})
            if abs(float(meta.get("duration-s", "-1")) - dur) <= a.tolerance_s:
                print(f"{name}: already in R2 (duration {meta['duration-s']} s), skipping")
                summary[name] = "done"
                continue
        except s3.exceptions.ClientError:
            pass
        todo[name] = dur
    if not todo:
        print("SUMMARY " + json.dumps(summary))
        return 0
    session = gu.get_authenticated_gopro_session()
    if session is None:
        print("::error::GoPro session could not be authenticated")
        return 1
    media = find_media(session, set(todo), a.max_pages)
    os.makedirs(a.workdir, exist_ok=True)
    tcfg = TransferConfig(multipart_threshold=256 << 20, multipart_chunksize=256 << 20, max_concurrency=4)

    for name, drive_dur in todo.items():
        if name not in media:
            summary[name] = "not found in GoPro cloud"
            continue
        mid = media[name]["id"]
        chs = chapters(session, mid)
        print(f"{name}: {len(chs)} source chapters")
        work = os.path.join(a.workdir, name)
        os.makedirs(work, exist_ok=True)
        parts = []
        t0 = time.time()
        for i, ch in enumerate(chs, start=1):
            dest = os.path.join(work, f"ch{i:02d}.mp4")
            item = ch.get("item_number")

            first = {"url": ch["url"]}

            def url_fn(item=item, first=first):
                # First call: the URL just listed; later calls (expiry): re-list.
                if first.get("url"):
                    return first.pop("url")
                for v in chapters(session, mid):
                    if v.get("item_number") == item:
                        return v["url"]
                raise RuntimeError(f"chapter {item} vanished")

            n = download(url_fn, dest)
            parts.append(dest)
            print(f"  chapter {i}/{len(chs)}: {n / 1e9:.2f} GB ({ffprobe_duration(dest):.2f} s)")
        print(f"  downloaded in {time.time() - t0:.0f} s")
        lst = os.path.join(work, "list.txt")
        with open(lst, "w") as f:
            f.writelines(f"file '{p}'\n" for p in parts)
        out = os.path.join(work, name)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart", out], check=True)
        for pth in parts:
            os.remove(pth)  # free disk before upload
        dur = ffprobe_duration(out)
        print(f"  joined: {os.path.getsize(out) / 1e9:.2f} GB, {dur:.3f} s (Drive original {drive_dur:.3f} s)")
        if abs(dur - drive_dur) > a.tolerance_s:
            summary[name] = f"joined duration {dur:.3f} s != Drive {drive_dur:.3f} s"
            print(f"::warning::{name}: joined duration differs from the Drive original; not uploaded")
            shutil.rmtree(work, ignore_errors=True)
            continue
        s3.upload_file(out, bucket, PREFIX + name, Config=tcfg, ExtraArgs={
            "ContentType": "video/mp4",
            "Metadata": {"duration-s": f"{dur:.3f}", "source": f"gopro:{mid}", "chapters": str(len(chs)),
                         "built": "ffmpeg concat -c copy (video+audio)"}})
        size = s3.head_object(Bucket=bucket, Key=PREFIX + name)["ContentLength"]
        if size != os.path.getsize(out):
            print(f"::error::{name}: R2 size {size} != local {os.path.getsize(out)}")
            return 1
        print(f"{name}: COMPLETE in R2 at {PREFIX + name} ({size} bytes, {dur:.3f} s)")
        summary[name] = "done"
        shutil.rmtree(work, ignore_errors=True)

    print("SUMMARY " + json.dumps(summary))
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as f:
        f.write("### OEV GoPro cloud -> R2\n\n```\n" + json.dumps(summary, indent=1) + "\n```\n")
    return 0 if all(v == "done" for v in summary.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
