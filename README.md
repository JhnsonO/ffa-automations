# FFA Automations

Automated pipeline for FFA Leicester Football — GoPro session recording, YouTube upload, clip extraction, and Google Sheets management.

---

## Architecture Overview

```
GoPro Cloud
    │
    ▼
gopro_uploader.py ──► YouTube (unlisted) + Drive: FFA/Sources/
    │
    ▼
sheet_manager.py sync-videos  (every 30 mins, RSS feed)
    │  Creates a tab in FFA Clips Sheet for each newly-public session video
    │
    ▼
Kris fills timestamps in sheet (Start / End / Name / Tags)
    │
    ▼
sheet_manager.py process-clips  (every 6 hours)
    │  yt-dlp --download-sections → ffmpeg re-encode → Drive: FFA/Clips/<video>/
    │  Writes shareable Drive link back to sheet
    ▼
Kris downloads clips from Drive for TikTok/Instagram
```

---

## Infrastructure

### GitHub Actions (Scheduled workflows)
| Workflow | Schedule | Purpose |
|----------|----------|---------|
| `gopro-upload.yml` | Every 30 mins | GoPro Cloud → YouTube + Drive sources |
| `sheet-sync.yml` | Every 30 mins | RSS feed → create sheet tabs + process Add Video tab |
| `clip-extractor.yml` | Every 6 hours | Cut clips from timestamps, upload to Drive |
| `check-cookies.yml` | Monday 9am | Check GoPro cookies validity, raise GitHub issue if expired |
| `cleanup-and-sort.yml` | Manual | Sort Index, hide processed tabs |

### Self-Hosted Runner (Vultr)
- **Provider:** Vultr London
- **Plan:** $10/month (2GB RAM, 52GB disk)
- **IP:** `136.244.71.153`
- **OS:** Ubuntu 22.04 LTS
- **Runner name:** `vultr-ffa`
- **Service:** `actions.runner.JhnsonO-ffa-automations.vultr-ffa.service`

### GCP Project
- **Project:** `autoupload-452300` under `footffa@gmail.com`
- **Service account:** `ffa-sheet-manager@autoupload-452300.iam.gserviceaccount.com`
- **Used for:** Google Sheets read/write only (not Drive — Drive uses user OAuth token)

---

## GitHub Secrets

| Secret | Purpose |
|--------|---------|
| `YOUTUBE_TOKEN` | OAuth token — youtube.upload + youtube.readonly + drive scopes |
| `YOUTUBE_CREDENTIALS` | YouTube OAuth credentials |
| `YOUTUBE_COOKIES` | YouTube cookies for yt-dlp (legacy fallback) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account for Sheets API |
| `GOPRO_COOKIES` | GoPro auth cookies |
| `GOPRO_EMAIL` | GoPro account email |
| `GOPRO_PASSWORD` | GoPro account password |

---

## Key IDs

| Resource | ID |
|----------|----|
| FFA Clips Sheet | `1AKJlZ_Ze7rTH-Ve3W_ZObvWOxq5Pige4QOHgLdy0TB8` |
| FFA Drive Folder | `1KL89cURI4PR2N7YUDxWaExpsZc8mzpt3` |
| FFA YouTube Channel | `UCSj-hQdqQ9La4FMM3HFqvXw` |

---

## Key Files

| File | Purpose |
|------|---------|
| `gopro_uploader.py` | GoPro Cloud → YouTube pipeline + Drive source upload |
| `sheet_manager.py` | Sheet sync, clip extraction, Add Video processing |
| `cleanup_and_sort.py` | Sorts Index oldest-first, hides processed tabs |
| `.ffa_sheet_id` | FFA Clips sheet ID |
| `.ffa_drive_folder_id` | FFA Drive root folder ID |

---

## Vultr Runner Setup (Rebuild Guide)

If you ever need to migrate to a new VM, follow these steps exactly.

