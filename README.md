# FFA GitHub Actions

Two phone-triggered workflows for Football For All.

---

## Workflow 1 — Upload GoPro Sessions to YouTube

Finds new GoPro Cloud videos and uploads them to YouTube.

**How to trigger (phone or desktop):**
1. Go to the **Actions** tab in this repo
2. Click **"Upload GoPro Sessions to YouTube"**
3. Click **"Run workflow"**
4. Optionally change the days lookback (default: 2)
5. Click the green **"Run workflow"** button

Takes ~10-30 mins depending on session length. Check the run logs to see progress.

---

## Workflow 2 — Download YouTube Clip

Downloads a YouTube clip at full quality, saves it as a downloadable artifact.

**How to trigger:**
1. Go to the **Actions** tab
2. Click **"Download YouTube Clip"**
3. Click **"Run workflow"**
4. Paste the YouTube URL
5. Choose quality (default: best)
6. Click **"Run workflow"**

When done (~2-5 mins), click into the run → scroll to **Artifacts** → download `downloaded-clip.zip`. The clip stays available for 3 days.

---

## First-time setup — GitHub Secrets

You need to add 3 secrets so the workflows can authenticate.

Go to: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | What it is |
|---|---|
| `GOPRO_COOKIES` | Contents of `gopro_cookies.json` |
| `YOUTUBE_CREDENTIALS` | Contents of `youtube_credentials.json` |
| `YOUTUBE_TOKEN` | Contents of `youtube_token.json` |

Just open each file in a text editor, copy everything, paste as the secret value.

---

## GitHub Mobile App

Install **GitHub** on your phone → sign in → go to this repo → Actions tab.
You can trigger both workflows directly from your phone in seconds.
