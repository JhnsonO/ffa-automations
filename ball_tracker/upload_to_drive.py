#!/usr/bin/env python3
"""Upload a file to Drive folder."""
import json, requests, os, sys

local_path, drive_name, folder_id = sys.argv[1], sys.argv[2], sys.argv[3]

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
headers = {"Authorization": f"Bearer {access_token}"}

meta = {"name": drive_name, "parents": [folder_id]}
with open(local_path, "rb") as f:
    data = f.read()

resp = requests.post(
    "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
    headers=headers,
    files={
        "metadata": ("meta.json", json.dumps(meta), "application/json"),
        "file": (drive_name, data, "image/png"),
    }
)
resp.raise_for_status()
fi = resp.json()
print(f"Uploaded: {fi['name']} (id={fi['id']})")