### 1. Provision the server
- Provider: Vultr
- Plan: **$10/month** minimum (2GB RAM, 52GB disk) — smaller plans cause OOM during clip encoding
- OS: Ubuntu 22.04 LTS
- Region: London (lowest latency to YouTube CDN)

### 2. Install dependencies
SSH in as root and run:
```bash
apt-get update -y && apt-get install -y curl git python3 python3-pip ffmpeg openbox wget xvfb x11vnc

# Install Google Chrome (non-snap, required for cookie extraction)
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get install -y ./google-chrome-stable_current_amd64.deb
rm google-chrome-stable_current_amd64.deb

# Install Deno (for yt-dlp JS challenge solver)
curl -fsSL https://deno.land/install.sh | sh -s -- -y
export PATH="/root/.deno/bin:$PATH"
```

### 3. Create runner user
```bash
useradd -m -s /bin/bash runner
echo "runner ALL=(ALL) NOPASSWD:ALL" | tee /etc/sudoers.d/runner
```

### 4. Install GitHub Actions runner
Go to: `https://github.com/JhnsonO/ffa-automations/settings/actions/runners/new?runnerOs=linux`

Copy the token from the `config.sh` command, then run:
```bash
cd /home/runner
curl -o actions-runner-linux-x64-2.317.0.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.317.0/actions-runner-linux-x64-2.317.0.tar.gz
tar xzf actions-runner-linux-x64-2.317.0.tar.gz
rm actions-runner-linux-x64-2.317.0.tar.gz
chown -R runner:runner /home/runner

sudo -u runner ./config.sh \
  --url https://github.com/JhnsonO/ffa-automations \
  --token YOUR_TOKEN_HERE \
  --name "vultr-ffa" \
  --labels "self-hosted,linux,vultr" \
  --unattended --replace

./svc.sh install runner
./svc.sh start
```

### 5. Install yt-dlp for runner user
```bash
mkdir -p /home/runner/.local/bin
curl -sL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /home/runner/.local/bin/yt-dlp
chmod a+rx /home/runner/.local/bin/yt-dlp
chown runner:runner /home/runner/.local/bin/yt-dlp

# Install Deno for runner user too
sudo -u runner bash -c 'curl -fsSL https://deno.land/install.sh | sh -s -- -y'
```

### 6. Set up Chrome for YouTube cookie extraction
```bash
# Start virtual display and VNC (for initial login)
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99
x11vnc -display :99 -passwd ffavnc -forever -bg -quiet
ufw allow 5900/tcp

# Launch Chrome with persistent profile
DISPLAY=:99 openbox &
DISPLAY=:99 google-chrome --no-sandbox --disable-gpu \
  --user-data-dir=/root/.config/chrome-ffa https://youtube.com &
```

Connect via VNC (`IP:5900`, password `ffavnc`) and log into YouTube with `footffa@gmail.com`.

Then extract initial cookies:
```bash
/home/runner/.local/bin/yt-dlp \
  --cookies-from-browser "chrome:/root/.config/chrome-ffa" \
  --cookies /root/yt_cookies_permanent.txt \
  --skip-download https://www.youtube.com/watch?v=VnujPOUYVGg
chmod 644 /root/yt_cookies_permanent.txt
```

Copy profile to runner-accessible location:
```bash
mkdir -p /home/runner/.config
cp -r /root/.config/chrome-ffa /home/runner/.config/chrome-ffa
chown -R runner:runner /home/runner/.config/chrome-ffa
```

### 7. Set up auto-start on reboot
```bash
cat > /etc/rc.local << 'EOF'
#!/bin/bash
Xvfb :99 -screen 0 1280x800x24 &
sleep 2
export DISPLAY=:99
x11vnc -display :99 -passwd ffavnc -forever -bg -quiet
DISPLAY=:99 openbox &
sleep 1
DISPLAY=:99 google-chrome --no-sandbox --disable-gpu \
  --user-data-dir=/root/.config/chrome-ffa https://youtube.com &
exit 0
EOF
chmod +x /etc/rc.local
systemctl enable rc-local
```

