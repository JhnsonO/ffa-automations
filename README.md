# FFA Automations

Automation backbone for **Football For All (FFA)**, Leicester. It takes raw GoPro
footage filmed at sessions and gets it onto YouTube and into the clips workflow
with zero manual steps, then helps Kris turn it into shareable clips.

There are three independent pipelines, all driven by GitHub Actions:

| Pipeline | What it does | Entry workflow |
|----------|--------------|----------------|
| **MP4 upload** | Standard GoPro `.MP4` sessions → YouTube (unlisted, branded) | `gopro-scanner` → `gopro-upload` |
| **360 upload** | GoPro MAX `.360` sessions → stitched on a rented GPU → YouTube | `gopro-scanner` → `gopro360-upload` |
| **Clips / Sheet** | Newly-public videos → Google Sheet; pending clips cut + pushed to Drive | `sheet-sync`, `clip-extractor` |

---

## 1. How the upload pipeline works (the core loop)

```
                 ┌──────────────────────────────────────────────┐
                 │  gopro-scanner.yml   (hourly, GitHub runner)  │
                 │  - validates GoPro cookies                    │
                 │  - lists GoPro Cloud media (gopro_uploader.py)│
                 │  - decides which files are "complete"         │
                 │  - writes upload_queue.json + size_check.json │
                 │  - dispatches one upload run per new video    │
                 └───────────────┬───────────────┬──────────────┘
                  .MP4 │                          │ .360
                       ▼                          ▼
        ┌──────────────────────────┐   ┌────────────────────────────────┐
        │ gopro-upload.yml         │   │ gopro360-upload.yml            │
        │ (GitHub runner)          │   │ (GitHub runner orchestrates,   │
        │ - stream GoPro CDN→YT    │   │  Vast.ai GPU does the work)    │
        │ - via gopro_uploader.py  │   │ - rents cheapest GPU instance  │
        └──────────────┬───────────┘   │ - runs gopro360/vastai_stitch.sh│
                       │               │ - stitches dual-fisheye→equirect│
                       │               │ - uploads to YT, commits DB     │
                       │               └──────────────┬─────────────────┘
                       └───────────────┬──────────────┘
                                       ▼
                          uploaded.db committed back to main
```

### Why a scanner + separate upload runs?

GoPro Cloud uploads are slow and a session file can still be growing when the
scanner first sees it. The scanner therefore does **completeness detection**
before queuing anything:

- **≥ 15 GB** → assumed a finished 4K session, queued immediately.
- **< 1 GB** → still recording/uploading, ignored.
- **1–15 GB** → tracked in `size_check.json`; only queued once its size has been
  **stable for 1 hour** (handles shorter / non-4K sessions).

Once a file is "complete" it's added to `upload_queue.json` and the scanner fires
a `workflow_dispatch` for the right uploader, one run per video. `.360` files are
routed to the Vast.ai pipeline; everything else goes to the standard runner.

### Cookie handling (the fragile bit)

GoPro has no public OAuth, so access is via **session cookies** stored in the
`GOPRO_COOKIES` secret. Every workflow writes that secret to `gopro_cookies.json`
at runtime and validates it with a cheap `/media/search` call. If cookies are
dead, the workflow dispatches `cookie-refresh.yml`, which logs in headlessly with
Playwright (`gopro_uploader.refresh_cookies_via_playwright`), then **writes the
fresh cookies back into the `GOPRO_COOKIES` secret** and re-triggers the scanner.
`cookie-refresh` runs daily at 03:00 UTC as a safety net, and `check-cookies`
opens a GitHub Issue every Monday if cookies are dead.

---

## 2. How the 360 pipeline works (Vast.ai)

GoPro MAX `.360` files are dual-fisheye and must be stitched to equirectangular
before YouTube will treat them as normal video. That stitch is GPU/CPU heavy, so
`gopro360-upload.yml` rents a GPU by the hour instead of using a GitHub runner:

