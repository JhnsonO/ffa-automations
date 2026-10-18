#!/usr/bin/env python3
"""Copy large OEV source videos from Google Drive to R2, resumably.

Drive rations downloads of large files (OEV-006 hit "Quota exceeded" after
the first bulk attempts), so this copies one file at a time, single
connection, in fixed-size ranged parts streamed straight into an S3
multipart upload on R2. Progress (upload id + finished parts) is stored in
R2 next to the target, so a run that is stopped by a 403/429 or the time
budget simply resumes on the next run. It never retries a 403 in the same
run.

Target keys: oev/sources/<filename>. A finished object is verified by size
against Drive's reported size and skipped on later runs.

Env: DRIVE_ACCESS_TOKEN, S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY_ID,
S3_SECRET_ACCESS_KEY. Args: --file NAME=DRIVE_ID (repeatable),
--part-mb 256, --budget-min 330.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import boto3
from botocore.config import Config

PREFIX = "oev/sources/"


class Blocked(Exception):
    pass


def drive_size(fid: str, token: str) -> int:
    req = urllib.request.Request(
        f"https://www.googleapis.com/drive/v3/files/{fid}?fields=size&supportsAllDrives=true",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(json.load(r)["size"])


def drive_range(fid: str, token: str, start: int, end: int) -> bytes:
    req = urllib.request.Request(
        f"https://www.googleapis.com/drive/v3/files/{fid}?alt=media&supportsAllDrives=true",
        headers={"Authorization": f"Bearer {token}", "Range": f"bytes={start}-{end}"},
    )
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                if r.status != 206:
                    raise Blocked(f"status {r.status} (expected 206)")
                data = r.read()
            if len(data) != end - start + 1:
                last = f"short read {len(data)} of {end - start + 1}"
                continue
            return data
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                body = e.read()[:300].decode(errors="replace").replace("\n", " ")
                raise Blocked(f"HTTP {e.code}: {body}") from e
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = repr(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"bytes {start}-{end}: {last}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", required=True, help="NAME=DRIVE_ID")
    ap.add_argument("--part-mb", type=int, default=256)
    ap.add_argument("--budget-min", type=float, default=330)
    a = ap.parse_args(argv)
    token = os.environ["DRIVE_ACCESS_TOKEN"]
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
    blocked = None

    for spec in a.file:
        name, fid = spec.split("=", 1)
        key = PREFIX + name
        state_key = key + ".copy-state.json"
        size = drive_size(fid, token)
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            if head["ContentLength"] == size:
                print(f"{name}: already in R2 ({size} bytes), skipping")
                summary[name] = "done"
                continue
        except s3.exceptions.ClientError:
            pass
        try:
            st = json.loads(s3.get_object(Bucket=bucket, Key=state_key)["Body"].read())
            if st.get("size") != size or st.get("part_bytes") != part:
                raise ValueError("stale state")
            print(f"{name}: resuming upload, {len(st['parts'])} parts done")
        except Exception:
            up = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType="video/mp4")
            st = {"upload_id": up["UploadId"], "size": size, "part_bytes": part, "drive_id": fid, "parts": []}
            s3.put_object(Bucket=bucket, Key=state_key, Body=json.dumps(st).encode())
            print(f"{name}: new multipart upload, {size} bytes in {-(-size // part)} parts")
        n_parts = -(-size // part)
        done = {p["PartNumber"] for p in st["parts"]}
        t0 = time.time()
        for pn in range(1, n_parts + 1):
            if pn in done:
                continue
            if time.time() > deadline:
                print(f"{name}: time budget reached at part {pn}/{n_parts}; resume next run")
                summary[name] = f"paused {len(st['parts'])}/{n_parts}"
                break
            start = (pn - 1) * part
            end = min(size, pn * part) - 1
            try:
                data = drive_range(fid, token, start, end)
            except Blocked as e:
                blocked = f"{name} part {pn}: {e}"
                print(f"::warning::Drive refused {blocked}; stopping (no retry). Resume after the quota resets.")
                summary[name] = f"blocked {len(st['parts'])}/{n_parts}"
                break
            r = s3.upload_part(Bucket=bucket, Key=key, UploadId=st["upload_id"], PartNumber=pn, Body=data)
            st["parts"].append({"PartNumber": pn, "ETag": r["ETag"]})
            s3.put_object(Bucket=bucket, Key=state_key, Body=json.dumps(st).encode())
            mb = (end - start + 1) / 1e6
            print(f"{name}: part {pn}/{n_parts} ({mb:.0f} MB) ok, {mb / max(time.time() - t0, 1e-6):.1f} MB/s avg since start")
            t0 = time.time()
        else:
            parts = sorted(st["parts"], key=lambda p: p["PartNumber"])
            s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=st["upload_id"], MultipartUpload={"Parts": parts})
            got = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
            if got != size:
                print(f"::error::{name}: R2 size {got} != Drive size {size}")
                return 1
            s3.delete_object(Bucket=bucket, Key=state_key)
            print(f"{name}: COMPLETE in R2 at {key} ({got} bytes, size verified)")
            summary[name] = "done"
        if blocked or time.time() > deadline:
            break

    print("SUMMARY " + json.dumps(summary))
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as f:
        f.write("### OEV Drive -> R2\n\n```\n" + json.dumps(summary, indent=1) + "\n```\n")
        if blocked:
            f.write(f"\nStopped: Drive refused ({blocked}). Re-run after the quota resets; progress is kept.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