Set up cron to keep Chrome profile synced to runner user every 15 mins:
```bash
echo "*/15 * * * * root rsync -a /root/.config/chrome-ffa/ /home/runner/.config/chrome-ffa/ && chown -R runner:runner /home/runner/.config/chrome-ffa/" > /etc/cron.d/sync-chrome-profile
```

### 8. Update workflow files
After setting up the new runner, update all three workflow files to use `runs-on: self-hosted`:
- `.github/workflows/gopro-upload.yml`
- `.github/workflows/sheet-sync.yml`
- `.github/workflows/clip-extractor.yml`

All three should already have `runs-on: self-hosted` — just verify.

---

## Clip Extraction — How It Works

The clip extractor uses `yt-dlp --download-sections` to fetch **only the exact timestamp range** Kris fills in — never the full video. This means:

- A 30-second clip downloads ~30 seconds of video (not 2 hours)
- Processing time per clip is typically 1-3 minutes
- Maximum clip length is capped at **90 seconds** — longer entries are flagged in the sheet

**Cookie handling:** At runtime, the workflow uses `--cookies-from-browser chrome:/home/runner/.config/chrome-ffa` to extract live cookies from the Chrome profile logged into YouTube. This avoids cookie expiry issues since cookies are pulled fresh from the live session on each run.

---

## Sheet Structure

### FFA Clips Sheet tabs
- **Index** — one row per video (Title, YouTube link, Source filename, Date, Tab link, Status)
- **Add Video** — Kris pastes YouTube URLs here for older videos not in the RSS feed
- **Per-video tabs** — hidden once fully processed; visible if they have pending clips or are one of the 3 most recent sessions

### Per-video tab layout
```
Row 1: Title     | <video title>
Row 2: YouTube   | =HYPERLINK("url","▶ Watch")
Row 3: Source    | <GoPro filename or —>
Row 4: (blank)
Row 5: Start | End | Name | Tags | Status | Link
Row 6+: clip rows (filled by Kris)
```

**Status values:** blank = pending, "Processing..." = in progress, "Done" = complete, "Skipped: clip too long" = >90s timestamp

---

## Kris Workflow

1. Watch the session video on YouTube
2. Find the clip you want, note the timestamps (MM:SS format)
3. Go to the FFA Clips Sheet, find the session tab
4. Fill in: Start, End, Name, Tags
5. Wait up to 6 hours — clip appears in Drive: FFA/Clips/\<session\>/
6. Link written back to sheet in the Link column

**For older videos not in the sheet:**
1. Go to the "Add Video" tab
2. Paste the YouTube URL in column A
3. Within 30 minutes a tab will be created
4. Fill in timestamps as normal

---

## Troubleshooting

### Runner not picking up jobs
```bash
systemctl status actions.runner.JhnsonO-ffa-automations.vultr-ffa.service
systemctl restart actions.runner.JhnsonO-ffa-automations.vultr-ffa.service
```
If session conflict error, stop the old instance first, then restart.

### yt-dlp bot detection / cookie errors
The Chrome profile needs to be logged into YouTube. Connect via VNC (`IP:5900`, password `ffavnc`) and check Chrome is open and logged in. If logged out, log back in — cookies will re-sync within 15 minutes via cron.

### Drive upload SSL errors
These are intermittent on GitHub-hosted runners. The self-hosted Vultr runner fixes this permanently. Retries are built into `_upload_to_drive()` (3 attempts, 10s apart).

### GoPro cookies expired
The `check-cookies.yml` workflow runs every Monday and raises a GitHub issue if expired. Refresh via the `refresh.html` tool or manually update the `GOPRO_COOKIES` secret.

### Disk space
Check with `df -h`. The runner work directory should stay clean — tmpdirs are deleted after each clip. If disk fills up: `rm -rf /home/runner/_work/ffa-automations/ffa-automations/tmp*`
