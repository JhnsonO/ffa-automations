#!/usr/bin/env python3
"""Pure detector A/B for yolo26m vs SoccerNet-v3D yolo-sn-ball-opt.

Experiment-only. Both detectors receive the exact same decoded BGR frame and
an identical 1920x1920 RGB/114-letterbox/float32 tensor. No tracker, panner,
bridge, renderer or OEV world-state logic participates in this comparison.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import pathlib
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
import onnxruntime as ort
from ultralytics import YOLO

IMG_SIZE = 1920
CONF = 0.10
IOU = 0.70
MAX_DET = 300
CONTROL_BALL_CLASS = 32
EXPERIMENT_BALL_CLASS = 0
REVIEW_WINDOWS = [
    ("early loss", 6.0, 15.0),
    ("22s near-loss", 20.0, 24.0),
    ("36-41s losses", 34.0, 43.0),
    ("1:26 loss", 84.0, 90.0),
    ("1:36 brief loss", 94.0, 99.0),
    ("1:59-2:09 stationary goal ball", 117.0, 131.0),
    ("2:14-2:19 long-pass recovery", 132.0, 141.0),
]

@dataclass
class Det:
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float
    camera: str

    @property
    def size_px(self) -> float:
        return math.sqrt(max(0.0, self.x2-self.x1) * max(0.0, self.y2-self.y1))


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def onnx_meta(path: pathlib.Path) -> dict[str, Any]:
    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    return {
        "inputs": [{"name": i.name, "shape": list(i.shape), "type": i.type} for i in sess.get_inputs()],
        "outputs": [{"name": o.name, "shape": list(o.shape), "type": o.type} for o in sess.get_outputs()],
        "metadata": dict(sess.get_modelmeta().custom_metadata_map),
    }


def preprocess(frame_bgr: np.ndarray) -> tuple[np.ndarray, float, float, float]:
    h, w = frame_bgr.shape[:2]
    scale = min(IMG_SIZE / w, IMG_SIZE / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((IMG_SIZE, IMG_SIZE, 3), 114, dtype=np.uint8)
    px = (IMG_SIZE - nw) // 2
    py = (IMG_SIZE - nh) // 2
    canvas[py:py+nh, px:px+nw] = resized
    tensor = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255.0)
    return tensor, scale, float(px), float(py)


def decode_e2e(output: np.ndarray, *, class_id: int, camera: str, scale: float, pad_x: float, pad_y: float, fw: int, fh: int) -> list[Det]:
    arr = np.asarray(output)
    if arr.ndim == 3:
        arr = arr[0]
    if arr.ndim != 2 or arr.shape[1] != 6:
        raise RuntimeError(f"Expected end-to-end [N,6] output, got {arr.shape}")
    out: list[Det] = []
    for x1, y1, x2, y2, score, cls in arr:
        if float(score) < CONF or int(round(float(cls))) != class_id:
            continue
        ox1 = max(0.0, min(float(fw), (float(x1) - pad_x) / scale))
        oy1 = max(0.0, min(float(fh), (float(y1) - pad_y) / scale))
        ox2 = max(0.0, min(float(fw), (float(x2) - pad_x) / scale))
        oy2 = max(0.0, min(float(fh), (float(y2) - pad_y) / scale))
        if ox2 <= ox1 or oy2 <= oy1:
            continue
        out.append(Det(float(score), ox1, oy1, ox2, oy2, camera))
    out.sort(key=lambda d: d.conf, reverse=True)
    return out


def make_session(path: pathlib.Path) -> ort.InferenceSession:
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    sess = ort.InferenceSession(str(path), providers=providers)
    active = sess.get_providers()
    if "CUDAExecutionProvider" not in active:
        raise RuntimeError(f"CUDAExecutionProvider unavailable for {path}: {active}")
    return sess


def run_session(sess: ort.InferenceSession, tensor: np.ndarray) -> tuple[np.ndarray, float]:
    name = sess.get_inputs()[0].name
    t0 = time.perf_counter()
    output = sess.run(None, {name: tensor})[0]
    ms = (time.perf_counter() - t0) * 1000.0
    return output, ms


def pctile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    return float(np.percentile(np.asarray(xs, dtype=float), q))


def gap_stats(present: list[bool], fps: float) -> dict[str, Any]:
    gaps: list[int] = []
    cur = 0
    for p in present:
        if p:
            if cur:
                gaps.append(cur)
                cur = 0
        else:
            cur += 1
    if cur:
        gaps.append(cur)
    thresholds = [0.100, 0.250, 0.500, 1.0, 2.0]
    return {
        "gap_count": len(gaps),
        "longest_gap_frames": max(gaps, default=0),
        "longest_gap_s": max(gaps, default=0) / fps,
        "gap_counts_over": {str(t): sum((g / fps) > t for g in gaps) for t in thresholds},
        "reacquisition_latency_s": {
            "mean": statistics.mean([g / fps for g in gaps]) if gaps else 0.0,
            "p50": pctile([g / fps for g in gaps], 50) or 0.0,
            "p95": pctile([g / fps for g in gaps], 95) or 0.0,
            "max": max([g / fps for g in gaps], default=0.0),
        },
    }


def draw(frame: np.ndarray, control: list[Det], experiment: list[Det], label: str) -> np.ndarray:
    img = frame.copy()
    for d in control[:3]:
        cv2.rectangle(img, (int(d.x1), int(d.y1)), (int(d.x2), int(d.y2)), (0,255,0), 3)
        cv2.putText(img, f"Y26m {d.conf:.2f}", (int(d.x1), max(25,int(d.y1)-8)), cv2.FONT_HERSHEY_SIMPLEX, .8, (0,255,0), 2)
    for d in experiment[:3]:
        cv2.rectangle(img, (int(d.x1), int(d.y1)), (int(d.x2), int(d.y2)), (0,0,255), 3)
        cv2.putText(img, f"SN {d.conf:.2f}", (int(d.x1), min(img.shape[0]-10,int(d.y2)+28)), cv2.FONT_HERSHEY_SIMPLEX, .8, (0,0,255), 2)
    cv2.putText(img, label, (30,50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255,255,255), 3)
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--left", required=True)
    ap.add_argument("--right", required=True)
    ap.add_argument("--control-model", required=True)
    ap.add_argument("--soccernet-pt", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    out = pathlib.Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    review_dir = out / "phase1_review"
    review_dir.mkdir(exist_ok=True)
    control_path = pathlib.Path(args.control_model)
    pt_path = pathlib.Path(args.soccernet_pt)

    # Checkpoint introspection is a hard gate: exact requested model, detect task,
    # one class named ball. Do not silently substitute another checkpoint.
    y = YOLO(str(pt_path))
    names = {int(k): str(v) for k, v in dict(y.names).items()}
    if len(names) != 1 or names.get(0, "").strip().lower() != "ball":
        raise RuntimeError(f"Unexpected SoccerNet classes: {names}")
    task = str(getattr(y, "task", ""))
    if task != "detect":
        raise RuntimeError(f"Expected detect task, got {task!r}")
    model_yaml = getattr(getattr(y, "model", None), "yaml", {}) or {}
    ckpt = getattr(y, "ckpt", {}) or {}
    train_args = ckpt.get("train_args", {}) if isinstance(ckpt, dict) else {}
    try:
        params = int(sum(p.numel() for p in y.model.parameters()))
    except Exception:
        params = -1

    # Export ONLY for environment compatibility. Resolution is explicitly fixed
    # to the accepted control's 1920. nms=True gives Reco-compatible [N,6].
    export_path = pathlib.Path(y.export(
        format="onnx", imgsz=IMG_SIZE, dynamic=False, nms=True,
        conf=CONF, iou=IOU, max_det=MAX_DET, simplify=False,
    ))
    sn_onnx = out / "yolo-sn-ball-opt-1920-nms010.onnx"
    if export_path.resolve() != sn_onnx.resolve():
        sn_onnx.write_bytes(export_path.read_bytes())

    control_meta = onnx_meta(control_path)
    sn_meta = onnx_meta(sn_onnx)
    cshape = control_meta["inputs"][0]["shape"]
    sshape = sn_meta["inputs"][0]["shape"]
    if cshape != [1,3,IMG_SIZE,IMG_SIZE]:
        raise RuntimeError(f"Control inference resolution drift: {cshape}")
    if sshape != [1,3,IMG_SIZE,IMG_SIZE]:
        raise RuntimeError(f"SoccerNet inference resolution drift: {sshape}")
    sout = sn_meta["outputs"][0]["shape"]
    if len(sn_meta["outputs"]) != 1 or len(sout) != 3 or sout[-1] != 6:
        raise RuntimeError(f"SoccerNet export incompatible with end-to-end detector path: {sn_meta['outputs']}")

    metadata = {
        "experiment": "soccernet-ball-detector-01-phase1",
        "control_model": str(control_path),
        "control_sha256": sha256(control_path),
        "control_onnx": control_meta,
        "soccernet_source": "mguti97/SoccerNet-v3D release v1.0.0 yolo-sn-ball-opt.pt",
        "soccernet_pt_sha256": sha256(pt_path),
        "soccernet_pt_size": pt_path.stat().st_size,
        "soccernet_task": task,
        "soccernet_classes": names,
        "soccernet_model_yaml": model_yaml,
        "soccernet_train_args": train_args,
        "soccernet_parameter_count": params,
        "soccernet_export": str(sn_onnx),
        "soccernet_export_sha256": sha256(sn_onnx),
        "soccernet_onnx": sn_meta,
        "inference_resolution": IMG_SIZE,
        "confidence_threshold": CONF,
        "iou": IOU,
        "max_det": MAX_DET,
        "preprocess": "same decoded BGR frame -> RGB -> bilinear centered letterbox rgb(114) -> /255 float32 CHW",
    }
    (out / "phase1_model_metadata.json").write_text(json.dumps(metadata, indent=2, default=str))

    control_sess = make_session(control_path)
    sn_sess = make_session(sn_onnx)
    # Warm up both on the exact same tensor shape.
    z = np.full((1,3,IMG_SIZE,IMG_SIZE), 114/255.0, dtype=np.float32)
    for _ in range(3):
        run_session(control_sess, z)
        run_session(sn_sess, z)

    caps = {"Left": cv2.VideoCapture(args.left), "Right": cv2.VideoCapture(args.right)}
    for name, cap in caps.items():
        if not cap.isOpened():
            raise RuntimeError(f"Could not open {name} source")
    fps = float(caps["Left"].get(cv2.CAP_PROP_FPS))
    fps_r = float(caps["Right"].get(cv2.CAP_PROP_FPS))
    if abs(fps - fps_r) > 0.01:
        raise RuntimeError(f"FPS mismatch left={fps} right={fps_r}")
    frame_count = min(int(c.get(cv2.CAP_PROP_FRAME_COUNT)) for c in caps.values())

    frame_rows: list[dict[str, Any]] = []
    camera_rows: list[dict[str, Any]] = []
    control_ms: list[float] = []
    sn_ms: list[float] = []
    preprocess_ms: list[float] = []
    saved = 0
    max_saved = 420

    for idx in range(frame_count):
        per_camera: dict[str, tuple[list[Det], list[Det]]] = {}
        raw_frames: dict[str, np.ndarray] = {}
        for camera, cap in caps.items():
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f"Decode ended early camera={camera} frame={idx}")
            raw_frames[camera] = frame
            t0 = time.perf_counter()
            tensor, scale, px, py = preprocess(frame)
            preprocess_ms.append((time.perf_counter() - t0) * 1000.0)
            fw, fh = frame.shape[1], frame.shape[0]
            co, cms = run_session(control_sess, tensor)
            so, sms = run_session(sn_sess, tensor)
            control_ms.append(cms)
            sn_ms.append(sms)
            cd = decode_e2e(co, class_id=CONTROL_BALL_CLASS, camera=camera, scale=scale, pad_x=px, pad_y=py, fw=fw, fh=fh)
            sd = decode_e2e(so, class_id=EXPERIMENT_BALL_CLASS, camera=camera, scale=scale, pad_x=px, pad_y=py, fw=fw, fh=fh)
            per_camera[camera] = (cd, sd)
            camera_rows.append({
                "frame_index": idx, "time_s": idx/fps, "camera": camera,
                "control_present": bool(cd), "control_conf": cd[0].conf if cd else "",
                "control_size_px": cd[0].size_px if cd else "",
                "experiment_present": bool(sd), "experiment_conf": sd[0].conf if sd else "",
                "experiment_size_px": sd[0].size_px if sd else "",
            })

        all_c = [d for cd, _ in per_camera.values() for d in cd]
        all_s = [d for _, sd in per_camera.values() for d in sd]
        all_c.sort(key=lambda d:d.conf, reverse=True)
        all_s.sort(key=lambda d:d.conf, reverse=True)
        cp, sp = bool(all_c), bool(all_s)
        category = "both" if cp and sp else "control_only" if cp else "soccernet_only" if sp else "neither"
        t = idx / fps
        frame_rows.append({
            "frame_index": idx, "time_s": t, "control_present": cp,
            "control_conf": all_c[0].conf if cp else "", "control_camera": all_c[0].camera if cp else "",
            "control_size_px": all_c[0].size_px if cp else "",
            "soccernet_present": sp, "soccernet_conf": all_s[0].conf if sp else "",
            "soccernet_camera": all_s[0].camera if sp else "", "soccernet_size_px": all_s[0].size_px if sp else "",
            "category": category,
        })

        in_review = any(a <= t < b for _, a, b in REVIEW_WINDOWS)
        # Save representative disagreement frames every ~0.5s plus every ~1s
        # in known review windows. These are for human validation, not metrics.
        want = (category in {"soccernet_only","control_only"} and idx % max(1,round(fps/2)) == 0) or (in_review and idx % max(1,round(fps)) == 0)
        if want and saved < max_saved:
            for camera, frame in raw_frames.items():
                cd, sd = per_camera[camera]
                ann = draw(frame, cd, sd, f"{camera} t={t:.2f}s frame={idx} {category}")
                # Preserve enough detail for tiny-ball review while controlling artifact size.
                if ann.shape[1] > 2560:
                    nh = int(ann.shape[0] * 2560 / ann.shape[1])
                    ann = cv2.resize(ann, (2560, nh), interpolation=cv2.INTER_AREA)
                cv2.imwrite(str(review_dir / f"f{idx:05d}_{camera.lower()}_{category}.jpg"), ann, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                saved += 1
                if saved >= max_saved:
                    break

        if idx and idx % 300 == 0:
            print(f"PHASE1_PROGRESS frame={idx}/{frame_count} t={t:.1f}s", flush=True)

    for cap in caps.values():
        cap.release()

    with (out / "phase1_frame_comparison.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(frame_rows[0].keys()))
        w.writeheader(); w.writerows(frame_rows)
    with (out / "phase1_camera_comparison.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(camera_rows[0].keys()))
        w.writeheader(); w.writerows(camera_rows)

    control_present = [bool(r["control_present"]) for r in frame_rows]
    sn_present = [bool(r["soccernet_present"]) for r in frame_rows]
    recovered = [r for r in frame_rows if r["category"] == "soccernet_only"]
    lost = [r for r in frame_rows if r["category"] == "control_only"]
    both = [r for r in frame_rows if r["category"] == "both"]
    neither = [r for r in frame_rows if r["category"] == "neither"]
    cconfs = [float(r["control_conf"]) for r in frame_rows if r["control_present"]]
    sconfs = [float(r["soccernet_conf"]) for r in frame_rows if r["soccernet_present"]]

    def detector_summary(present: list[bool], confs: list[float], infer_ms: list[float]) -> dict[str, Any]:
        return {
            "present_frames": sum(present), "present_pct": 100*sum(present)/len(present),
            "confidence": {"mean": statistics.mean(confs) if confs else None, "p10": pctile(confs,10), "p50": pctile(confs,50), "p90": pctile(confs,90)},
            "gaps": gap_stats(present, fps),
            "inference_ms_per_camera_frame": {"mean": statistics.mean(infer_ms), "p50": pctile(infer_ms,50), "p95": pctile(infer_ms,95)},
        }

    windows = []
    for label, a, b in REVIEW_WINDOWS:
        rs = [r for r in frame_rows if a <= float(r["time_s"]) < b]
        windows.append({
            "label": label, "start_s": a, "end_s": b, "frames": len(rs),
            "control_present": sum(bool(r["control_present"]) for r in rs),
            "soccernet_present": sum(bool(r["soccernet_present"]) for r in rs),
            "soccernet_only": sum(r["category"]=="soccernet_only" for r in rs),
            "control_only": sum(r["category"]=="control_only" for r in rs),
            "neither": sum(r["category"]=="neither" for r in rs),
        })

    # Contiguous disagreement intervals for timestamp-level review.
    intervals = []
    for category in ("soccernet_only", "control_only", "neither"):
        start = None
        prev = None
        for r in frame_rows:
            i = int(r["frame_index"])
            if r["category"] == category:
                if start is None: start = i
                prev = i
            elif start is not None:
                intervals.append({"category": category, "start_frame": start, "end_frame": prev, "start_s": start/fps, "end_s": (prev+1)/fps, "duration_s": (prev-start+1)/fps})
                start = prev = None
        if start is not None:
            intervals.append({"category": category, "start_frame": start, "end_frame": prev, "start_s": start/fps, "end_s": (prev+1)/fps, "duration_s": (prev-start+1)/fps})
    intervals.sort(key=lambda x:(-x["duration_s"], x["start_s"]))

    summary = {
        "schema_version": 1,
        "phase": 1,
        "sample": "sample_02",
        "fps": fps,
        "frames": len(frame_rows),
        "source_camera_frames": len(camera_rows),
        "same_decoded_frame_guarantee": True,
        "control": detector_summary(control_present, cconfs, control_ms),
        "soccernet": detector_summary(sn_present, sconfs, sn_ms),
        "cross_detector": {
            "both_frames": len(both), "soccernet_only_frames": len(recovered),
            "control_only_frames": len(lost), "neither_frames": len(neither),
            "net_presence_delta_frames": sum(sn_present)-sum(control_present),
            "net_presence_delta_pp": 100*(sum(sn_present)-sum(control_present))/len(frame_rows),
        },
        "preprocess_ms_per_camera_frame": {"mean": statistics.mean(preprocess_ms), "p50": pctile(preprocess_ms,50), "p95": pctile(preprocess_ms,95)},
        "review_windows": windows,
        "longest_disagreement_intervals": intervals[:100],
        "human_review": {"status": "pending", "review_images": saved, "note": "Automated presence is not human-visible recall. Review images must be inspected before the Phase 1 gate."},
    }
    (out / "phase1_summary.json").write_text(json.dumps(summary, indent=2))

    # TSV-like concise report is convenient in Actions logs.
    lines = [
        "PHASE1_DETECTOR_AB_COMPLETE",
        f"frames={len(frame_rows)} fps={fps:.5f}",
        f"control_present={sum(control_present)} ({100*sum(control_present)/len(frame_rows):.2f}%)",
        f"soccernet_present={sum(sn_present)} ({100*sum(sn_present)/len(frame_rows):.2f}%)",
        f"soccernet_only={len(recovered)} control_only={len(lost)} neither={len(neither)}",
        f"control_longest_miss_s={summary['control']['gaps']['longest_gap_s']:.3f}",
        f"soccernet_longest_miss_s={summary['soccernet']['gaps']['longest_gap_s']:.3f}",
        f"control_infer_mean_ms={summary['control']['inference_ms_per_camera_frame']['mean']:.3f}",
        f"soccernet_infer_mean_ms={summary['soccernet']['inference_ms_per_camera_frame']['mean']:.3f}",
    ]
    for w in windows:
        lines.append(f"window={w['label']} control={w['control_present']}/{w['frames']} soccernet={w['soccernet_present']}/{w['frames']} sn_only={w['soccernet_only']} control_only={w['control_only']} neither={w['neither']}")
    (out / "phase1_summary.txt").write_text("\n".join(lines)+"\n")
    print("\n".join(lines), flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
