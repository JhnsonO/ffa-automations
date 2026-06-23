#!/usr/bin/env python3
"""Download tracklets.json and gaps.json from Drive."""
import json, requests, os

creds      = json.loads(os.environ["YOUTUBE_CREDENTIALS"])
token_data = json.loads(os.environ["YOUTUBE_TOKEN"])

resp = requests.post("https://oauth2.googleapis.com/token", data={
    "client_id":     creds["installed"]["client_id"],
    "client_secret": creds["installed"]["client_secret"],
    "refresh_token": token_data["refresh_token"],
    "grant_type":    "refresh_token",
})
resp.raise_for_status()
access_token = resp.json()["access_token"]
headers = {"Authorization": f"Bearer {access_token}"}

FILES = {
    "tracklets.json": "1uUhqnTeF664xvdR-Tma-XFdmp5p-YroH",
    "gaps.json":      "1Q-BW7SmZ8PQxU0D3Nu0TehZCGlCNYPe4",
}

for name, file_id in FILES.items():
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    with open(name, "wb") as f:
        f.write(r.content)
    print(f"Downloaded {name} ({len(r.content)/1024:.1f} KB)")
