import json, os, sys, urllib.request

token_data = json.loads(os.environ["YOUTUBE_TOKEN"])
token = token_data.get("token") or token_data.get("access_token")

if not token:
    print("ERROR: No token found in YOUTUBE_TOKEN")
    sys.exit(1)

file_id = os.environ["DRIVE_FILE_ID"]
url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
print(f"Downloading {file_id} from Drive...")
try:
    with urllib.request.urlopen(req) as resp, open("/tmp/source.360", "wb") as out:
        total = 0
        while True:
            chunk = resp.read(4 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
            print(f"  {total / 1024 / 1024:.0f} MB downloaded", flush=True)
    print(f"Download complete: {total / 1024 / 1024:.1f} MB")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:500]}")
    sys.exit(1)