1. Resolve the GoPro CDN URL for the `.360` file (`concat` variant preferred).
2. Generate a throwaway SSH keypair for the run.
3. Search Vast.ai for the **cheapest verified, non-CN** offer meeting the spec
   (≥24 cores, ≥64 GB RAM, ≥800 Mbps up/down, Intel, fast clock), skipping known
   weak CPUs. Try the top 5 by price until one boots and SSH comes up.
4. SCP `gopro360/vastai_stitch.sh` to the instance and launch it **detached**
   (`setsid`+`nohup`) so it survives SSH drops; poll a log + `DONE`/`FAILED`
   marker files over short-lived SSH connections.
5. `vastai_stitch.sh` streams the source from the CDN, stitches with FFmpeg
   (NVENC with a `libx264` fallback, maskedmerge seam blend), uploads to YouTube,
   and commits `uploaded.db` back.
6. **Always** terminate the instance at the end (cost control).

Built-in resilience: dud hosts (SSH never comes up) and failed benchmarks trigger
an automatic re-dispatch that excludes the bad offer ID and tries a fresh host.

Reference spec / cost: ~i5-14600KF class, 20 threads, 64 GB RAM, ~850 Mbps+,
~\$0.15/hr.

---

## 3. How the clips / sheet pipeline works

Separate from uploads. Driven by `sheet_manager.py` against the **FFA Clips
Google Sheet** (`1AKJlZ_Ze7rTH-Ve3W_ZObvWOxq5Pige4QOHgLdy0TB8`).

- **`sheet-sync.yml`** (every 30 min, VM): reads the channel's public-video RSS
  feed and creates a tab + Index row per new video (`sync-videos`), and processes
  the "Add Video" request tab (`process-add-video`). No YouTube OAuth needed.
- **`clip-extractor.yml`** (every 6 h, VM): for every **Pending** clip in a tab,
  uses `yt-dlp --download-sections` to fetch just that clip range, re-encodes to
  clean H.264/yuv420p, uploads to Google Drive, and writes the Drive link back to
  the sheet (`process-clips`). Needs Deno (yt-dlp JS challenges) + YouTube cookies.

Sheet layout: an **Index** tab (one row per video) plus one tab per video with a
header block and a clip table from row 6. Kris fills in clip timestamps; the
workflow does the rest.

---

## 4. Workflow reference

| Workflow | Trigger | Runner | Purpose |
|----------|---------|--------|---------|
| `gopro-scanner.yml` | Hourly + manual | GitHub | Detect complete sessions, queue them, dispatch uploaders |
| `gopro-upload.yml` | Dispatched per video + manual | GitHub | Upload one standard `.MP4` session to YouTube |
| `gopro360-upload.yml` | Dispatched per `.360` + manual | GitHub + Vast.ai | Stitch + upload one `.360` session |
| `cookie-refresh.yml` | Daily 03:00 + on cookie failure | **Self-hosted VM** | Playwright login, refresh `GOPRO_COOKIES` secret |
| `check-cookies.yml` | Weekly Mon 09:00 + manual | GitHub | Alert (GitHub Issue) if GoPro cookies are dead |
| `sheet-sync.yml` | Every 30 min + manual | **Self-hosted VM** | Add newly-public videos to the clips sheet |
| `clip-extractor.yml` | Every 6 h + manual | **Self-hosted VM** | Cut Pending clips, upload to Drive, link back |
| `cleanup-and-sort.yml` | Manual only | GitHub | One-off sheet tidy (hide processed tabs, sort Index) |

---

## 5. File inventory

**Core code (do not remove):**
- `gopro_uploader.py` — the heart of the upload pipeline. GoPro Cloud API client,
  cookie/Playwright auth, completeness logic, YouTube + Drive upload, the
  `uploaded.db` model. Imported by all GoPro workflows.
- `sheet_manager.py` — clips/sheet engine. Subcommands: `sync-videos`,
  `process-clips`, `process-add-video`.
- `gopro360/vastai_stitch.sh` — runs on the rented GPU; stitch + upload.
- `cleanup_and_sort.py` — sheet tidy, run by `cleanup-and-sort.yml`.

