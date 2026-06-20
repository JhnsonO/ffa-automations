import json, os, sys, urllib.request
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/drive",
]

token_data = json.loads(os.environ["YOUTUBE_TOKEN"])
creds_data = json.loads(os.environ["YOUTUBE_CREDENTIALS"])

creds = Credentials(
    token=token_data.get("token"),
    refresh_token=token_data.get("refresh_token"),
    token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
    client_id=creds_data["installed"]["client_id"],
    client_secret=creds_data["installed"]["client_secret"],
    scopes=SCOPES,
)

if creds.expired and creds.refresh_token:
    print("Refreshing access token...")
    creds.refresh(Request())
    print("Token refreshed")

token = creds.token
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
