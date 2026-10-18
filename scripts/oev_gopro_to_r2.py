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
    vs = variants(session, media_id)
    for v in vs:
        size = int(v.get("file_size") or v.get("size") or 0)
        print(f"  variant label={v.get('label')} quality={v.get('quality')} size={size} available={v.get('available')} url={bool(v.get('url'))}")
    for v in vs:
        size = int(v.get("file_size") or v.get("size") or 0)
        if v.get("url") and v.get("available", True) and size == expected:
            return v["url"], v
    return None, None


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", required=True, help="NAME=EXPECTED_BYTES")
    ap.add_argument("--part-mb", type=int, default=256)
    ap.add_argument("--budget-min", type=float, default=320)
    ap.add_argument("--max-pages", type=int, default=80)
    a = ap.parse_args(argv)
    want = dict(s.split("=", 1) for s in a.file)
    want = {k: int(v) for k, v in want.items()}
    s3 = boto3.client(
        "s3", endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name="auto", config=Config(signature_version="s3v4"),
    )
    bucket = os.environ["S3_BUCKET"]
    part = a.part_mb * 1024 * 1024
    deadline = time.time() + a.budget_min * 60
    summary = {}

    todo = {}
    for name, size in want.items():
        try:
            if s3.head_object(Bucket=bucket, Key=PREFIX + name)["ContentLength"] == size:
                print(f"{name}: already in R2, skipping")
                summary[name] = "done"
                continue
        except s3.exceptions.ClientError:
            pass
        todo[name] = size
    if not todo:
        print("SUMMARY " + json.dumps(summary))
        return 0

    session = gu.get_authenticated_gopro_session()
    if session is None:
        print("::error::GoPro session could not be authenticated (cookies invalid and refresh failed)")
        return 1
    media = find_media(session, set(todo), a.max_pages)

    for name, size in todo.items():
        if name not in media:
            summary[name] = "not found in GoPro cloud"
            print(f"::warning::{name}: not found in the first {a.max_pages} pages of GoPro cloud media")
            continue
        mid = media[name]["id"]
        url, v = pick_url(session, mid, size)
        if not url:
            summary[name] = "no variant matching the Drive original's size"
            print(f"::warning::{name}: no GoPro variant is exactly {size} bytes; skipped so window times stay valid")
            continue
        got = url_size(url)
        if got != size:
            summary[name] = f"variant URL size {got} != {size}"
            print(f"::warning::{name}: download URL reports {got} bytes, expected {size}; skipped")
            continue
        key = PREFIX + name
        state_key = key + ".copy-state.json"
        try:
            st = json.loads(s3.get_object(Bucket=bucket, Key=state_key)["Body"].read())
            if st.get("size") != size or st.get("part_bytes") != part:
                raise ValueError("stale")
            print(f"{name}: resuming, {len(st['parts'])} parts done")
        except Exception:
            up = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType="video/mp4")
            st = {"upload_id": up["UploadId"], "size": size, "part_bytes": part, "source": f"gopro:{mid}:{v.get('label')}", "parts": []}
            s3.put_object(Bucket=bucket, Key=state_key, Body=json.dumps(st).encode())
            print(f"{name}: new upload from GoPro variant {v.get('label')}, {-(-size // part)} parts")
        n = -(-size // part)
        done = {p["PartNumber"] for p in st["parts"]}
        t0 = time.time()
        for pn in range(1, n + 1):
            if pn in done:
                continue
            if time.time() > deadline:
                summary[name] = f"paused {len(st['parts'])}/{n}"
                break
            start, end = (pn - 1) * part, min(size, pn * part) - 1
            try:
                data = fetch(url, start, end)
            except PermissionError:
                url, _ = pick_url(session, mid, size)  # signed URL expired: get a fresh one
                if not url:
                    summary[name] = f"url refresh failed at {len(st['parts'])}/{n}"
                    break
                data = fetch(url, start, end)
            r = s3.upload_part(Bucket=bucket, Key=key, UploadId=st["upload_id"], PartNumber=pn, Body=data)
            st["parts"].append({"PartNumber": pn, "ETag": r["ETag"]})
            s3.put_object(Bucket=bucket, Key=state_key, Body=json.dumps(st).encode())
            mb = (end - start + 1) / 1e6
            print(f"{name}: part {pn}/{n} ok ({mb / max(time.time() - t0, 1e-6):.0f} MB/s)")
            t0 = time.time()
        else:
            s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=st["upload_id"],
                                         MultipartUpload={"Parts": sorted(st["parts"], key=lambda p: p["PartNumber"])})
            got = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
            if got != size:
                print(f"::error::{name}: R2 size {got} != {size}")
                return 1
            s3.delete_object(Bucket=bucket, Key=state_key)
            print(f"{name}: COMPLETE in R2 at {key} ({got} bytes, matches Drive original)")
            summary[name] = "done"
        if time.time() > deadline:
            break

    print("SUMMARY " + json.dumps(summary))
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as f:
        f.write("### OEV GoPro cloud -> R2\n\n```\n" + json.dumps(summary, indent=1) + "\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
