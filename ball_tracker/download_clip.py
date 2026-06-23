#!/usr/bin/env python3
"""Download test clip from Drive."""
import json, requests, os, sys

creds = json.loads(os.environ["YOUTUBE_CREDENTIALS"])
token_data = json.loads(os.environ["YOUTUBE_TOKEN"])

resp = requests.post("https://oauth2.googleapis.com/token", data={
    "client_id": creds["installed"]["client_id"],
    "client_secret": creds["installed"]["client_secret"],
    "refresh_token": token_data["refresh_token"],
    "grant_type": "refresh_token",
})
resp.raise_for_status()
access_token = resp.json()["access_token"]

file_id = "1z2p2FgLsjgvIIBw0HZXWEenckMLWpVNX"
url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
headers = {"Authorization": f"Bearer {access_token}"}

print("Downloading clip...", flush=True)
with requests.get(url, headers=headers, stream=True) as r:
    r.raise_for_status()
    total = 0
    with open("clip.mp4", "wb") as f:
        for chunk in r.iter_content(chunk_size=8*1024*1024):
            f.write(chunk)
            total += len(chunk)
            print(f"  {total/1e6:.0f} MB", flush=True)
print(f"Done: {total/1e6:.1f} MB")