**Manual / operator utilities (keep):**
- `refresh_cookies.py` — refresh GoPro cookies locally (`python3 refresh_cookies.py`).
- `youtube_reauth.py` — regenerate the YouTube OAuth token (`--manual` mode for phone/Termux).
- `refresh.html` / `yt-cookie-refresh.html` — browser helpers for manual cookie capture.

**State / config (committed on purpose — must stay tracked):**
- `uploaded.db` — SQLite record of every uploaded video + failure cooldowns.
- `upload_queue.json` — videos detected and waiting to upload.
- `size_check.json` — in-flight size-stability tracker for the scanner.
- `.ffa_sheet_id` — FFA Clips Google Sheet ID.
- `.ffa_drive_folder_id` — Drive folder ID for clip/source uploads.

**Experimental / not wired into any workflow:**
- `goal_detector.py` (+ `requirements_goal_detector.txt`) — prototype goal-clip
  detector (motion + GPT-4o vision). Standalone; not part of the live pipeline.

> Dependencies are installed inline per workflow (`pip install ...`); there is no
> top-level `requirements.txt`. Python 3.11 across all runners.

---

## 6. Secrets reference (GitHub → Settings → Secrets → Actions)

| Secret | Used by | What it is |
|--------|---------|------------|
| `GOPRO_COOKIES` | all GoPro workflows | GoPro Cloud session cookies (JSON). Auto-refreshed. |
| `GOPRO_EMAIL` / `GOPRO_PASSWORD` | cookie-refresh, upload | GoPro login for Playwright refresh |
| `GH_PAT` | most workflows | PAT (`repo`,`workflow`) for committing state + dispatching runs |
| `YOUTUBE_CREDENTIALS` | upload, 360, clips | YouTube OAuth client creds |
| `YOUTUBE_TOKEN` | upload, 360, clips | YouTube OAuth token (regen via `youtube_reauth.py`) |
| `YOUTUBE_COOKIES` | clip-extractor | YouTube cookies for yt-dlp |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | sheet/clips workflows | Service account for Sheets + Drive |
| `VASTAI_API_KEY` | gopro360-upload | Vast.ai API key for renting GPUs |
| `ALERT_EMAIL_FROM` / `_PASSWORD` / `_TO` | gopro-upload | Gmail SMTP for the 3-strike failure alert |

> Secrets are **write-only** at rest — they cannot be read back out via the API,
> even with `GH_PAT`. To rotate, overwrite via the GitHub UI or the public-key
> seal flow used in `cookie-refresh.yml`.

---

## 7. Runners

- **GitHub-hosted (`ubuntu-latest`)** — scanner, MP4 upload, 360 orchestration,
  weekly cookie check, sheet cleanup. Stateless; fetch state from `main` each run.
- **Self-hosted VM (`self-hosted`, Vultr `vultr-ffa`)** — cookie refresh (needs a
  persistent browser profile for Playwright), sheet-sync, clip-extractor.
- **Vast.ai (rented per run)** — the actual 360 stitch, launched and torn down by
  `gopro360-upload.yml`.

---

## 8. Replicating this from scratch

1. **Create the repo**, push this code.
2. **Add all secrets** from §6.
3. **YouTube OAuth:** run `python youtube_reauth.py`, paste the result into
   `YOUTUBE_TOKEN`; put the client creds into `YOUTUBE_CREDENTIALS`.
4. **GoPro cookies:** log into GoPro Cloud in Chrome, capture cookies (use
   `refresh.html` as a guide), put them in `GOPRO_COOKIES`. Set
   `GOPRO_EMAIL`/`GOPRO_PASSWORD` so auto-refresh works.
5. **Google:** create a service account, share the Clips sheet + Drive folder
   with it, put the JSON in `GOOGLE_SERVICE_ACCOUNT_JSON`. Set `.ffa_sheet_id`
   and `.ffa_drive_folder_id`.
