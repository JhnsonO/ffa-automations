## Clip Extractor — new "Clip Errors" tab: cross-tab error index (13 July 2026, merge `835cb9c`)

**Why:** Johnson spotted scattered false-positive errors across tabs (e.g. a 10s clip on the Monday Camera B tab wrongly flagged `Skipped: clip too long (46.4 mins)`) and wanted one place to see every clip currently in an error state instead of hunting tab by tab. This ticket only builds the index — it does NOT fix the underlying false-positive duration/parse bug, which is a separate, not-yet-started ticket.

**Fix @ `c3754f7` (Claude-authored, merged to main via `835cb9c`, feature branch `feat/clip-errors-tab` deleted after merge):** `sheet_manager.py` only, additive.
- New `CLIP_ERRORS_TAB = "Clip Errors"` constant, excluded from the video-tab loop alongside Index/Add Video/Clips Tracker.
- `ensure_clip_errors_tab()` — same create-if-missing pattern as `ensure_clips_tracker_tab()`; header `Video | Row | Start | End | Name | Status`.
- `_reconcile_clip_errors(sheets_svc, spreadsheet_id, tab_names, tab_gids)` — runs at the end of every `process_clips()` pass (1 batchGet across all video tabs' `A6:E` + 1 clear + 1 write). Full overwrite each run rather than incremental, so it always reflects current sheet state as clips get fixed/retried. Any row with Status starting `Error:` or `Skipped:` gets a row here, with a `HYPERLINK("#gid={gid}&range=A{row}", tab_name)` formula that jumps straight to the offending row in its source tab (same `#gid=` pattern the Index tab already uses, extended with `&range=` for a direct row jump).
- `process_clips()` now builds a `tab_gids` map from the same `meta` call it already makes (no extra API cost) and calls the new reconcile function after the Clips Tracker one; completion log line now also reports error-row count.
- Nothing else touched — diff verified clean before merge (additive only, no changes to sheet-writing, Drive, encode, credentials, or bot-check logic).

**Not yet dispatched for verification** — the earlier clip-extractor run (`29288376802`, from the bot-check breaker fix) was still `in_progress` at merge time, so no test dispatch was queued behind it to avoid delaying that run's own verification. This will self-verify on the next natural `process_clips()` run (scheduled or manual) since the reconcile now runs every pass — check the "Clip Errors" tab exists with correct rows/links next time either run is inspected.

**Next:** once a process-clips run completes with this code, confirm the Clip Errors tab was created, contains one row per current Error/Skipped clip, and each row's link correctly jumps to the right tab+row.

## Clip Extractor — bot-check circuit breaker fixed to be run-scoped, not per-tab (13 July 2026, merge `3a9d595`)

**Bug (found in a prior session):** `consecutive_botchecks` was a local variable inside `_process_tab()`, resetting to 0 at the start of every tab. In a real run touching 8 tabs the breaker tripped 8 separate times (5 failures each) instead of once — 44 wasted bot-check hits instead of ~5, defeating the purpose of backing off once IP-throttling is detected.

**Fix @ `f780896` (Claude-authored, merged to main via `3a9d595`, feature branch `fix/botcheck-breaker-run-scoped` deleted after merge):** `sheet_manager.py` only. `process_clips()` now creates one `botcheck_state = {"count": 0, "tripped": False}` dict for the whole run and passes it into every `_process_tab()` call. `_process_tab()` takes `botcheck_state` as a new required param and, as its very first action (before any Sheets reads), returns 0 immediately if `tripped` is already True — so a tripped breaker skips all remaining tabs entirely, with zero further `_fetch_clip_section` calls. Inside the per-clip loop, all `consecutive_botchecks` reads/writes were replaced with `botcheck_state["count"]`/`botcheck_state["tripped"]`; hitting 5 sets `tripped = True` and prints one stop message, then `break`s the current tab loop as before. Untouched per spec: sheet-writing logic, Drive upload logic, `_reencode_clip`, credential handling, `_classify_ytdlp_error`, `MAX_CLIP_SECONDS`, cookies-first logic, sleep/throttle flags, the Processing/Link-empty self-heal pickup.

**Diff verified before merge** via `compare/main...fix/botcheck-breaker-run-scoped`: exactly the expected 6 hunks in `sheet_manager.py`, no other files touched.

**Verification run `29288376802`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/29288376802 — still `in_progress` after 2 polls (poll budget exhausted this session). Head commit `3a9d595`.

**Next:** once the run completes, pull the log and confirm the breaker message ("Stopping early: N consecutive bot-checks...") appears **at most once** across the whole run, and that any tabs after a trip log "Skipping — bot-check circuit breaker already tripped this run" with no clip-fetch attempts. If it never trips this run (i.e. no bot-checks occurred at all, e.g. cookies now valid per the 13 July Chrome fix above), that is inconclusive for this specific fix and does not confirm or deny the run-scoping — would need a run that actually hits bot-checks to fully verify.



## Clip Extractor cookie saga — RESOLVED: Chrome restarted, cookies persisting again (13 July 2026)

**Gate cleared — Johnson gave the go and re-logged in.** The dead root Chrome process was killed and relaunched fresh on Xvfb `:99` with the `chrome-ffa` profile via a new self-healing cron watchdog; Johnson signed into YouTube once via VNC. Cookie persistence is now confirmed working.

**Verification (conclusive):** root's `/root/.config/chrome-ffa/Default/Cookies` is now **36864 bytes, mtime 2026-07-13 15:54:59** (written at Johnson's fresh login) — versus the dead 4096-byte schema-0 shell frozen at 2026-06-28 21:31:58 that caused the two-week outage. The old file was backed up to `Cookies.broken.<ts>` before relaunch. Chrome confirmed alive via remote-debug port (`Chrome/149.0.7827.53` on `http://localhost:9222`).

**Root cause of the failed first relaunch attempt (fixed):** Chrome refuses to run as root without `--no-sandbox` (`zygote_host_impl_linux.cc:101`). The original long-lived process had it; the watchdog now includes `--no-sandbox --disable-dev-shm-usage`.

**Watchdog now installed on the VM (persistent, VM-side state — no repo footprint):**
- Script: `/root/chrome_ffa_watchdog.sh` (relaunches Chrome on `:99`, profile `/root/.config/chrome-ffa`, `--no-sandbox`, remote-debug port 9222, opens youtube.com; no-ops if already running; clears stale `Singleton*` locks; inherits Xvfb `:99` auth if any).
- Root crontab: `* * * * *` + `@reboot sleep 25` → same self-healing pattern as the x11vnc watchdog. If Chrome ever dies again, check `crontab -l` / `/tmp/chrome_ffa_watchdog.cron.log` before rebuilding.
- Xvfb `:99` runs as root: `Xvfb :99 -screen 0 1280x800x24` (untouched since 5 June).
- The remote-debug port also gives a disk-independent way to pull live cookies from browser memory in future (immune to another disk wipe).

