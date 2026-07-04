# FFA 360 / Playcam — AI Project State

**Last reconciled:** 4 July 2026 (run #14 28684441469 full pipeline SUCCESS; mpeg4 mux confirmed; render was upside-down, orientation fixed in crop_utils.py f0e1b62 — not yet re-rendered; transition ball-loss noted for future tuning)

This is the operational handoff. It records what is evidenced in the repo, what has been visually/technically validated, and the next safe task. Do not infer that a design is complete merely because a prototype exists.

## Start here

1. Read this file and `CLAUDE.md`.
2. Read only files named by the active task.
3. Preserve subsystem boundaries and frozen tracker code.
4. A workflow is `DISPATCHED — UNVERIFIED` until its artifact/output is inspected.
5. Do not claim the Playcam resumable-stage refactor is complete until it is committed and validated end-to-end.

## Current priority — Playcam chunked pipeline resilience

### Status: ARCHITECTURE PROVEN; ORCHESTRATOR REFACTOR REQUIRED

**Active file:** `playcam/chunked_pipeline.py`

Playcam is an independent person/activity-based camera pipeline. It must not import, modify, or be coupled to `ball_tracker/`.

**Current chunk-native architecture:**

```text
Drive source
→ download each chunk with trailing overlap
→ Phase 1 person/activity measurement per chunk
→ shift to global timestamps and trim overlap tails
→ merge one global timeline
→ smooth once across the whole global timeline
→ re-download exact source chunks and render clean chunks
→ concat rendered chunks without video re-encode
```

This architecture is correct: global Phase 2 smoothing happens once after the Phase 1 chunk timelines are merged, so the camera path remains continuous across joins.

### Evidence — validated 3 July 2026

- A manually separated **2-chunk / 40-second** join test completed.
- Final output duration was about **39.6 seconds** at **1920×1080 / 29.97 fps**.
- No visible camera snap or pause at the chunk boundary.
- Velocity remained continuous through the join and did not exceed the configured cap.
- Timeline timestamps were monotonic with zero duplicates.
- Audio had no silence gap or audible glitch in playback.

### Current defect — do not ignore

The committed `chunked_pipeline.py` still chains download, Phase 1, and render loops inside one Python invocation. In the GitHub-runner sandbox, a long chained call can hit the surrounding command timeout; this was observed once as a clean-render truncation to **6.4 seconds instead of 20 seconds**.

The manual two-chunk validation proves the architecture, **not** unattended end-to-end reliability of the current monolithic orchestrator.

### Active item — playcam render orientation + transition tuning

**Status: full pipeline VERIFIED end-to-end (run #14). mpeg4 mux CONFIRMED WORKING. Termination CONFIRMED SOLID. Render was upside-down — orientation bug FIXED in `crop_utils.py` (`f0e1b62`), not yet re-rendered.**

- **Termination/leak: fixed and confirmed.** Both the in-loop offer-cleanup and final "Terminate Vast.ai instance" step use a 3-endpoint fallback (`console.vast.ai/api/v0` → `cloud.vast.ai/api/v0` → `cloud.vast.ai/api/v1`, commit `2422ae2`) — proven working across 3 separate runs today, including on failed dispatches. Do not reduce back to a single endpoint without new evidence; a single-endpoint fix already 404'd once in production.
- **Mux codec: CONFIRMED WORKING (run #14).** `libx264` (missing from build) → `libopenh264` (ABI mismatch) → `-c:v mpeg4 -q:v 3` (commit `1412735`, built-in encoder). Run #14 produced a valid 99.8s `mpeg4` 1920×1080 @29.97fps MP4 (143 MB) — the mpeg4 fix is proven. No further action on the mux.
- Offer readiness wait extended 3min→5min; Install dependencies output un-silenced + SSH keepalive added (commits `2ace975`, `59c7e70`) — both untested against a real full run since landing.
- **Open, unaddressed:** 46min/iteration is mostly fixed overhead (~23min install+download) paid every run regardless of outcome. Options (pre-baked image, source caching) not actioned — needs Johnson's go-ahead.

**Run #14 `28684441469` (3 July 2026, `main`, 240s window) — SUCCESS, full pipeline end-to-end (19m52s):**

- Run #13 (`28684124570`) failed first at Wait for SSH (90s window, offer reliability 0.981 reported `running` before sshd was up). Run #14 was a straight redispatch and cleared SSH in 6s on a different offer — confirms that failure was transient host flakiness, not a code bug. No SSH-wait change was needed.
- All steps green: launch (2m44s) → SSH (6s) → install (1m1s) → upload → download (240s window from Drive) → Phase 1 → Phase 2.5 render → artifacts. Instance terminated cleanly.
- **Render output verified from artifact:** 2989 frames, 99.6s, `mpeg4` 1920×1080 @29.97fps, 143 MB. Mode logic sound — 1 transition, wide for first ~8.5s then follow; wide_mode_fraction 0.086; score mean 0.455.
- **Bug found — render was upside-down.** Root cause: `crop_utils.extract_crop_frame` (the render path, called by `wide_safety_camera.render_wide_safety`) did not negate image-plane vertical, so top row mapped to −up. It is algebraically identical to the known-good `play_location.extract_crop_frame` **except** for that missing sign. Yaw and mode-selection were unaffected (they don't depend on the vertical sign).
- **Fix pushed (`f0e1b62`):** `y = -ys/norm` in `crop_utils.py`. Verified locally **pixel-identical (max_abs_diff=0)** to `play_location`'s crop on a synthetic equirect frame; old output confirmed a pure vertical flip. Smallest-diff, one line + comment. **Not yet re-rendered** — needs a paid rerun to produce a real upright clip.
- **Second, expected issue — ball lost during transitions.** Camera currently follows player-cluster/concentration centroid, not the ball, so during a pan or wide→follow change the small ball can drop out of frame. This is the known limit of a player-led camera, not a failure. Tuning options (faster yaw on follow-entry, shorten wide→follow sustain 1.5s→1.0s, cap lag during transitions, brief wider FOV hold ~92–95 on follow-entry) and a later high-confidence "ball nudge" (small offset, never full hijack) are noted for a future pass — not yet actioned.

**Upright render CONFIRMED:** run `28692555236` (4 July 2026, `1e4fc4f`) succeeded and Johnson visually approved the upright output — the `f0e1b62` orientation fix is validated. Phase 1 + Phase 2.5 upright real-footage POC is passed.

**Next gate — tougher validation window (reliability pass only):** run `28699224749` CANCELLED (wrong source again — MAX2 chapter guess was also wrong). Correct source confirmed by Johnson via direct Drive link: `equirect_full.mp4`, Drive ID `1inJzAUL0ho-O6hbm1EKsT_EEBJQLXLLS` (8.06 GB, 4032×2388 h264, real duration 482.48s ≈ 8m02s — same resolution family as the already-validated source). Run `28699519056` COMPLETE, SUCCESS, artifact `8079929523` reviewed. Real usable duration was 99.6s, not the 482s read from the file's moov header (header duration was stale/wrong — do not trust ffprobe moov-only duration on this source family again; verify by inspecting actual decoded frame count/render output instead). Result: 1 mode change only (wide 0.0s → follow 26.5s), no second wide re-entry — follow→wide→follow NOT demonstrated on this clip. No camera snap; yaw/FOV eases are smooth (max yaw rate ~25°/s, still a clean ease-in/out, checked at t=38.7s). Frames spot-checked at t=24/26/27/28/30s: upright, on-pitch, no obvious ball-loss. Watchable, but this clip does not satisfy the stoppage/restart validation goal — need a longer/different source containing an actual second stoppage to prove follow→wide→follow. Purpose: prove real follow → wide → follow on footage containing stoppages/restarts/spread play, not just wide → follow. On completion report only: all mode-transition timestamps; whether follow→wide→follow occurred; camera snap / yaw lag / FOV issues; ball loss during transitions; overall watchability. No targeting redesign, no threshold/FOV tuning (follow 85 / wide 100 held), no action-centre or ball-guidance, `ball_tracker/` untouched. Confirm instance terminated.

**Keepalive hardening (`995a44d`):** `-o ServerAliveInterval=30 -o ServerAliveCountMax=10 -o TCPKeepAlive=yes` added to all 5 long SSH commands and both SCP commands in `playcam-poc.yml`. Diff vs last-good `1e4fc4f` verified keepalives-only. Note: termination endpoints already console/v0-first with proven cloud/v0→cloud/v1 fallbacks — left intact per the do-not-reduce rule. Full-session single-shot run was considered and rejected (40GB disk + 180-min timeout would kill it); full-session remains chunked-pipeline-refactor territory.

### Required next implementation

Refactor `playcam/chunked_pipeline.py` into resumable named stages:

1. `download_trim`
2. `phase1`
3. `render`
4. `concat`

Each stage must:

- Persist a manifest/state record and its outputs.
- Validate existing outputs before trusting them.
- Skip completed valid work on rerun.
- Be runnable independently so a GitHub/Vast failure resumes from the last completed stage instead of restarting the full job.
- Keep raw source disk use bounded to one chunk where possible.

**Acceptance gate:** run the refactored pipeline end-to-end across at least two chunks, then inspect output duration, join continuity, duplicate timestamps, velocity limits, and audio joins. Do not move to a larger unattended POC until this passes.

### Playcam camera policy — Phase 1 + Phase 2.5

**Phase 1:** `playcam/play_location.py`

- Samples equirectangular footage through four fixed-yaw rectilinear crops.
- Detects people, back-projects to spherical yaw/pitch, de-duplicates crop-seam detections, and chooses a dominant play cluster.
- It is measurement only: no ball tracking, no rendering, no tracker imports.
- July 2026 fixes include pitch-aware preview extraction and corrected vertical back-projection geometry.

**Phase 2.5:** `playcam/wide_safety_camera.py`

- `follow`: pan toward the current person-cluster yaw at the venue follow FOV.
- `wide`: hold a known pitch-centre shot at wider FOV when there is no sustained clear cluster.
- Mode switches use hysteresis; yaw and FOV are both acceleration/speed limited.
- Baseline mode remains available for like-for-like comparison (`--baseline`, always follow).

**St Margarets profile:**

- Follow: yaw `0°`, pitch `+4°`, FOV `85°`.
- Wide fallback: yaw `0°`, FOV `100°`.
- `100°` is a working placeholder, not final venue calibration. Keep pitch fixed at `+4°` unless a venue-specific wide pitch is proven necessary.

**Concentration score calibration (3 July 2026):**

- Score = cluster density × exponential tightness.
- `DISPERSION_SCALE_DEG = 40`.
- Follow threshold = `0.45`; wide threshold = `0.30`; hysteresis = `1.5 s`.
- The older scale was too aggressive: ordinary dense small-sided play could not enter follow mode.
- The current weak/wide threshold is **not validated on real restart or spread-out footage**. The evaluated real clip contained continuous play, not a genuine kickoff, throw-in, goal kick, or stoppage. Do not tune further from that clip alone.

### Playcam execution environment

- `.github/workflows/playcam-poc.yml` launches a reliable GPU-equipped Vast.ai worker, runs Phase 1 then wide-safety render, and uploads output artifacts.
- GitHub-runner POC windows are typically **180–300 seconds**; use the resumable stage design before relying on large chained jobs.
- Production target is Vast.ai. Chunking remains useful there for recovery and throughput even though runner disk limits are less relevant.

## Ball tracker — MOG2-primary track

### Status: MOG2 primary detection is validated; broader full-session decisions are deferred

**Product invariant:** the camera follows only credible fused ball evidence. Player/activity evidence may assist search/recovery but must never directly set ball-tracker camera yaw or pitch.

**Primary detector architecture:**

```text
equirectangular video
→ venue polygon mask
→ MOG2 foreground blobs inside venue
→ one confident blob: MOG2 candidate
→ zero or multiple blobs: existing YOLO fallback
→ temporal/tracklet stages
→ renderer
```

- `ball_tracker/stage1_candidate_gen.py` uses MOG2 as primary.
- A single MOG2 blob becomes `source="mog2"`; zero or multiple blobs fall through to existing YOLO logic.
- `--no-mog2` supports regression checks.
- Run summaries include `mog2_primary_count` and `mog2_fallthrough_count`.
- MOG2 baseline: min circularity `0.50`, var threshold `16`, history `500`.
- Stationary-ball loss is structural background absorption. Correct product response is bridge/hold/widen, not endlessly retuning MOG2 thresholds.

### Stage 2 evidence — verified 29 June 2026

- Run `28355256427`, artifact `7944978610`.
- Input: MOG2-primary Stage 1 artifact `7942126312`, 3,597 frames.
- 376 tracklets; 25 anchors; 96 passing; 255 fragments; zero static-suspect anchors; 16 gaps.
- Johnson adjudicated 20/25 anchors as likely ball versus 2/41 under the prior YOLO-primary run.
- MOG2-primary is a material candidate-quality improvement.

### Deferred tracker decisions

- The largest ambiguous windows remain f162–930 and f1218–2180, where competing blobs dominate.
- Decide later between a full-session render from verified anchors or targeted diagnosis of the long ambiguous gaps.
- Phase 5 gnomonic reprojection for YOLO fallback crops remains deferred.

### Frozen tracker boundaries

- `ball_tracker/run_tracker.py` v11 remains the honest baseline; do not modify casually.
- Stage 1b quarantine, Stage 2 temporal linker, Tier A filter, and accepted renderer behaviour are frozen.
- `ball_tracker/render_segment.py` is visually accepted and frozen unless there is a demonstrated regression.
- No threshold, suppression, follow-cam activation, or camera-path wiring without explicit approval.
- Phase B replay/resolver must not write accepted detections, confirmation semantics, `best_score`, or FSM state. It previously caused renderer oscillation and remains paused until redesigned as a non-confirming camera-target overlay.

## GoPro 360 uploader — MAX2 chapter pipeline

### Status: VERIFIED end-to-end; host-quality monitoring remains open

- MAX2 speed floors were recalibrated to real 8K host performance: preflight `0.55x`, sustained minimum `0.55x`, abort `0.45x`, target `0.90x`.
- Offer selection uses persistent reputation, ranking, preflight benchmarking, sustained-speed checks, and an expanded retry budget.
- The parallel chapter pipeline stitches individual raw GoPro chapters, uploads them to Drive Inbox, stream-concats in order, then re-injects 360 XMP spherical metadata.
- End-to-end session `0419` was verified on 1–2 July 2026: chapter jobs succeeded and concat/upload run `28551023163` succeeded.
- Open: re-measure `offers_exhausted` after pool expansion plus MAX2 floor recalibration; reputation storage is last-write-wins at current volume.

## Security gate — urgent

Do not store OAuth refresh tokens, client secrets, API keys, or similar credentials in repository source files. A credential is currently embedded in `playcam/chunked_pipeline.py`; rotate/revoke it and move replacement access to GitHub Actions Secrets or another secret store before treating the public repository as safe. Do not paste the credential into logs, issues, docs, or chat.

## Claude operating mode — low-token execution

Claude is a bounded executor/reviewer, not a general repo-exploration agent.

1. Read this file and `CLAUDE.md`, then only files directly required by the active task.
0. Use `scripts/gh.sh` (needs `GH_PAT` env) for all GitHub API operations — reads, pushes, dispatch, run status, filtered failure logs, artifacts. Do not re-implement API boilerplate.
2. Do not scan unrelated subsystems, re-read the repo broadly, narrate progress, or explain internal reasoning.
3. Before editing, state the exact file list in one line.
4. Make the smallest safe patch; do not redesign architecture or cross frozen boundaries without explicit instruction.
5. Run only named/relevant checks. Commit or dispatch only when the task requires it.
6. Final response format only: changed files; what changed; test/workflow run; result/next gate.
7. Prefer ChatGPT or Codex for contained implementation drafts; reserve Claude for live-repo verification, commits, workflow dispatches, and execution-bound debugging.

## Compact change log

- **2026-07-04 (correction):** `equirect_full.mp4` and `equirect_trim.mp4` confirmed (checksum/size) to be the same source recording, not distinct clips — explains why 28699519056 matched run #14's transition pattern. Follow→wide→follow validation blocked on source availability, not a pipeline defect.
- **2026-07-04 (final):** Run `28699519056` verified — real duration 99.6s (moov header was misleading at 482s). Single wide→follow transition only; follow→wide→follow not proven on this clip. No snap/lag defects found. Confirmed via Drive checksum/size compare: `equirect_full.mp4` (`1inJzAUL0ho-O6hbm1EKsT_EEBJQLXLLS`, 8.06GB) and `equirect_trim.mp4` (`1z2p2FgLsjgvIIBw0HZXWEenckMLWpVNX`, 2.07GB, the old workflow default) are the same source recording (created 18s apart, `_trim` is a cut of `_full`) — not two different sessions. This is why 28699519056 reproduced the same single-transition pattern as run #14. BLOCKER: neither file contains a second stoppage/restart; no currently-available Drive source satisfies the follow→wide→follow validation goal. Next gate: obtain/identify a genuinely different clip or timestamp with a real second stoppage before dispatching again.
- **2026-07-04 (later still x2):** Run `28699224749` (MAX2 chapter guess) cancelled — also wrong source. Johnson supplied the correct Drive link directly: `equirect_full.mp4` (`1inJzAUL0ho-O6hbm1EKsT_EEBJQLXLLS`, 8.06GB, 4032x2388, 482.48s real duration, verified via local ffprobe on moov header — not by trusting Drive API metadata, which returned zeros for this file). Dispatched full-clip run `28699519056` — DISPATCHED, UNVERIFIED.
- **2026-07-04 (later still):** Run `28699134976` cancelled — wrong source; per past-chat evidence the intended source is converted chapter `0421_ch01.mp4` (Drive `10D_Zhntym2rO00aISMsMYU7m5CRtRURU`, 8K MAX2, 18.2 min). Redispatched entire chapter as run `28699224749` — DISPATCHED, UNVERIFIED. Orphan check dispatched.
- **2026-07-04 (later):** Upright render confirmed via run `28692555236` — POC passed. SSH/SCP keepalives added to `playcam-poc.yml` (`995a44d`, diff vs last-good verified). Dispatched tougher 20-min validation window run `28699134976` (start=600s, dur=1200s) — DISPATCHED, UNVERIFIED. Full-clip single-shot rejected (disk/timeout limits).

- **2026-07-04:** Repo-ops infrastructure added: `scripts/gh.sh` (GitHub API helper — get/push/dispatch/latest/run/logs/artifacts; token from `GH_PAT` env, read-only subcommands live-tested), `docs/ai-usage-protocol.md` (Johnson's two-AI working instruction). `CLAUDE.md` extended with Repo operations, Debug budget (max 3 cycles/chat), and ChatGPT handoff contract sections. No pipeline code touched; playcam gate unchanged. Observed: `playcam-poc.yml` run `28692555236` in progress (user-dispatched, presumed upright re-render) — not verified this session.

- **2026-07-03:** Run #14 `28684441469` — full playcam-poc pipeline SUCCESS (19m52s). mpeg4 mux confirmed (valid 99.6s 1920×1080 MP4). Render was upside-down: fixed in `crop_utils.py` (`f0e1b62`, `y=-ys/norm`), verified pixel-identical to known-good `play_location.extract_crop_frame`. Not yet re-rendered. Transition ball-loss noted (player-led camera limit) for future tuning.
- **2026-07-03:** Run `28684124570` dispatched (defaults) — failed at "Wait for SSH" (90s window, 18 retries) after the selected instance (RTX 4070S Ti, reliability 0.981) reported `running` via API but sshd never came up. First offer tried (reliability 0.995) never left `status=loading` in 5min and was skipped. Termination succeeded, no leak. Mux fix still unverified — never reached the render step.
- **2026-07-03:** Run #12 completed (46m10s, failed at render). Termination endpoint from prior fix 404'd in production (instance 43731958 leaked, later swept by hourly orphan-cleanup cron) — replaced with the 3-endpoint fallback proven in `vastai-orphan-cleanup.yml` (`2422ae2`). libopenh264 hit an ABI/library-version mismatch on the instance — switched mux to built-in `mpeg4` (`1412735`). Cost/time flagged: ~23min of the 46min run is fixed install+download overhead per iteration, unaddressed.

- **2026-07-03:** Fixed instance-leak (wrong termination endpoint console/v0 -> cloud/v1), extended offer-readiness timeout 3min->5min, un-silenced Install dependencies output + added SSH keepalive so stalls are detectable. Commits `2ace975`, `59c7e70`. Verified 0 live instances on account after leak fix.
- **2026-07-03:** Root-caused ffmpeg mux failure to missing libx264/gpl in the Vast.ai ffmpeg build; pushed fix (commit `54c4081`) switching `render_wide_safety()` to `libopenh264`; redispatched `playcam-poc.yml` — DISPATCHED, UNVERIFIED.
- **2026-07-03:** Recorded active blocker: playcam-poc.yml render step fails on ffmpeg `-preset` (ffmpeg 4.3 on Vast.ai image), confirmed across 5 consecutive runs; crop_utils.py and pipefail fixes verified working upstream of it. Not yet fixed as of HEAD `56ddd4e`.
- **2026-07-03:** State reconciled. Added Playcam as the active priority; recorded validated two-chunk architecture, the monolithic chaining timeout/truncation defect, and the required resumable named-stage refactor. Recorded live camera-mode calibration limits and security gate.
- **2026-07-03:** Claude token discipline added: scoped reads, no broad repo scans/narration, minimal patching, compact final reports.
- **2026-07-02 to 2026-07-03:** Playcam Phase 1 geometry fixes, Phase 2.5 wide-safety camera, venue profile, concentration calibration, and two-chunk join validation added.
- **2026-07-01:** GoPro MAX2 floor recalibration, robust instance termination, and parallel chapter stitching/concat with 360 metadata re-injection verified.
- **2026-06-29:** MOG2 wired as Stage 1 primary detector and Stage 2 validation demonstrated a major improvement over YOLO-primary anchors.
- **2026-06-25:** Renderer local-wide fallback and 20-frame reacquisition blend visually accepted; replay-based confirmation mutation rejected and frozen out.
