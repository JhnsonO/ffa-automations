#!/usr/bin/env python3
"""Run the controlled OEV YOLO inference-resolution arm on RunPod without a network volume.

The experiment variable is the YOLO26m ONNX export input size (1920 vs 2560).
Everything else reuses the accepted-v4 control wrapper pinned by
runpod_yolo_resolution_local_remote.sh.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

REPO = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO / "followcam_artifacts"
CONFIG = REPO / "oev" / "yolo_resolution_run.json"
BASE = "https://rest.runpod.io/v1"
EXPECTED_RECO_SHA = "c8b0d74b537d192c7de8d2856de64620a82830cf"
ULTRALYTICS_VERSION = "8.4.118"
OEV_DRIVE_ROOT = "18Y8hI_S29BMeg5FEoxlaqoGy1DsQ7GKJ"
SAMPLE_SET = "GX010197-seed1384188843"
SAMPLE_ID = "sample_02"
DURATION_S = 180

ALL_DCS = [
    "EU-RO-1", "CA-MTL-1", "EU-SE-1", "US-IL-1", "EUR-IS-1", "EU-CZ-1",
    "US-TX-3", "EUR-IS-2", "US-KS-2", "US-GA-2", "US-WA-1", "US-TX-1",
    "CA-MTL-3", "EU-NL-1", "US-TX-4", "US-CA-2", "US-NC-1", "OC-AU-1",
    "US-DE-1", "EUR-IS-3", "CA-MTL-2", "AP-JP-1", "EUR-NO-1", "EU-FR-1",
    "US-KS-3", "US-GA-1", "AP-IN-1", "US-MD-1",
]
GPU_TYPES = [
    "NVIDIA GeForce RTX 4090",
    "NVIDIA RTX A5000",
    "NVIDIA GeForce RTX 3090",
    "NVIDIA L4",
    "NVIDIA L40S",
    "NVIDIA GeForce RTX 5090",
    "NVIDIA RTX 2000 Ada Generation",
    "NVIDIA RTX PRO 6000 Blackwell Server Edition",
]
OVERLAY = (
    '0.08, "ball_weight": 0.70, "dead_zone_rad": 0.06, "velocity_alpha": 0.08, '
    '"max_velocity_rad_per_sec": 0.31, "fov_tight": 38.0, "fov_default": 44.0, '
    '"fov_wide": 58.0, "ball_containment_enabled": true, '
    '"ball_containment_enter_fraction": 0.80, "ball_containment_exit_fraction": 0.45'
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd, *, check=True, capture=False, timeout=None, cwd=None):
    print("+", " ".join(shlex.quote(str(x)) for x in cmd), flush=True)
    return subprocess.run(
        [str(x) for x in cmd],
        check=check,
        text=True,
        capture_output=capture,
        timeout=timeout,
        cwd=cwd,
    )


def api_request(api_key: str, method: str, path: str, data=None, timeout=30):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode(errors="replace")[:800]
        raise RuntimeError(f"HTTP {exc.code} {method} {path}: {err}") from exc


def make_keypair():
    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    )
    Path("/tmp/runpod_key").write_bytes(priv)
    Path("/tmp/runpod_key.pub").write_bytes(pub + b" ffa-oev-yolo-resolution\n")
    os.chmod("/tmp/runpod_key", 0o600)
    return Path("/tmp/runpod_key.pub").read_text().strip()


def ssh_base(ip, port):
    return [
        "ssh", "-i", "/tmp/runpod_key", "-p", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=10",
        "-o", "TCPKeepAlive=yes", f"root@{ip}",
    ]


def scp_base(port):
    return [
        "scp", "-i", "/tmp/runpod_key", "-P", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=10",
        "-o", "TCPKeepAlive=yes",
    ]


def ssh(ip, port, command, *, check=True, capture=False, timeout=None):
    return run(ssh_base(ip, port) + [command], check=check, capture=capture, timeout=timeout)


def scp_to(ip, port, local: Path, remote: str, *, check=True, timeout=120):
    return run(scp_base(port) + [str(local), f"root@{ip}:{remote}"], check=check, timeout=timeout)


def scp_from(ip, port, remote: str, local: Path, *, check=False, timeout=300):
    return run(scp_base(port) + [f"root@{ip}:{remote}", str(local)], check=check, timeout=timeout)


def wait_network(api_key, pod_id, timeout_s=360):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        info = api_request(api_key, "GET", f"/pods/{pod_id}?includeMachine=true")
        mappings = info.get("portMappings") or {}
        port = None
        if isinstance(mappings, dict):
            port = mappings.get("22") or mappings.get("22/tcp")
        if info.get("publicIp") and port:
            return info, info["publicIp"], port
        time.sleep(5)
    raise RuntimeError(f"pod {pod_id} network timeout")


def wait_ssh(ip, port, attempts=24):
    for i in range(attempts):
        p = run(
            ["ssh", "-i", "/tmp/runpod_key", "-p", str(port),
             "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
             f"root@{ip}", "echo OK"],
            check=False, capture=True, timeout=15,
        )
        if "OK" in p.stdout:
            return True
        print(f"SSH not ready ({i + 1}/{attempts})", flush=True)
        time.sleep(5)
    return False


def run_preflight(ip, port, attempt):
    local = REPO / "runpod_gpu_preflight.sh"
    scp_to(ip, port, local, "/tmp/runpod_gpu_preflight.sh")
    ssh(ip, port, "chmod +x /tmp/runpod_gpu_preflight.sh", timeout=30)
    p = ssh(
        ip, port, "stdbuf -oL -eL /tmp/runpod_gpu_preflight.sh",
        check=False, capture=True, timeout=240,
    )
    output = (p.stdout or "") + (p.stderr or "")
    (ARTIFACTS / f"preflight_attempt_{attempt}.txt").write_text(output)
    ok = p.returncode == 0 and "PREFLIGHT_RESULT=PASS" in output.splitlines()
    driver = next(
        (line.split("=", 1)[1] for line in output.splitlines()
         if line.startswith("PREFLIGHT_DRIVER_VERSION=")),
        "unknown",
    )
    if ok:
        (ARTIFACTS / "preflight_output.txt").write_text(output)
    return ok, driver


def delete_pod(api_key, pod_id):
    for attempt in range(3):
        try:
            api_request(api_key, "DELETE", f"/pods/{pod_id}", timeout=20)
            print(f"Deleted pod {pod_id}", flush=True)
            return
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                print(f"Pod {pod_id} already absent", flush=True)
                return
            print(f"Delete attempt {attempt + 1}/3 failed for {pod_id}: {exc}", flush=True)
            if attempt < 2:
                time.sleep(3)


def allocate_healthy(api_key, pub_key, target_dc, target_gpu, pod_ids):
    allowed = [target_dc] if target_dc else list(ALL_DCS)
    gpu_types = [target_gpu] if target_gpu else list(GPU_TYPES)
    for attempt in range(1, 11):
        if not allowed:
            raise RuntimeError("all candidate datacenters exhausted by NVDEC failures")
        if attempt > 1:
            time.sleep(8)
        body = {
            "name": "oev-yolo-resolution",
            "imageName": "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404",
            "gpuTypeIds": gpu_types,
            "gpuTypePriority": "custom",
            "gpuCount": 1,
            "cloudType": "SECURE",
            "containerDiskInGb": 120,
            "ports": ["22/tcp"],
            "supportPublicIp": True,
            "allowedCudaVersions": ["12.8"],
            "dataCenterIds": allowed,
            "env": {"PUBLIC_KEY": pub_key, "SSH_PUBLIC_KEY": pub_key},
        }
        try:
            pod = api_request(api_key, "POST", "/pods", body)
        except Exception as exc:
            print(f"Allocation attempt {attempt}: {exc}", flush=True)
            continue
        pod_id = pod.get("id")
        if not pod_id:
            print(f"Allocation attempt {attempt}: response had no id", flush=True)
            continue
        pod_ids.append(pod_id)
        print(f"Created pod {pod_id}", flush=True)
        try:
            info, ip, port = wait_network(api_key, pod_id)
        except Exception as exc:
            print(f"Allocation attempt {attempt}: {exc}", flush=True)
            delete_pod(api_key, pod_id)
            continue
        machine = info.get("machine") or {}
        dc = machine.get("dataCenterId") or "unknown"
        gpu = (
            (info.get("gpu") or {}).get("displayName")
            or (machine.get("gpuType") or {}).get("displayName")
            or machine.get("gpuDisplayName")
            or machine.get("gpuTypeId")
            or "unknown"
        )
        if not wait_ssh(ip, port):
            print(f"Allocation attempt {attempt}: SSH timeout on {dc}/{gpu}", flush=True)
            delete_pod(api_key, pod_id)
            continue
        try:
            ok, driver = run_preflight(ip, port, attempt)
        except Exception as exc:
            print(f"Allocation attempt {attempt}: preflight error: {exc}", flush=True)
            delete_pod(api_key, pod_id)
            continue
        print(
            f"attempt={attempt} pod={pod_id} dc={dc} gpu={gpu} "
            f"machine={info.get('machineId')} driver={driver} preflight={ok}",
            flush=True,
        )
        if ok:
            return {
                "pod_id": pod_id,
                "ip": ip,
                "port": port,
                "gpu_type": gpu,
                "datacenter": dc,
                "machine_id": info.get("machineId") or "unknown",
                "driver_version": driver,
                "cost_per_hr": info.get("costPerHr", "unavailable"),
            }
        delete_pod(api_key, pod_id)
        if not target_dc and dc in allowed and dc != "unknown":
            allowed.remove(dc)
            print(f"Steering subsequent attempts away from failed datacenter {dc}", flush=True)
    raise RuntimeError("no healthy RunPod GPU after 10 allocation attempts")


def google_creds():
    token = json.loads(os.environ["YOUTUBE_TOKEN"])
    cfg = json.loads(os.environ["YOUTUBE_CREDENTIALS"])
    creds = Credentials(
        token=None,
        refresh_token=token["refresh_token"],
        token_uri=token.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token.get("client_id") or cfg["installed"]["client_id"],
        client_secret=token.get("client_secret") or cfg["installed"]["client_secret"],
    )
    creds.refresh(Request())
    return creds


def drive_child(drive, name, parent, folder=False):
    mime = " and mimeType='application/vnd.google-apps.folder'" if folder else ""
    q = f"name='{name}' and '{parent}' in parents and trashed=false{mime}"
    files = drive.files().list(
        q=q, fields="files(id,name,size)", supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    if not files:
        raise RuntimeError(f"Drive item {name!r} missing under {parent!r}")
    return files[0]


def resolve_sample_files(creds):
    drive = build("drive", "v3", credentials=creds)
    samples = drive_child(drive, "Samples", OEV_DRIVE_ROOT, True)
    pack = drive_child(drive, SAMPLE_SET, samples["id"], True)
    sample = drive_child(drive, SAMPLE_ID, pack["id"], True)
    left = drive_child(drive, "sample_02_left_180s.mp4", sample["id"])
    right = drive_child(drive, "sample_02_right_180s.mp4", sample["id"])
    return drive, left, right


def download_samples_to_pod(meta, creds, left, right):
    ip, port = meta["ip"], meta["port"]
    ssh(ip, port, "apt-get update -qq && apt-get install -y -qq --no-install-recommends aria2 >/dev/null && mkdir -p /tmp/oev_run", timeout=600)
    token = creds.token
    for side, f in (("left", left), ("right", right)):
        url = f"https://www.googleapis.com/drive/v3/files/{f['id']}?alt=media"
        cmd = (
            "aria2c -x 8 -s 8 --file-allocation=none "
            f"--header={shlex.quote('Authorization: Bearer ' + token)} "
            f"--dir=/tmp/oev_run --out={side}.mp4 {shlex.quote(url)} >/dev/null"
        )
        ssh(ip, port, cmd, timeout=1200)
    p = ssh(
        ip, port,
        "test -s /tmp/oev_run/left.mp4 && test -s /tmp/oev_run/right.mp4 && "
        "ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/oev_run/left.mp4 && "
        "ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/oev_run/right.mp4",
        capture=True, timeout=120,
    )
    print("Sample durations:\n" + p.stdout, flush=True)


def bootstrap(meta):
    ip, port = meta["ip"], meta["port"]
    scp_to(ip, port, REPO / "runpod_bootstrap.sh", "/tmp/runpod_bootstrap.sh")
    p = ssh(
        ip, port,
        "chmod +x /tmp/runpod_bootstrap.sh && stdbuf -oL -eL /tmp/runpod_bootstrap.sh",
        check=False, capture=True, timeout=3000,
    )
    (ARTIFACTS / "bootstrap_output.txt").write_text((p.stdout or "") + (p.stderr or ""))
    print(p.stdout or "", flush=True)
    if p.returncode != 0:
        raise RuntimeError(f"runpod_bootstrap.sh failed with {p.returncode}")
    actual = ssh(
        ip, port,
        "sed -n 's/^video-stitcher_sha=//p' /tmp/runpod_bootstrap_versions.log | tail -1",
        capture=True, timeout=30,
    ).stdout.strip()
    print(f"bootstrap_reco_sha={actual}", flush=True)
    if actual != EXPECTED_RECO_SHA:
        raise RuntimeError(f"expected Reco {EXPECTED_RECO_SHA}, got {actual}")


def export_model(meta, resolution):
    ip, port = meta["ip"], meta["port"]
    ssh(
        ip, port,
        "python3 -m venv /tmp/yolo-resolution-venv && "
        "/tmp/yolo-resolution-venv/bin/pip install -q --upgrade pip && "
        f"/tmp/yolo-resolution-venv/bin/pip install -q 'ultralytics=={ULTRALYTICS_VERSION}' onnxruntime-gpu",
        timeout=1800,
    )
    export_cmd = (
        "cd /tmp/oev_run && "
        f"/tmp/yolo-resolution-venv/bin/yolo export model=yolo26m.pt format=onnx imgsz={resolution} "
        "2>&1 | tee model_export.log"
    )
    p = ssh(ip, port, export_cmd, check=False, capture=True, timeout=2400)
    print(p.stdout or "", flush=True)
    if p.returncode != 0:
        raise RuntimeError(f"YOLO export failed with {p.returncode}")
    shape_code = (
        "import onnxruntime as ort; "
        "s=ort.InferenceSession('/tmp/oev_run/yolo26m.onnx', providers=['CPUExecutionProvider']); "
        "print('onnx_input_shape='+'x'.join(str(x) for x in s.get_inputs()[0].shape))"
    )
    cmd = (
        "cd /tmp/oev_run && test -s yolo26m.pt && test -s yolo26m.onnx && "
        f"{{ echo resolution={resolution}; sha256sum yolo26m.pt yolo26m.onnx; "
        f"/tmp/yolo-resolution-venv/bin/python3 -c {shlex.quote(shape_code)}; }} | tee model_identity.txt"
    )
    identity = ssh(ip, port, cmd, capture=True, timeout=180).stdout
    print(identity, flush=True)
    expected = f"onnx_input_shape=1x3x{resolution}x{resolution}"
    if expected not in identity:
        raise RuntimeError(f"expected {expected}, identity was:\n{identity}")


def run_oev(meta, resolution):
    ip, port = meta["ip"], meta["port"]
    wrapper = REPO / "runpod_yolo_resolution_local_remote.sh"
    run(["bash", "-n", wrapper])
    scp_to(ip, port, wrapper, "/tmp/oev_run/runpod_yolo_resolution_local_remote.sh")
    ssh(ip, port, "chmod +x /tmp/oev_run/runpod_yolo_resolution_local_remote.sh", timeout=30)
    env = {
        "SAMPLE_ID": SAMPLE_ID,
        "DURATION_S": str(DURATION_S),
        "LEFT_CLIP": "sample_02_left_180s.mp4",
        "RIGHT_CLIP": "sample_02_right_180s.mp4",
        "YOLO26_VARIANT": "yolo26m",
        "YOLO_RESOLUTION": str(resolution),
        "LOOKAHEAD": "1.5",
        "CLUSTER_ALPHA_OVERRIDE": OVERLAY,
    }
    exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    command = (
        "test -d /usr/local/cuda-12.8/lib64 && test -f /tmp/nvidia_egl_icd.json && "
        "export LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64:${LD_LIBRARY_PATH:-} && "
        "export VK_DRIVER_FILES=/tmp/nvidia_egl_icd.json && "
        "export VK_ICD_FILENAMES=/tmp/nvidia_egl_icd.json && unset DISPLAY; "
        f"cd /tmp/oev_run && {exports} stdbuf -oL -eL ./runpod_yolo_resolution_local_remote.sh"
    )
    start = now_iso()
    p = ssh(ip, port, command, check=False, capture=False, timeout=7200)
    end = now_iso()
    return p.returncode, start, end


def pull_artifacts(meta):
    ip, port = meta["ip"], meta["port"]
    remote_names = [
        "segment.log", "calibrate.log", "stitch.log", "acceptance.log", "match.json",
        "events.jsonl", "followcam.mp4", "gpu_telemetry.csv", "containment_metrics.json",
        "model_export.log", "model_identity.txt",
    ]
    for name in remote_names:
        try:
            scp_from(ip, port, f"/tmp/oev_run/{name}", ARTIFACTS / name, check=False, timeout=600)
        except Exception as exc:
            print(f"Artifact pull warning for {name}: {exc}", flush=True)


def upload_drive(drive, label):
    q = (
        f"name='Baseline' and '{OEV_DRIVE_ROOT}' in parents and trashed=false "
        "and mimeType='application/vnd.google-apps.folder'"
    )
    files = drive.files().list(
        q=q, fields="files(id)", supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    if files:
        folder = files[0]["id"]
    else:
        folder = drive.files().create(
            body={"name": "Baseline", "mimeType": "application/vnd.google-apps.folder", "parents": [OEV_DRIVE_ROOT]},
            fields="id", supportsAllDrives=True,
        ).execute()["id"]
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    uploads = [
        ("followcam.mp4", "video/mp4"),
        ("events.jsonl", "application/json"),
        ("stitch.log", "text/plain"),
        ("run_metadata.txt", "text/plain"),
        ("model_identity.txt", "text/plain"),
    ]
    for name, mime in uploads:
        path = ARTIFACTS / name
        if not path.is_file() or path.stat().st_size == 0:
            continue
        out_name = f"{label}-run{run_id}-{name}"
        uploaded = drive.files().create(
            body={"name": out_name, "parents": [folder]},
            media_body=MediaFileUpload(str(path), mimetype=mime, resumable=True),
            fields="id,size", supportsAllDrives=True,
        ).execute()
        print(f"Drive uploaded {out_name}: {uploaded.get('id')}", flush=True)


def write_metadata(cfg, meta, start, end, status):
    lines = {
        "label": cfg["label"],
        "resolution": cfg["resolution"],
        "sample_id": SAMPLE_ID,
        "duration_s": DURATION_S,
        "datacenter": meta.get("datacenter", ""),
        "gpu_type": meta.get("gpu_type", ""),
        "machine_id": meta.get("machine_id", ""),
        "driver_version": meta.get("driver_version", ""),
        "cost_per_hr": meta.get("cost_per_hr", ""),
        "processing_start": start or "",
        "processing_end": end or "",
        "experiment_ref": os.environ.get("GITHUB_REF_NAME", "experiment/yolo-highres-01"),
        "experiment_sha": os.environ.get("GITHUB_SHA", ""),
        "reco_sha": EXPECTED_RECO_SHA,
        "ultralytics_version": ULTRALYTICS_VERSION,
        "status": status,
    }
    (ARTIFACTS / "run_metadata.txt").write_text("".join(f"{k}={v}\n" for k, v in lines.items()))


def main():
    ARTIFACTS.mkdir(exist_ok=True)
    cfg = json.loads(CONFIG.read_text())
    resolution = int(cfg["resolution"])
    if resolution not in (1920, 2560):
        raise SystemExit(f"resolution must be 1920 or 2560, got {resolution}")
    cfg = {
        "resolution": resolution,
        "label": cfg.get("label") or f"yolo-resolution-{resolution}-180s",
        "target_datacenter": (cfg.get("target_datacenter") or "").strip(),
        "target_gpu": (cfg.get("target_gpu") or "").strip(),
    }
    api_key = os.environ["RUNPOD_API_KEY"]
    pub_key = make_keypair()
    pod_ids = []
    meta = {}
    start = end = None
    status = "failed"
    error = None
    try:
        print(f"CONTROLLED CONFIG: {json.dumps(cfg, sort_keys=True)}", flush=True)
        meta = allocate_healthy(api_key, pub_key, cfg["target_datacenter"], cfg["target_gpu"], pod_ids)
        print(f"Selected healthy pod: {json.dumps(meta, sort_keys=True)}", flush=True)
        bootstrap(meta)
        creds = google_creds()
        drive, left, right = resolve_sample_files(creds)
        print(f"Pinned Drive sample IDs: left={left['id']} right={right['id']}", flush=True)
        download_samples_to_pod(meta, creds, left, right)
        export_model(meta, resolution)
        rc, start, end = run_oev(meta, resolution)
        print(f"OEV remote exit={rc} processing={start} -> {end}", flush=True)
        pull_artifacts(meta)
        if rc != 0:
            raise RuntimeError(f"OEV remote script exited {rc}")
        for required in ("events.jsonl", "followcam.mp4", "model_identity.txt"):
            path = ARTIFACTS / required
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError(f"required artifact missing/empty: {required}")
        status = "success"
        write_metadata(cfg, meta, start, end, status)
        upload_drive(drive, cfg["label"])
    except Exception as exc:
        error = exc
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        if meta:
            try:
                pull_artifacts(meta)
            except Exception as pull_exc:
                print(f"Artifact pull after failure also failed: {pull_exc}", file=sys.stderr)
        write_metadata(cfg, meta, start, end, "failed")
    finally:
        for pod_id in dict.fromkeys(pod_ids):
            delete_pod(api_key, pod_id)
    if error is not None:
        raise SystemExit(1)
    print("Resolution arm completed successfully.", flush=True)


if __name__ == "__main__":
    main()