**Temp workflows used for this and then deleted** (`chrome-relaunch.yml`, `cookie-check.yml`) — no permanent repo footprint; all persistent state is VM-side (script + crontab).

**Clip-extractor: DISPATCHED — UNVERIFIED** (run `29264421618`, https://github.com/JhnsonO/ffa-automations/actions/runs/29264421618). Working through the ~2-week pending-clip backlog. **Next:** inspect that run + the FFA Clips sheet Status column for real "Done" links (not bot-check text) and confirm the Clips Tracker backfill from `967aad9`. If a fresh login still bot-checks, the Vultr IP itself may need rotating (no evidence for that yet).

**Superseded below:** the 12 July "awaiting go" / "session revoked" / earlier root-cause sections are historical — the disk-cleanup-broke-persistence diagnosis in them is correct, but their "not yet executed / needs manual re-login" status is now done.


## Clip Extractor cookie saga — [SUPERSEDED 13 July, see top section] fix proposed, awaiting go (12 July 2026)

**Definitive finding:** root's live Chrome process (pid alive since 5 June, `/root/.config/chrome-ffa`) has a permanently broken on-disk cookie persistence, NOT a stale-login problem. Evidence: `Safe Browsing Cookies` file in the same Default/ dir updated **today 22:23** (Chrome is alive and actively writing files), but the actual `Cookies` file (site logins) has not changed by a single byte since **2026-06-28 21:31:58** — through multiple fresh VNC logins tonight. Johnson identified the trigger: a Vultr disk-space-clearing pass around that date almost certainly deleted the Cookies file while Chrome held it open; Chrome has been holding a dead/orphaned file handle ever since and can never re-establish persistence without a process restart. No further diagnostic needed — this is conclusive.

**Why every workaround attempted tonight failed:** `9cb3c6a`'s sync step and the pre-existing root rsync cron both correctly copy whatever is in the Cookies file — but the source itself never receives new writes, so both faithfully sync an empty shell forever, regardless of login state.

**Fix proposed to Johnson, NOT YET EXECUTED (needs explicit go, kills the current Chrome session):**
1. Kill the current broken root Chrome process; delete the dead Cookies file.
2. Install a cron watchdog (same self-healing pattern as the x11vnc watchdog already running) that keeps Chrome alive on display `:99` with the `chrome-ffa` profile, plus a remote-debug port — giving a second, disk-independent way to pull live cookies from browser memory in future (immune to another disk wipe).
3. Chrome relaunches fresh on Johnson's VNC screen within ~1 minute; a clean process will persist cookies normally.
4. **Johnson logs in one more time** — last time needed, since persistence will then actually function; `9cb3c6a` sync + existing rsync cron pick it up automatically from then on.

**Immediate next action when resumed:** get Johnson's go, then execute step 1–2 above (new workflow dispatch to kill+relaunch Chrome via cron, mirroring the x11vnc watchdog installation pattern from earlier this session), confirm Chrome comes back up on :99, have Johnson log in once, then dispatch clip-extractor and verify real downloads succeed (Status column shows real "Done" links, not bot-check text) and Clips Tracker backfills per `967aad9`.

**All temporary diagnostic workflows from tonight deleted** (vnc-diagnose, vnc-authcheck, vnc-readonly, vnc-cookiecheck, vnc-postlogin). VNC access itself (x11vnc on :99, cron watchdog, password `58869612`) is confirmed working and self-healing independently of the Chrome cookie issue — that part is fully resolved.


## Clip Extractor / VNC saga — true root cause: cookies wiped during a Vultr disk-cleanup pass on 28 June (12 July 2026)

**Confirmed via direct inspection:** root's live Cookies file (`/root/.config/chrome-ffa/Default/Cookies`) is schema-0/empty, mtime frozen at exactly **2026-06-28 21:31:58**, unchanged even after Johnson's fresh VNC login attempts tonight — the browser LOOKS logged in because the open tab is stale/cached, not because a real session exists on disk. **Johnson identified the actual trigger: a disk-space-clearing pass on the Vultr VM around that date almost certainly deleted/truncated the Cookies file along with genuine cache/log files** (it's indistinguishable from disposable cache data to a generic cleanup). Not a Google-side revocation — this explains the abrupt wipe far better (no partial/invalidated-cookie trace, just gone).

**Also discovered (unrelated, pre-existing):** a root crontab entry already existed (`rsync -a /root/.config/chrome-ffa/ /home/runner/.config/chrome-ffa/ && chown ...`, ~every 15 min) built specifically to solve the same runner-read-permission problem `9cb3c6a`'s sync step addresses — redundant mechanisms now, both harmless to leave in place. Last seen firing 21:15/21:30 on 28 June in journalctl; not confirmed whether it's still active today (not re-checked, not urgent).

**Outstanding action (Johnson, whenever convenient, no rush):** in the VNC session, hard-refresh or open a fresh tab to youtube.com (not the existing stale one) and complete a real sign-in as `footffa@gmail.com`. Once genuine cookies are written, both `9cb3c6a`'s workflow sync step and the pre-existing rsync cron will pick them up automatically — no further manual steps needed after that.

**Recommendation for future VM disk cleanups:** exclude `~/.config/chrome-ffa` (both root's and any synced copies) from cache/disk-space sweeps — it holds live login state, not disposable cache.

**All temporary diagnostic workflows from this investigation deleted** (`vnc-diagnose`, `vnc-authcheck`, `vnc-readonly`, `vnc-cookiecheck`). No permanent repo footprint from tonight's VNC/cookie troubleshooting beyond `9cb3c6a` and the earlier sheet_manager.py fixes.


## VNC password correction (12 July 2026)

**Original password `ffa92762` (mixed letters+digits) was rejected on Johnson's device** — server log confirmed genuine `password check failed` on real connection attempts from his IP, ruling out any plumbing/path bug. Regenerated as all-digit to remove any mobile-keyboard autocapitalization risk. **Current password: `58869612`**, stored at `~/.vnc/passwd` on the VM (mtime 22:01 12 July), confirmed via read-only check to be what the live cron-respawned x11vnc instance is actually using.

**Process note:** two intermediate passwords (`45793261`, then `58869612`) were generated while diagnosing — only `58869612` is current/correct. All temporary diagnostic/mutating workflows used for this (`vnc-diagnose`, `vnc-authcheck`, `vnc-readonly`) have been deleted; no permanent workflow files remain from this VNC troubleshooting, all state lives on the VM (crontab + passwd file).


## VNC access restored + made self-healing (12 July 2026)

**Root cause:** no VNC server process existed on the VM at all (x11vnc/vncserver both absent from `ps aux`, port 5900 not listening) — whatever served it originally likely died with the manual terminal session that started it, since it was never a persistent service. Firewall was NOT the issue (`5900/tcp ALLOW Anywhere` already open); logged UFW blocks were UDP probe packets from Johnson's phone, irrelevant to the actual TCP VNC protocol.

**Fix (VM-side, not a repo code change):** confirmed `Xvfb :99` (the virtual display Chrome renders to) has been running since 5 June, untouched. Started `x11vnc -display :99 -rfbport 5900` pointed at that existing display — does not touch/restart Chrome or the X server. Generated a fresh VNC password (`ffa92762`, stored at `~/.vnc/passwd` on the VM) since none existed on disk. Installed a self-healing cron watchdog (`* * * * *` + `@reboot`, pattern copied from the existing working `ffa-labeling-tunnel` watchdog already in this VM's crontab) that restarts x11vnc if it ever dies again — confirmed working: the first x11vnc instance was killed by the Actions runner's own orphan-process cleanup at job-end (expected, same mechanism that blocked the cloudflared tunnel previously), but the cron-spawned replacement (different PID, spawned outside any job's process tree) survived a subsequent job's cleanup untouched.

**Verified:** port 5900 listening (x11vnc), reachable per Johnson's next message. No workflow files added — all changes are VM-side state (crontab + `~/.vnc/passwd` + `~/x11vnc_watchdog.sh`), done via temporary diagnostic workflows that were deleted after use (no permanent repo footprint).

**Note for future sessions:** if VNC ever stops working again, check `crontab -l` on the VM for the `x11vnc_watchdog.sh` lines before assuming it needs rebuilding — the watchdog should already self-heal; investigate why the watchdog itself died (e.g. VM reboot without the `@reboot` line firing correctly) rather than repeating this whole diagnosis.


## Clip Extractor — corrected diagnosis: session genuinely revoked ~28 June, not just a path bug (12 July 2026, run 29209210035 cancelled by Johnson)

**9cb3c6a's path/sync fix is correct and stays** — verified the sync step runs and copies successfully every time. But the "no valid cookie DB" check still failed after sync, so root's *actual* live file (not the copy) was inspected directly via sudo: it is **itself** a 4096-byte, schema-0, empty SQLite shell, last written **28 June 21:31 UTC** — identical state to the old broken runner-owned copy. No `-wal`/`-shm` sidecars exist either (checked; not a WAL-mode red herring).

**Revised root cause:** the Chrome window has been sitting on youtube.com since 5 June looking alive, but the session itself was invalidated — almost certainly Google revoking it, plausibly triggered by the sustained bot-flagged automated traffic once the outage began. 28 June lines up closely with when the run history's failure streak actually starts. The path/ownership bug (`b3a7d62`/`9cb3c6a`) was real and worth fixing, but is not what caused the two-week outage by itself — the session dying is.

**No further code fix possible here** — a live authenticated browser is required at least once to re-establish a session; this cannot be routed around programmatically. Johnson needs to reopen the existing Chrome window on the Vultr VM (however he originally accessed it — VNC/remote desktop) and log into YouTube again **in that same window/profile** (not a new one). Once live, the `9cb3c6a` sync step picks up fresh cookies automatically on the next scheduled run — no manual export/paste needed, and this was the one-time exception to "built the VM so I wouldn't have to do this," not a recurring requirement.

**Run `29209210035` was cancelled by Johnson mid-run** (still showing bot-check on both Chrome-profile and secret-cookie-file paths — consistent with this diagnosis, secret cookies are stale too).

**Next:** Johnson re-authenticates the VM's Chrome session; then dispatch clip-extractor.yml and verify real downloads succeed + tracker backfills. If a fresh login still gets bot-checked immediately, the VM's IP itself may need rotating — not yet a confirmed issue, no evidence for it either way.


## Clip Extractor — root cause found + fixed: profile path/user mismatch, not credential expiry (12 July 2026, `9cb3c6a`)

**Real root cause (via self-hosted diagnostic dispatch, since removed):** the workflow's `CHROME_PROFILE_PATH` pointed at `/home/runner/.config/chrome-ffa` — an empty, schema-0 SQLite shell (owned by `runner`, never had cookies). The actual live, logged-in-since-5-June Chrome session (still open on youtube.com) runs as `root` at `/root/.config/chrome-ffa`, unreadable by the `runner` user due to path/ownership mismatch — not because cookies expired. This was very likely wrong since initial setup, not a regression.

**Fix @ `9cb3c6a`:** new step in `clip-extractor.yml`, "Sync live YouTube cookie profile", runs before Process pending clips. Confirmed the runner has passwordless sudo (`sudo -n test -r ...` succeeded). Step does `sudo cp` of `Local State` + `Default/Cookies(-journal)` from root's live profile into `$HOME/.cache/yt-chrome-sync`, `chown`s to `runner`, tightens perms. `CHROME_PROFILE_PATH` for the process step repointed at that synced copy. `continue-on-error: true` on the sync step so a sudo/copy hiccup falls back gracefully to the `_chrome_profile_usable()` check already shipped in `b3a7d62` — no change needed in `sheet_manager.py` for this. Runs every 6h, so the copy is always near-live; no manual cookie export/re-login required as long as the root Chrome session stays signed in.

**Run `29209210035`: DISPATCHED — UNVERIFIED** (in_progress at both allowed polls, not failed — likely working through the 2-week backlog of pending clips). https://github.com/JhnsonO/ffa-automations/actions/runs/29209210035

**Next:** check run 29209210035 outcome + sheet Status column (should show real downloads succeeding, not bot-check flags, if this worked) + Clips Tracker backfill from the `967aad9` reconcile pass. If bot-check flags still appear, the root Chrome session itself may have been logged out server-side — that would be the one scenario still requiring a manual re-login via the existing `yt-cookie-refresh.html` helper.


## Clip Extractor — tracker reconcile shipped; first fix run GREEN (12 July 2026, `967aad9`)

**Run `29208205545` (post-fix verification) COMPLETED SUCCESS** — the b3a7d62/8c558e3 fixes executed cleanly end-to-end. Whether clips actually downloaded (cookies working) vs got classified error flags is not yet inspected — check the sheet Status column.

**Clips Tracker root cause:** rows only appended after full clip success (so 2 weeks of nothing was expected), plus a real gap — per-clip append ran AFTER the Done/link writes, so 429 mid-run crashes left Done clips missing from the tracker; numbering used len(col A) and breaks on manual edits.

**Fix @ `967aad9` (Claude-authored, Johnson chose link-match):** per-clip `_append_to_clips_tracker` removed, replaced with `_reconcile_clips_tracker()` at end of every process-clips run — 1 tracker read (FORMULA render) + 1 batchGet across all video tabs; any row with a Drive link absent from the tracker (matched by URL) is backfilled in one append; numbering = max existing # + 1. Special tabs (Add Video, Clips Tracker) now explicitly excluded from processing. Mock-tested: backfill, dedupe vs manual/renamed rows, plain-URL links, numbering.

**Run `29208745479`: DISPATCHED — UNVERIFIED** (reconcile + backfill exercise). https://github.com/JhnsonO/ffa-automations/actions/runs/29208745479

**Next:** inspect run 29208745479 log tail ("Tracker reconcile: N missing row(s) backfilled") + Johnson eyeballs the tracker tab. Cookie-staleness question from previous section still open pending sheet inspection.


## Clip Extractor — failure diagnosis + fix shipped (12 July 2026, `b3a7d62`/`8c558e3`)

**Diagnosis (runs inspected 28 June–12 July):** extractor produced zero clips for ~2 weeks. Every yt-dlp download failed: (1) cookie-less attempt hits YouTube bot-check (Vultr IP flagged); (2) cookie fallback dead — Chrome profile at `/home/runner/.config/chrome-ffa` has unreadable cookie DB (`no such table: meta`), and the workflow's `yt_cookies.txt` was never read by the script (env not passed); (3) poison row `Third_miss` (Thursday 4th June tab) has reversed timestamps 01:01:41→00:01:02, negative duration bypassed the 90s guard, retried every run; (4) accumulated per-tab reads tripped Sheets 60-reads/min quota → unhandled 429 killed runs mid-scan (only difference between red and green runs). Separate 28–30 June phase: checkout EACCES on stale .git refs on VM, self-cleared.

**Fix shipped to main (Claude-authored per Johnson's explicit "no codex" instruction):** `sheet_manager.py` @ `b3a7d62` — classified failure reasons written to Status col with last-try timestamp (bot-check / cookie-profile-unreadable / unavailable / private / no-1080p / rate-limited), `end<=start` validation before download, `_execute_with_backoff` (429 exponential backoff) on all process-clips-path Sheets calls, `_chrome_profile_usable()` sqlite check gates the chrome cookie source. `.github/workflows/clip-extractor.yml` @ `8c558e3` — one line: `YOUTUBE_COOKIES_FILE` env passed to process step (fixes dead cookie fallback). Retry semantics unchanged: rows retry while Link empty, so backlog self-redoes once downloads work.

**Verification run `29208205545`: DISPATCHED — UNVERIFIED** (in_progress at last poll). https://github.com/JhnsonO/ffa-automations/actions/runs/29208205545

**Open risks:** (1) `YOUTUBE_COOKIES` secret may itself be stale — if the run still shows bot-check flags in the sheet, secret needs refresh; (2) Chrome profile on Vultr VM needs manual repair/refresh regardless; (3) `Third_miss` timestamps must be corrected in the sheet by a human — it will now flag "end before start" instead of failing silently.


## Flatcam — lens strength + venue mask RESOLVED (9 July 2026, later still, `5d335a2`/`6d7d3f9`)

**Correction strength confirmed by Johnson: raw (0.0), deferred not final.** `flatcam/lens_profiles.json` MSV profile fixed: `distortion_correction_strength: 0.0`, `calibration_status: "deferred"`. Note: live `main` had drifted to `f90d967`'s `strength=1.0/fov=170`, self-described in its own commit/notes as "visually_tuned" — this was a live contradiction against this state file's own record of Johnson rejecting that render as over-corrected. Resolved in favour of this file's human-verified record; `f90d967`'s self-assessment was wrong. Flag for future sessions: don't trust a commit's own notes over Johnson's actual recorded verdict when they conflict.

**Venue mask written:** `flatcam/venues/st_margarets_msv.json`, the 24-point polygon Johnson approved (raw frame-pixel space, 3840x2160). Since strength=0.0 is a verified true identity map in `undistort.py` (`map_x=xs, map_y=ys` exactly when `s=0`), raw space = undistorted space here, so the approved points were written directly, no transform needed.

**Not yet done:** `render_segment_flat.py` re-run with both fixes — needs real MSV footage (`GX010424 copy.mp4` / `GX010424.MP4`), which is not present in this session's sandbox and was never committed to the repo (local-only per last session's note). Re-source from Drive (`footffa@gmail.com`, id `1xfr5gvMeYtkyVs1DdqU3GROcuVUt6BvQ`) or get a fresh clip from Johnson before this can run — CPU-only local pipeline, no workflow/dispatch needed once footage is available.

**Next gate:** get real footage, run `render_segment_flat.py --profile gopro_max2_msv_4k60 --venue flatcam/venues/st_margarets_msv.json`, visual sign-off from Johnson before calling flatcam done.

## Flatcam — pan-only v1 (9 July 2026, merge `e3b0f296`)

**FOLLOW-mode zoom removed per Johnson's request.** `flatcam/follow_camera_flat.py` FOLLOW mode now uses `self._wide_size()` for crop dimensions (same as WIDE_FALLBACK) instead of a fixed 0.55x zoom — crop size is constant across all modes, only `cx`/`cy` pan. Verified: 3s real-footage re-render (`GX010424`, frames ~128-131s) shows crop_w/crop_h constant at 3840x2160 across all 180 frames, both FOLLOW and WIDE_FALLBACK modes. Zoom deferred to v2, not deleted.

**First real-footage render also verified today** (pre-pan-only): 3s segment, output valid, FOLLOW mode engaged correctly on real footage for the first time.

**Next gate:** Johnson visual sign-off on pan-only render. If approved, flatcam v1 (pan-only, raw lens correction) is done — zoom tuning is a separate future task, not started.

## Flatcam — EDGE_MARGIN locked at 0.80 (10 July 2026, `1d13c2ad`)

**Johnson tested 0.9 / 0.85 / 0.80 / 0.75 renders on real footage and locked 0.80.** Constant crop-in (both modes, still pan-only) to reduce visible lens-edge distortion. Proper distortion correction (undistort calibration) explicitly DEFERRED by Johnson — do not revisit until he raises it. Intermediate commits: 0.9 @ `224373d2`, 0.85 @ `43dc2e88`.

**Flatcam v1 config now locked:** raw lens (strength 0.0), pan-only FSM, EDGE_MARGIN 0.80. All verified on GX010424 real footage (Drive id `1xfr5gvMeYtkyVs1DdqU3GROcuVUt6BvQ`).

## Flatcam — full-clip render workflow built, merged, dispatched (10 July 2026)

`.github/workflows/flatcam-render.yml` added: CPU-only Vast.ai `workflow_dispatch`, Vast lifecycle mechanics copied verbatim from `playcam-poc.yml` (`428ac208`) — only the offer query adapted (no GPU fields; `cpu_cores>=16`, `cpu_ram>=32768MB`, `disk_space>=60`). Drive download reuses the existing YOUTUBE_TOKEN/YOUTUBE_CREDENTIALS oauth-refresh pattern verbatim. Runs `render_segment_flat.py --input source.mp4 --profile gopro_max2_msv_4k60 --venue flatcam/venues/st_margarets_msv.json --output full_render.mp4 --csv-out full_render.csv` on the full downloaded clip (script has no trim flags, so no windowing — matches full-clip requirement). No frozen files touched; diff was a single new file, 288 additions.

**Run 1 (`29073069722`) FAILED** — instance launched fine (AMD EPYC 7502, 64 cores), but `Wait for SSH` timed out after 18 attempts. Cause: launch step used `image: python:3.11-slim` for the CPU-only offer instead of the proven Vast image — that generic image has no `sshd` installed, so Vast's SSH runtype never came up. Fixed @ `c2320fee`: image reverted to the exact proven `pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime` from `playcam-poc.yml` (CPU-only offer query unchanged; the image itself was not "legitimately script-specific" — only the offer query was meant to be adapted, that was the actual mistake).

**Run 2 (`29073246636`) SUCCEEDED** — all steps green, instance terminated cleanly, no leak. Artifact `flatcam-full-render-29073246636` (575.7 MB) uploaded, containing `full_render.mp4` + `full_render.csv` for the full 174s GX010424 clip. Run: https://github.com/JhnsonO/ffa-automations/actions/runs/29073246636

A green run proves execution only, not product quality — the actual gate is Johnson watching the full render.

**Next gate:** Johnson downloads and watches the full render (`flatcam-full-render-29073246636`, run https://github.com/JhnsonO/ffa-automations/actions/runs/29073246636) for: pan smoothness across real play, FSM behaviour during stoppages (WIDE_FALLBACK transitions), whether 0.80 margin holds up pitch-wide. That visual sign-off is the actual product gate.

## Flatcam — full-clip visual sign-off: PASSED WITH TWO OPEN ISSUES (10 July 2026)

**Johnson's verdict, watching the full 174s render: "not bad AT ALL... needs tweaking but pretty good."** Flatcam v1 (raw lens, pan-only, EDGE_MARGIN 0.80) is directionally validated on a full real clip, not yet production-final. Two issues flagged, neither fixed yet — no code touched since `c2320fee`.

**1. Camera lags when the ball goes to the far side.** Diagnosed (not yet confirmed against data): the pipeline tracks MOG2 motion-centroid concentration, not the ball itself — `action_centroid.py` finds where movement mass is clustered, `follow_camera_flat.py`'s FSM (FOLLOW_T 0.45 / WIDE_T 0.30 / HYSTERESIS_S 1.5, untouched defaults) reacts to that score. When the ball outruns the player cluster, the centroid lags behind the actual ball position — this is a structural property of the motion-mass approach, not obviously a threshold bug. **Not yet done:** pull `full_render.csv` (in the run-2 artifact) and correlate mode/score/cx/cy against the far-side moments Johnson noticed, to see whether it's a threshold/hysteresis tuning issue or a deeper approach limitation.

**2. Lens curve/distortion is noticeable.** Expected given `distortion_correction_strength: 0.0` (raw passthrough) on the MSV profile — the profile's own note says revisit only if a real render visibly shows edge warping, which is now the case. Two knobs, not to be conflated:
   - EDGE_MARGIN (crop-in) is already at its tested ceiling — Johnson tried 0.9/0.85/0.80/0.75 and picked 0.80 over 0.75, so "zoom in more" via this knob re-litigates a call already made on real footage.
   - `distortion_correction_strength` is the actual unexplored lever: 0.0 (now, raw) and 1.0 (rejected 9 July as over-corrected/edge-stretching) are the only two points tried. Nothing in between (e.g. 0.3–0.5) has been rendered or judged.

**Handover note:** Johnson wants to interrogate the process itself in the next chat before deciding how to proceed on either issue — treat this as open for discussion, not a green light to pick a correction-strength value or touch FSM constants. No do-not-touch rule has been lifted; Johnson raising the topics unlocks discussion, not unilateral changes.

Plan after both issues are resolved and re-verified:
1. Live-match test — record a real FFA session on Max 2 flat mode, run pipeline end-to-end, judge production quality.
2. Only after that passes: decide flatcam's relationship to playcam/360 pipeline (replace vs complement), revisit dynamic zoom v2.

Do NOT: pick a new distortion_correction_strength value, re-tune FSM constants, or dispatch a new render without Johnson's explicit go. Scope discipline per CLAUDE.md.

## Flatcam — lens distortion comparison stills workflow built, merged, dispatched (10 July 2026)

`.github/workflows/flatcam-stills.yml` + `flatcam/lens_stills.py` added (merge `9ebda929`, feature branch `flatcam-lens-stills`). Addresses issue #2 (lens curve) from the full-clip sign-off: `distortion_correction_strength` has only been tried at 0.0 (current) and 1.0 (rejected 9 July). No full render, no Vast.ai instance — standard GitHub-hosted runner only. Pulls one frame via ffmpeg from the same Drive source (`GX010424 copy.mp4`, id `1xfr5gvMeYtkyVs1DdqU3GROcuVUt6BvQ`, default timestamp `00:01:00`, arbitrary per Johnson — stills don't move through the video so timing doesn't matter), runs it through `undistort_frame()` at six strengths (0.0/0.25/0.4/0.55/0.7/1.0) with the override applied in-memory only, then center-crops each to 3072x1728 (3840x2160 x EDGE_MARGIN 0.80, matching `follow_camera_flat.py`'s production crop). Uploads only the 6 JPGs as the artifact. `flatcam/lens_profiles.json` itself not touched. Diff verified before merge: 2 files added, 0 modified, 150 lines.

**Run `29077900462` SUCCEEDED** — all steps green. Artifact `flatcam-lens-stills-29077900462` (5.85 MB, 6 JPGs) uploaded. Run: https://github.com/JhnsonO/ffa-automations/actions/runs/29077900462.

**SUPERSEDED (10 July 2026, later session):** this stills track was never reviewed and is discarded by Johnson's explicit decision, in favour of the bounded strength-segment visual test below. Do not resurrect the still-frame gate or ask Johnson to review `29077900462`. `lens_profiles.json` remains untouched either way — no do-not-touch rule lifted by this task.

## Flatcam — full calibration/rectilinear-renderer design (10 July 2026) — ARCHIVED AS FALLBACK, NOT ACTIVE

A multi-session design (Claude/ChatGPT/Johnson, static-frame lens calibration + rectilinear FOV renderer for a stronger dewarp than the strength-knob architecture: Gate 0 two-stage stability check (RANSAC homography frame-motion vs non-rigid residual, held-out static features), division-model warp (`k1,k2` bounded, monotonic) jointly fitted with camera pose against measured pitch geometry, full Monte Carlo uncertainty propagation (joint resample of correspondences + survey inputs, 200 resamples), primitive-level held-out validation, locked numeric thresholds (reprojection RMS ≤3.0px, anisotropy floor + directional-bias test, ≥25% held-out coverage), camera/venue file separation, provisional/mount-scoped naming. **Explicitly paused by Johnson as over-scoped for the current decision.** No code written. Kept here only as a reference design if a genuine camera-global rectilinear calibration is ever pursued — do not resume without Johnson explicitly re-opening it.

## Flatcam — strength segment A/B visual test built, merged (10 July 2026, later session, merge `895fcd2`)

**Replaces the stills track and the full calibration design (both above) as the active decision path.** Bounded ticket: does a mid-range `distortion_correction_strength` look better than raw (0.0) on a real followcam segment, judged by eye — no absolute calibration, no yaw, no pose fitting.

Built directly by Claude per Johnson's explicit routing override for this ticket (normally Codex writes, Claude verifies — CLAUDE.md three-AI split unchanged as default; this was a one-off exception due to renderer-boundary nuance).

**Files added, frozen files untouched** (`action_centroid.py`, `follow_camera_flat.py`, `render_segment_flat.py`, `undistort.py`, `lens_profiles.json`, `flatcam/venues/st_margarets_msv.json` all unmodified):
- `flatcam/strength_segment_test.py` — trims a segment (ffmpeg), computes the camera path ONCE by running the unmodified production renderer at the profile's on-disk strength (asserts it's 0.0 — the verified raw-space identity condition — or aborts rather than silently changing semantics); that run's own output is the strength-0.0 baseline. For each additional strength, overrides `distortion_correction_strength` on the loaded profile dict in memory only (never touches `lens_profiles.json` on disk) and replays the same segment.
- Framing method (revised twice this session after review): raw crop box's left/right/top/bottom edge-midpoints are inverted through the exact per-strength mapping (obtained via the public `undistort_frame()`, not a reimplemented formula) — horizontal span and centre are exact by construction. An earlier centre-point local-gradient approximation was tried and rejected: measured up to −64% span error and +219px centre bias under this warp.
- Diagnostics logged per frame, per strength, in `camera_path_s{XXX}.csv`: `v_cover` (vertical scene coverage vs the fixed 16:9 output aspect — verified 0.997–1.004 across strengths 0.1/0.3/0.5, i.e. vertical framing holds); `corner_err_px` (max distance between the raw box's true inverted corners and the assumed rectangle's corners — NOT zero, grows with strength: ~2.2% of crop width at 0.1, ~8.2% at 0.3, ~16.8% at 0.5. This is a genuine geometric property of fitting a rectangle to a non-conformal radial warp, not an implementation bug — centre framing is exact, corner/peripheral content diverges more at higher strengths, consistent with the known edge-stretching behaviour that got strength=1.0 rejected on 9 July. Logged as an inspectable diagnostic; no auto-gate applied).
- Timing: `timings.json` — path-compute (decode+detector+FSM+baseline render, one production-renderer pass) and per-strength replay render logged separately; encode is interleaved with render (VideoWriter), so `encode_s` is null by design rather than restructuring frozen code to split it.
- Output MP4s burned-in top-left strength label so review clips can't be confused.
- `.github/workflows/flatcam-strength-test.yml` — CPU-only Vast.ai `workflow_dispatch`; lifecycle/SSH/deps/Drive-oauth blocks are a verbatim string-substitution build off `flatcam-render.yml` (36 changed lines total: name, inputs incl. `start_sec`/`end_sec`/`strengths`, code-upload list, run command, artifact copy). YAML-validated. Inputs default `start_sec=115`, `end_sec=145`, `strengths=0.0,0.3,0.5` (dispatch this session used `0.0,0.1,0.2,0.3,0.5`).

**Verified locally** (synthetic 4K60-class clip + real St Margaret's venue polygon, not yet on real GX010424 footage): scene target preserved across strengths on extracted frames; inversion residual ≤1.5px; `v_cover` and `corner_err_px` behave as described above at 0.1/0.3/0.5.

**Merged to main:** `895fcd2` (2 files, +620/-0, feature branch `flatcam-strength-segment-test`). Follow-up fix `bb62ec9` (direct to main): `trim_segment` switched from libx264 re-encode to stream-copy — the Vast.ai CPU image's ffmpeg (4.3, conda build) rejects `-preset` outright; copy sidesteps codec options and is faster. Not a frozen-file change.

**Dispatch history:** run `29091492838` failed at SSH wait (infra flake, instance never answered within 90s, clean termination, no leak) → redispatched, run `29091788470` reached the actual script and failed on the `-preset` defect above → fixed, redispatched, run `29102830220` **SUCCEEDED**, all 13 steps green.

**Run `29102830220` — real GX010424 footage, 115–145s segment, strengths 0.0/0.1/0.2/0.3/0.5.** Artifact `flatcam-strength-test-29102830220` (417.3 MB): 5× labelled MP4 (each 29.996s, duration-matched, non-corrupt), 4× per-strength CSV, `timings.json`. Diagnostics transferred from synthetic test as expected: `v_cover` 0.988–1.011 across all strengths (vertical framing holds); `corner_err_px` grows with strength as predicted — max 2.3% of crop width at 0.1, 5.0% at 0.2, 8.2% at 0.3, 16.4% at 0.5. Timing: path-compute (production renderer, one pass, decode+detector+FSM+baseline render) 237s; each replay render ~70–72s; label pass 16s. Total wall time this run ≈ 6–7 min of instance time.

**Next gate:** Johnson watches the 5 renders in `flatcam-strength-test-29102830220` and judges: (1) pitch lines/fences straighter, (2) players look natural, (3) no visible pan acceleration/distortion near edges, (4) runtime acceptable (all four are visual/subjective calls — no numeric threshold applies here). Corner-drift caveat above applies most at 0.3–0.5, worth a deliberate look at frame edges on those two. Verdict decides: pick a strength for `lens_profiles.json` (still frozen, needs explicit approval to touch) vs reopen the archived rectilinear-renderer design vs stay at 0.0.

## Runner failsafe — GitHub-hosted fallback for GoPro pipeline (2 Aug 2026)

**Trigger:** `vultr-ffa` self-hosted runner offline since 28 Jul (second outage in a month, first was ~30 Jun). 18-run Actions backlog. GoPro Cloud Scanner (self-hosted, hourly) is the dispatcher for GoPro Upload (already `ubuntu-latest`, unaffected) — scanner being down is what stalled a week of session uploads, not the uploader itself.

**Merged to main** `afa9ff6c` (feature branch `feature/runner-failsafe`, built directly in this session per explicit routing override, not Codex):
- `gopro-scanner.yml` — added `runner` workflow_dispatch choice input (`self-hosted` default / `ubuntu-latest`), `runs-on: ${{ inputs.runner || 'self-hosted' }}`. +9/-1.
- `runner-failsafe.yml` (new) — hourly cron `15 * * * *`: checks `vultr-ffa` status via API; if offline and no scanner run `in_progress`, cancels queued scanner runs and dispatches scanner with `runner=ubuntu-latest`. Uses existing `GH_PAT` secret, no new secrets.

**Defect found post-merge — NOT WORKING, unresolved:** every `ubuntu-latest` fallback dispatch (manual: run `30718071559`; failsafe-triggered: `30719755839`, `30720767893`) sits in status `pending` with **zero jobs ever created** (`/actions/runs/{id}/jobs` → `total_count: 0`), then gets cancelled by the *next* hourly failsafe pass as "stale queued," which redispatches a fresh one — a self-perpetuating cancel/redispatch loop that never actually scans. Confirmed NOT a billing/capacity issue: repo is public (`private: false`), `actions/permissions` shows `enabled: true, allowed_actions: all`. `pending` (distinct from `queued`) points to a run being blocked pre-job-assignment — likely a repo/org Actions setting (e.g. required approval for `workflow_dispatch`) not visible via REST API with this PAT's scope, or a concurrency-group interaction. **Unconfirmed — needs Settings → Actions → General checked in the GitHub UI**, specifically "Require approval for workflow runs" and the fork/first-time-contributor approval settings, plus whether `gopro-scanner`'s `concurrency: group: gopro-scanner, cancel-in-progress: false` is somehow blocking despite no run showing `in_progress`.

**Current live impact:** `runner-failsafe.yml` is active in production (hourly cron) and is currently looping — cancelling and redispatching a scanner run every hour with no effect. It is not causing damage (no compute cost, no data risk) but is not fixing the backlog either. Backlog as of this session: still 0 successful GoPro Upload runs since 27 Jul 02:01 UTC.

**Not yet started:** Clip Extractor and XbotGo pipelines have no failsafe — assessed non-portable to `ubuntu-latest` (Clip Extractor needs the VM's live Chrome cookie profile; XbotGo needs local SQLite `xbotgo.db`) and were intentionally excluded from this ticket's scope.


## Runner failsafe consolidated (2 Aug 2026) — loop fixed, real blocker found

**Merged to main** `319edac3` (branch `fix/consolidate-runner-fallback`, built directly, not Codex): `gopro-scanner.yml` restructured into two jobs in one file — `check-runner` (always `ubuntu-latest`, checks `vultr-ffa` status via API, outputs the target runner) → `scan` (`needs: check-runner`, `runs-on: ${{ needs.check-runner.outputs.runner }}`). Standalone `runner-failsafe.yml` deleted (logic absorbed). `workflow_dispatch` `runner` input changed to `auto`/`self-hosted`/`ubuntu-latest` (was `self-hosted`/`ubuntu-latest`).

**Confirmed working:** dispatch `30750571938` — both jobs `completed`/`success` on `ubuntu-latest`, no more cancel-loop (previous two-file design self-cancelled every hour, see prior section above — 8+ failed loop iterations before this fix).

**New blocker found, unresolved:** the scan itself failed cleanly with `401 Unauthorized` from GoPro's API — cookies expired (unsurprising after a week with no successful scan to refresh them). Cookie refresh happens via `cookie-refresh.yml`, which is **also `runs-on: self-hosted`** (needs a live browser login, same category of VM-dependency as Clip Extractor's Chrome profile — not portable to GitHub-hosted without storing GoPro login credentials in Actions secrets and building a headless-login flow, which is a materially bigger, credential-sensitive task not yet scoped or approved).

**Net effect:** the GitHub-hosted fallback is now structurally correct, but the GoPro pipeline (scan → upload) cannot fully recover until either (a) `vultr-ffa` comes back online and successfully refreshes cookies, or (b) Johnson explicitly approves scoping a cookie-refresh fallback (bigger, credential-sensitive ticket). Backlog as of this session: still 0 successful GoPro Upload runs since 27 Jul 02:01 UTC.

## OEV — GoPro Cloud → Drive uploader + trim pipeline (6–8 Aug 2026)

**New files, merged to main:**
- `oev_drive_uploader.py` (`3dac978993`, updated `9f1866253e`) — downloads a GoPro Cloud video by `media_id` or exact filename, uploads directly to OEV Drive folder (`18Y8hI_S29BMeg5FEoxlaqoGy1DsQ7GKJ`), skips if same-named file already present. Reuses `gopro_uploader.py`'s auth/download helpers via import, no duplication. No YouTube step, no `uploaded.db`. Downloads to `/mnt/oevdata` when present, falls back to `gu.DOWNLOAD_DIR` otherwise.
- `.github/workflows/oev-drive-upload.yml` (`340c17bb64`, switched to self-hosted `c42ef2d7d9`) — `workflow_dispatch` with `media_id`/`gopro_filename` inputs, `runs-on: [self-hosted, vultr]`. Same GoPro-cookie-validate/refresh-dispatch pattern as `gopro-upload.yml`.
- `oev_trim_clip.py` (`147c1a5694`) + `.github/workflows/oev-trim-clip.yml` (`4f14e6b903`) — pulls a named clip already in the OEV Drive folder, `ffmpeg -ss <offset> -t <duration> -c copy` trim (defaults 10:00 offset / 45s duration), uploads trimmed result to a `Trimmed/` subfolder (auto-created) inside the OEV Drive folder. Runs on `vultr-ffa`, scratch space `/mnt/oevdata`.
- `.github/workflows/cookie-refresh.yml` fix (`9762ddb209`): YouTube-token commit retry changed from flat 3s×5 to exponential backoff (`2**attempt`, matches the proven `uploaded.db` pattern) — was 409-conflicting on every single run since 20 Jun due to high commit volume on `main`. Not yet verified against a live cookie-refresh run.

**Infra:** `vultr-ffa` had only 7.3G free on its 52G root disk (cause not fully diagnosed — `_work`, chrome profile, downloads dirs didn't account for the ~42G used). Attached a pre-existing 40GB Vultr Block Storage volume (region-matched to `vultr-ffa`, Atlanta) instead of debugging further: partitioned (`vdb1`, GPT), `mkfs.ext4`, mounted at `/mnt/oevdata`, `UUID=346edaa2-8b9d-4807-9cba-4d26811adc25` in `/etc/fstab`, owned `runner:runner`. Second identical 40GB volume still sits unattached/spare.

**Context:** this work happened during a live GitHub Actions platform incident (started 6 Aug ~15:22 UTC) — hosted-runner (`ubuntu-latest`) jobs were stuck `queued` with zero jobs assigned. Switching the OEV workflows to the self-hosted `vultr-ffa` runner successfully bypassed it, since self-hosted runner dispatch went through even while hosted-runner assignment was stuck.

**Verified runs — uploads (`oev-drive-upload.yml`):**
- `GX010197.MP4` — Aylestone. First attempt (`31128612979`) stuck in GitHub's hosted-runner queue (platform incident), abandoned. Retry on `vultr-ffa` (`31129084980`) failed — `/mnt/oevdata` was `root`-owned, `runner` user got permission denied. Fixed with `chown -R runner:runner /mnt/oevdata`. Redispatch (`31129133117`) succeeded.
- `GX010198.MP4`, `GX010175.MP4` — Aylestone. Both succeeded first try (`31150601739`, `31150604634`), post permission fix.
- `GX010173.MP4` — Aylestone, the pair to `GX010197`. Never went through this uploader (only `GX010197` was dispatched from that pair, per explicit "try 1 for now"), but was confirmed already present in the OEV Drive folder when the trim workflow ran against it (see below) — uploaded some other way, not tracked here.

**Verified runs — trims (`oev-trim-clip.yml`), all 10:00 offset / 45s duration, all `completed success`:**
`GX010197.MP4` → `31270018554`, `GX010173.MP4` → `31270020775`, `GX010198.MP4` → `31270022821`, `GX010175.MP4` → `31270024820`. Output: `trimmed_GX0101xx.MP4` × 4 in `Trimmed/` subfolder of the OEV Drive folder. **Not yet visually reviewed by Johnson.**

**Known factor, venue-specific (not a general OEV rig issue):** at St Margaret's, one camera faces the sun and the other faces away, causing independent auto-exposure mismatch between the two feeds — this was the likely cause of the earlier weak `reco calibrate` match quality (8–19 features/frame) on that footage. Aylestone reshoots (this session's 4 files) don't have this issue per Johnson. Not yet a formal repo issue; worth filing when someone picks up rig/exposure work, with fixed-exposure (Protune) or preprocessing histogram-matching as candidate fixes.

**Decision this session:** `reco calibrate` (CPU, AKAZE-based, no GPU dependency per repo's own architecture docs) and `reco stitch` (GPU-first, `wgpu` rendering engine) will be run together on Vast.ai rather than splitting calibrate off onto `vultr-ffa`/GitHub-hosted — explicit call to avoid pipeline complexity, even though calibrate alone could run without GPU.

**Superseded below** — see "OEV — reco-cli build attempt on Vast.ai GPU" for what happened when this was actually attempted.

## OEV — reco-cli build attempt on Vast.ai GPU: 3 issues found, blocked on FFmpeg/ffmpeg-next version mismatch (8 Aug 2026)

**New files, merged to main** (feature branch `feature/oev-reco-stitch`, built directly by Claude per explicit routing override, not Codex):
- `oev_reco_stitch_remote.sh` — bash script uploaded to and run on the Vast.ai instance: builds `reco-cli` from source (git clone + `cargo build --release -p reco-cli`), then runs `reco calibrate` + `reco stitch` against a pre-downloaded clip pair. Every stage writes its own log (`env.log`/`build.log`/`calibrate.log`/`stitch.log`).
- `.github/workflows/oev-reco-stitch.yml` — Vast.ai GPU `workflow_dispatch` (`left_clip`/`right_clip` inputs, default `trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`). Lifecycle block (launch-retry/offer-selection/delete_instance) copied verbatim from `playcam-poc.yml` per CLAUDE.md; offer query relaxed vs playcam (`gpu_ram>=4096`, `cpu_cores>=8`, `cpu_ram>=8192`) since reco calibrate/stitch is far lighter than YOLO. Resolves clip file IDs in the OEV Drive `Trimmed/` folder, downloads via aria2c, runs the remote script, pulls back all logs regardless of outcome (uploaded as run artifact `oev-reco-stitch-{run_id}`), uploads `panorama.mp4`+`match.json` to a new `Stitched/` Drive folder only if stitch succeeded.

**Issue 1 — logging was silently lost (fixed, commit `d8c13c9e43`):** original script used `>> file 2>&1` redirects, which don't reliably flush before the SSH session ends — the first run (`31273809221`) failed at the cargo build step but `build.log` only captured one line ("Cloning into..."), the actual compiler error was never written anywhere. Fixed by switching every long-running command to `2>&1 | tee -a file` (exit codes still correct — `pipefail` already set). This fix is validated: subsequent runs captured full build output including real errors.

**Issue 2 — missing FFmpeg dev packages (fixed, commit `f21f1bd496`):** with logging fixed, run `31275779314` showed the real error — `ffmpeg-sys-next` (a `reco-cli` dependency) failed because `libavutil.pc` wasn't found; the `ffmpeg` apt package installs runtime libs only, not the `-dev`/pkg-config files needed to build against. Added `libavutil-dev libavcodec-dev libavformat-dev libswscale-dev libavdevice-dev libavfilter-dev libswresample-dev` to the apt install list — this exactly matches the prerequisite list in reco-project's own README, confirming the fix is correct per upstream docs.

**Issue 3 — BLOCKED, not a config problem: `Pixel::VAAPI` missing from `ffmpeg-next`'s enum (run `31276633754`, unresolved):** with issues 1–2 fixed, the build got much further — all of `wgpu`/`naga`/`reco-core` compiled successfully, then failed compiling `reco-io` with 6 instances of `error[E0599]: no variant or associated item named 'VAAPI' found for enum 'Pixel'` (in `crates/reco-io/src/ffmpeg/encoder.rs` and `hw_upload.rs`). This is a version/API mismatch between what `reco-io`'s VAAPI hardware-encode path expects from the `ffmpeg-next` crate and what's actually available when `ffmpeg-next` is built (via bindgen) against this image's FFmpeg libraries — the base image (`pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime`) ships Ubuntu 20.04's FFmpeg 4.2.7, likely older/different from whatever FFmpeg version reco-project's own CI builds and tests against. Not fixable by installing more packages — this is a genuine toolchain/dependency compatibility wall.

**RESOLVED (8 Aug 2026, fresh chat): base image swap.** Checked `reco-io/Cargo.toml` on `reco-project/video-stitcher` — no feature flag to disable VAAPI; it's unconditional inside the default `ffmpeg` feature (`ffmpeg-next = "8"`), so path 2 (feature flag) is dead. Path 1 confirmed: their CI pins FFmpeg n7.1.4 (Windows) / builds on `ubuntu-latest` (24.04 → FFmpeg 6.1); `AV_PIX_FMT_VAAPI` only became a real enum member (not a `#define` alias) in FFmpeg 5.0, so Ubuntu 20.04 (4.2.7, the old base image) and 22.04 (4.4.2) both fail — 24.04 is the floor.

**Fix @ `e9fda96` (Claude-authored direct build, explicit routing override, merged to main via `4fa3e22`, feature branch `fix/oev-reco-stitch-ubuntu24-image` deleted after merge):** `.github/workflows/oev-reco-stitch.yml` only, single-line change — Vast.ai launch `'image'` field: `pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime` → `nvidia/cuda:12.4.1-devel-ubuntu24.04`. Diff verified against `main` before merge: exactly 1 file, 1 hunk. `oev_reco_stitch_remote.sh` untouched — it already carries the Vulkan packages (`mesa-vulkan-drivers vulkan-tools libvulkan1`) and a `vulkaninfo --summary` line in `env.log`, added in an earlier session; this was mistakenly re-proposed as new work before checking the live file, then corrected.

**Verification run `31277876978`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31277876978 — head commit `4fa3e22`. Inputs: `left_clip=trimmed_GX010197.MP4`, `right_clip=trimmed_GX010173.MP4`.

**Known open risk, not yet evidenced either way:** `reco stitch` renders via `wgpu` (Vulkan), not CUDA. Dropping the PyTorch base image is fine since nothing depends on it, but whether the Vast.ai host's NVIDIA ICD is exposed into a plain CUDA-devel container is unconfirmed — the `vulkaninfo --summary` line in `env.log` will show this on the same run rather than requiring a separate cycle.

**Next:** inspect run `31277876978` — confirm (a) `vulkaninfo` succeeds (Vulkan ICD reachable), (b) `cargo build` completes past the `reco-io` stage that previously hit `Pixel::VAAPI`, (c) `reco calibrate` + `reco stitch` complete against the `GX010197`/`GX010173` pair. Inspect calibrate's match quality before treating stitch output as gate-worthy. If this run is green, run the second pair (`GX010198`+`GX010175`) and Johnson's visual sign-off on the better of the two is the M1 gate.
