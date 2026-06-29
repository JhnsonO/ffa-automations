"""
extract_anchor_frames.py
Downloads equirect_trim.mp4 from Google Drive, extracts one frame per anchor
at its midpoint, draws tracklet ID + conf label, composites into a contact sheet.
"""
import argparse, json, os, sys, math, subprocess, tempfile
import urllib.request
from pathlib import Path

ANCHORS = [
  {"id":"T0003","frame":21,"span":20,"conf":0.795},
  {"id":"T0005","frame":42,"span":22,"conf":0.73},
  {"id":"T0009","frame":89,"span":76,"conf":0.775},
  {"id":"T0016","frame":147,"span":29,"conf":0.432},
  {"id":"T0073","frame":968,"span":76,"conf":0.681},
  {"id":"T0083","frame":1026,"span":14,"conf":0.404},
  {"id":"T0101","frame":1147,"span":8,"conf":0.37},
  {"id":"T0106","frame":1203,"span":29,"conf":0.396},
  {"id":"T0207","frame":2207,"span":30,"conf":0.817},
  {"id":"T0205","frame":2202,"span":43,"conf":0.416},
  {"id":"T0218","frame":2263,"span":34,"conf":0.721},
  {"id":"T0224","frame":2283,"span":24,"conf":0.531},
  {"id":"T0234","frame":2330,"span":19,"conf":0.421},
  {"id":"T0237","frame":2347,"span":27,"conf":0.69},
  {"id":"T0241","frame":2394,"span":73,"conf":0.307},
  {"id":"T0265","frame":2521,"span":25,"conf":0.582},
  {"id":"T0270","frame":2557,"span":37,"conf":0.685},
  {"id":"T0291","frame":2710,"span":16,"conf":0.425},
  {"id":"T0328","frame":3083,"span":29,"conf":0.265},
  {"id":"T0351","frame":3381,"span":119,"conf":0.784},
  {"id":"T0356","frame":3440,"span":67,"conf":0.227},
  {"id":"T0366","frame":3490,"span":13,"conf":0.686},
  {"id":"T0365","frame":3505,"span":43,"conf":0.671},
  {"id":"T0371","frame":3563,"span":63,"conf":0.748},
  {"id":"T0373","frame":3579,"span":34,"conf":0.438},
]

FPS = 29.97  # GoPro MAX equirect

def download_drive_file(file_id, dest_path, access_token):
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    print(f"Downloading {file_id} -> {dest_path} ...")
    with urllib.request.urlopen(req) as r, open(dest_path, "wb") as f:
        total = 0
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            print(f"  {total // (1024*1024)} MB", end="\r", flush=True)
    print(f"\nDownloaded {total // (1024*1024)} MB")

def extract_frame(video_path, frame_num, out_path):
    ts = frame_num / FPS
    subprocess.run([
        "ffmpeg", "-y", "-ss", f"{ts:.4f}", "-i", video_path,
        "-vframes", "1", "-q:v", "2", out_path
    ], capture_output=True, check=True)

def label_image(img_path, label, out_path):
    """Use ffmpeg drawtext to stamp label onto image."""
    subprocess.run([
        "ffmpeg", "-y", "-i", img_path,
        "-vf", f"drawtext=text='{label}':fontsize=32:fontcolor=white:borderw=2:bordercolor=black:x=10:y=10",
        out_path
    ], capture_output=True, check=True)

def make_contact_sheet(frame_paths, labels, out_path, cols=5, thumb_w=640, thumb_h=360):
    rows = math.ceil(len(frame_paths) / cols)
    # Build ffmpeg tile filter
    inputs = []
    for p in frame_paths:
        inputs += ["-i", p]
    n = len(frame_paths)
    # Scale each input
    filter_parts = [f"[{i}:v]scale={thumb_w}:{thumb_h}[v{i}]" for i in range(n)]
    tile_inputs = "".join(f"[v{i}]" for i in range(n))
    filter_parts.append(f"{tile_inputs}xstack=inputs={n}:layout=" + "|".join(
        f"{(i % cols) * thumb_w}_{(i // cols) * thumb_h}" for i in range(n)
    ) + f":fill=black[out]")
    filter_str = ";".join(filter_parts)
    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_str,
        "-map", "[out]", "-frames:v", "1", out_path
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(result.stderr.decode()[-500:])
        raise RuntimeError("Contact sheet failed")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drive-file-id", required=True)
    ap.add_argument("--access-token", required=True)
    ap.add_argument("--output-dir", default="anchor_frames_output")
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    video_path = os.path.join(args.output_dir, "equirect_trim.mp4")

    download_drive_file(args.drive_file_id, video_path, args.access_token)

    frame_paths = []
    for a in ANCHORS:
        raw = os.path.join(args.output_dir, f"{a['id']}_raw.jpg")
        labelled = os.path.join(args.output_dir, f"{a['id']}.jpg")
        print(f"Extracting frame {a['frame']} for {a['id']} ...")
        extract_frame(video_path, a['frame'], raw)
        label = f"{a['id']} f{a['frame']} span{a['span']} conf{a['conf']}"
        label_image(raw, label, labelled)
        frame_paths.append(labelled)
        os.remove(raw)

    print("Building contact sheet ...")
    sheet_path = os.path.join(args.output_dir, "anchor_contact_sheet.jpg")
    labels = [f"{a['id']}" for a in ANCHORS]
    make_contact_sheet(frame_paths, labels, sheet_path, cols=5)
    print(f"Contact sheet saved: {sheet_path}")

if __name__ == "__main__":
    main()