6. **Vast.ai:** create an account + API key → `VASTAI_API_KEY` (only needed for `.360`).
7. **Register a self-hosted runner** (labelled `self-hosted`) on a small VM with
   Chrome/Playwright for the cookie-refresh, sheet-sync and clip-extractor jobs.
8. Enable Actions. The scanner runs hourly and the whole thing self-drives.

---

## 9. Manual operations

- **Force a cookie refresh:** Actions → *Refresh GoPro Cookies* → Run workflow.
- **Re-auth YouTube:** `python youtube_reauth.py` (or `--manual` on a phone).
- **Upload one specific file by hand:** Actions → *Upload GoPro Session to
  YouTube* → Run with `gopro_filename` (e.g. `GX010130.MP4`) or a `media_id`.
- **Test the 360 pipeline cheaply:** dispatch *Upload GoPro 360 Session* with
  `test_duration_sec` (process N seconds only) or `dry_run=true` (boot + benchmark
  + terminate, no encode/upload).

---

## 10. Known constraints & gotchas

- **Cookies are the #1 failure mode.** Everything downstream dies if GoPro cookies
  expire and the refresh fails. The Mon issue + daily refresh are the safety nets.
- **State lives in git.** `uploaded.db`, `upload_queue.json` and `size_check.json`
  are committed with `[skip ci]`. They must stay tracked or the pipeline loses its
  memory and re-uploads everything. **Do not gitignore them.**
- **Vast.ai instances cost money.** The teardown step is `if: always()`; if a run
  is force-cancelled mid-flight, check the Vast.ai console for orphaned instances.
- **No public GoPro API** is the root reason for the Playwright fragility. Proper
  OAuth access would remove the cookie dance entirely.

---

## 11. Camera labeling tool (Issue #5)

Browser tool for labeling desired virtual-camera yaw on 360° equirectangular
preview clips: click where the camera should point, the tool derives yaw from
the click position and saves the label immediately (no export/import step).
Lives entirely under `labeling_tool/` and never imports `ball_tracker/` or
`playcam/`.

**Run it locally (local-folder clip source, no credentials needed):**

```bash
pip install --break-system-packages -r labeling_tool/requirements.txt
cp your_test_clip.mp4 labeling_tool/clips/
cd labeling_tool && python3 app.py
# open http://localhost:8090
```

**Env vars it reads:**

- `LABELING_TOOL_PORT` — bind port, default `8090`. Always binds `0.0.0.0`.
- `GOOGLE_SERVICE_ACCOUNT_JSON` — same service-account JSON used elsewhere in
  this repo (see `sheet_manager.py`). If unset, the Drive path is skipped
  entirely and only local/cached clips are listed — no error, no crash.
- `FFA_LABELING_DRIVE_FOLDER_ID` — Drive folder id to pull clips from. Falls
  back to a `.ffa_labeling_drive_folder_id` file (same convention as the
  existing `.ffa_drive_folder_id`) if the env var isn't set.

**Point it at a real Drive folder later:** share the Drive folder with the
service account's email (from the JSON key), then set
`FFA_LABELING_DRIVE_FOLDER_ID` (or drop a `.ffa_labeling_drive_folder_id` file
in the repo root) to that folder's id. Matching video files are cached into
`labeling_tool/drive_cache/` on first request and served like local clips —
no code change needed.

**Deploy to the VM:** Actions → *Deploy Labeling Tool* → Run workflow. Installs
the tool into `~/ffa-labeling-tool` on the `self-hosted` runner, starts it
immediately, and installs an `@reboot` entry plus a 5-minute watchdog cron so
it survives reboots and crashes (mirrors *Install Scanner Crontab on VM*).
Existing `clips/`, `labels/` and `drive_cache/` data on the VM is never wiped
by a redeploy. Reaching the bound port from outside the VM (firewall/tunnel)
is a separate infra step, not something this workflow solves. To enable Drive
on the VM without touching the workflow, create `~/ffa-labeling-tool/.env`
by hand with `GOOGLE_SERVICE_ACCOUNT_JSON=...` and
`FFA_LABELING_DRIVE_FOLDER_ID=...` — the wrapper script sources it if present.
