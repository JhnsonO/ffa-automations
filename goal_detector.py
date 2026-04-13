import cv2
import numpy as np
import os
import json
import re
import base64
from datetime import datetime, timedelta
from tqdm import tqdm
from openai import OpenAI

# ---------------- CONFIG ---------------- #
VIDEO_PATH = "match.mp4"
OUTPUT_DIR = "goals_output"
CLIP_BEFORE = 10  # seconds before event
CLIP_AFTER = 5    # seconds after event

MOTION_THRESHOLD = None  # Auto-calibrated from first 60 seconds
MIN_EVENT_GAP = 5         # seconds between events

FRAME_SAMPLE_RATE = 2     # frames per second for motion detection
GPT_FRAME_SAMPLES = 3     # frames sent to GPT per event

client = OpenAI()

# ---------------------------------------- #

def extract_start_time_from_filename(filename):
    """
    Attempts to extract datetime from filename.
    Example formats:
      match_2026-04-13_18-30.mp4
      20260413_183000.mp4
    """
    patterns = [
        r'(\d{4}-\d{2}-\d{2})[_ ](\d{2}[-:]\d{2})',
        r'(\d{8})[_ ](\d{6})'
    ]
    for pattern in patterns:
        match = re.search(pattern, filename)
        if match:
            try:
                if "-" in match.group(1):
                    dt_str = f"{match.group(1)} {match.group(2).replace('-', ':')}"
                    return datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                else:
                    dt_str = match.group(1) + match.group(2)
                    return datetime.strptime(dt_str, "%Y%m%d%H%M%S")
            except Exception:
                pass
    return None


def calibrate_threshold(video_path, calibration_seconds=60):
    """
    Sample the first N seconds of footage to auto-set motion threshold.
    Returns mean + 2.5 standard deviations as the threshold.
    """
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_interval = max(1, int(fps / FRAME_SAMPLE_RATE))
    max_frames = int(calibration_seconds * fps)

    prev_gray = None
    scores = []
    frame_idx = 0

    print(f"📐 Calibrating motion threshold from first {calibration_seconds}s...")

    while frame_idx < max_frames:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % frame_interval == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if prev_gray is not None:
                diff = cv2.absdiff(prev_gray, gray)
                scores.append(np.mean(diff))
            prev_gray = gray
        frame_idx += 1

    cap.release()

    if not scores:
        return 15.0  # fallback

    threshold = np.mean(scores) + 2.5 * np.std(scores)
    print(f"✅ Threshold set to {threshold:.2f} (baseline mean: {np.mean(scores):.2f})")
    return threshold


def compute_motion_scores(video_path, threshold):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_interval = max(1, int(fps / FRAME_SAMPLE_RATE))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    prev_gray = None
    motion_scores = []
    timestamps = []
    frame_idx = 0

    for _ in tqdm(range(total_frames), desc="🔍 Scanning footage"):
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % frame_interval == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if prev_gray is not None:
                diff = cv2.absdiff(prev_gray, gray)
                score = np.mean(diff)
                motion_scores.append(score)
                timestamps.append(frame_idx / fps)
            prev_gray = gray
        frame_idx += 1

    cap.release()
    return timestamps, motion_scores


def detect_motion_spikes(timestamps, scores, threshold):
    spikes = [t for t, s in zip(timestamps, scores) if s > threshold]

    events = []
    last_time = -999
    for t in spikes:
        if t - last_time > MIN_EVENT_GAP:
            events.append(t)
            last_time = t

    return events


def sample_frames(video_path, timestamp, num_samples):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frames = []

    for i in range(num_samples):
        t = timestamp + (i * 0.5)
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ret, frame = cap.read()
        if ret:
            _, buffer = cv2.imencode(".jpg", frame)
            frames.append(base64.b64encode(buffer.tobytes()).decode("utf-8"))

    cap.release()
    return frames


def is_celebration(frames):
    """
    Send frames to GPT-4o Vision to confirm celebration.
    """
    content = [
        {
            "type": "text",
            "text": "Are the players celebrating a goal in these images? Answer YES or NO only."
        }
    ] + [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{frame}",
                "detail": "low"
            }
        }
        for frame in frames
    ]

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": content}],
        max_tokens=5
    )

    answer = response.choices[0].message.content.strip().upper()
    return "YES" in answer


def extract_clip(video_path, start, end, output_path):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    cap.set(cv2.CAP_PROP_POS_FRAMES, int(start * fps))

    while cap.get(cv2.CAP_PROP_POS_MSEC) / 1000 < end:
        ret, frame = cap.read()
        if not ret:
            break
        out.write(frame)

    cap.release()
    out.release()


def format_timestamp(seconds):
    return str(timedelta(seconds=int(seconds)))


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Auto-calibrate threshold
    threshold = calibrate_threshold(VIDEO_PATH)

    # Compute motion across full video
    timestamps, scores = compute_motion_scores(VIDEO_PATH, threshold)

    # Detect spike events
    events = detect_motion_spikes(timestamps, scores, threshold)
    print(f"\n⚡ Found {len(events)} candidate events")

    start_time = extract_start_time_from_filename(os.path.basename(VIDEO_PATH))
    if start_time:
        print(f"🕐 Match start time detected: {start_time.strftime('%H:%M:%S')}")
    else:
        print("⚠️  No start time found in filename — real-world clock times will be null")

    results = []
    clip_index = 1

    for event in events:
        print(f"\n🧠 Checking event at {format_timestamp(event)}...")

        frames = sample_frames(VIDEO_PATH, event, GPT_FRAME_SAMPLES)
        if not frames:
            continue

        try:
            if is_celebration(frames):
                print("✅ Goal detected!")

                clip_start = max(0, event - CLIP_BEFORE)
                clip_end = event + CLIP_AFTER
                clip_name = f"goal_{clip_index}.mp4"
                clip_path = os.path.join(OUTPUT_DIR, clip_name)

                extract_clip(VIDEO_PATH, clip_start, clip_end, clip_path)

                real_time = None
                if start_time:
                    real_time = (start_time + timedelta(seconds=event)).strftime("%H:%M:%S")

                results.append({
                    "clip": clip_name,
                    "video_time": format_timestamp(event),
                    "real_time": real_time
                })

                clip_index += 1
            else:
                print("❌ Not a goal")

        except Exception as e:
            print(f"⚠️  GPT error: {e}")

    # Save summary JSON
    summary_path = os.path.join(OUTPUT_DIR, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=4)

    print(f"\n🎬 Done! {len(results)} goal(s) found. Clips saved in: {OUTPUT_DIR}")
    print(f"📄 Summary: {summary_path}")


if __name__ == "__main__":
    main()
