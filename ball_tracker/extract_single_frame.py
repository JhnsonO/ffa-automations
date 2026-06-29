import argparse, os, subprocess, urllib.request

FPS = 29.97

def download_drive_file(file_id, dest_path, access_token):
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    print(f"Downloading to {dest_path}...")
    with urllib.request.urlopen(req) as r, open(dest_path, "wb") as f:
        total = 0
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk: break
            f.write(chunk)
            total += len(chunk)
            print(f"  {total//(1024*1024)} MB", end="\r", flush=True)
    print(f"\nDone: {total//(1024*1024)} MB")

ap = argparse.ArgumentParser()
ap.add_argument("--drive-file-id", required=True)
ap.add_argument("--access-token", required=True)
ap.add_argument("--frame", type=int, required=True)
ap.add_argument("--label", default="")
ap.add_argument("--output-dir", default="single_frame_output")
args = ap.parse_args()

os.makedirs(args.output_dir, exist_ok=True)
video_path = os.path.join(args.output_dir, "equirect_trim.mp4")
download_drive_file(args.drive_file_id, video_path, args.access_token)

ts = args.frame / FPS
raw = os.path.join(args.output_dir, "raw.jpg")
out = os.path.join(args.output_dir, f"frame_{args.frame}.jpg")
subprocess.run(["ffmpeg","-y","-ss",f"{ts:.4f}","-i",video_path,"-vframes","1","-q:v","2",raw], capture_output=True, check=True)
label = args.label or f"f{args.frame}"
subprocess.run(["ffmpeg","-y","-i",raw,"-vf",
    f"drawtext=text='{label}':fontsize=40:fontcolor=yellow:borderw=2:bordercolor=black:x=10:y=10",
    out], capture_output=True, check=True)
os.remove(raw)
print(f"Saved: {out}")
