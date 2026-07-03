# FFA 360 / Playcam — AI Project State

**Last reconciled:** 3 July 2026 (run 28684124570 failed at Wait for SSH before reaching render; mux fix still unverified; termination confirmed again)

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

### Active blocker — playcam-poc.yml render step (ffmpeg mux)

**Status: mux fix pushed, UNVERIFIED. Termination fix CONFIRMED SOLID. Not yet redispatched.**

- **Termination/leak: fixed and confirmed.** Both the in-loop offer-cleanup and final "Terminate Vast.ai instance" step use a 3-endpoint fallback (`console.vast.ai/api/v0` → `cloud.vast.ai/api/v0` → `cloud.vast.ai/api/v1`, commit `2422ae2`) — proven working across 3 separate runs today, including on failed dispatches. Do not reduce back to a single endpoint without new evidence; a single-endpoint fix already 404'd once in production.
- **Mux codec: 2 wrong guesses, 3rd fix unverified.** `libx264` (missing from build) → `libopenh264` (ABI/library-version mismatch on instance) → currently `-c:v mpeg4 -q:v 3` (commit `1412735`, built-in encoder, no external `.so` dependency). Attempted isolated validation via `debug-ffmpeg-mux.yml` failed twice on Vast.ai SSH flakiness (unrelated to the mux code) — abandoned per Johnson's direction. **The mpeg4 fix has never actually run.** Next `playcam-poc.yml` dispatch is the real test.
- Offer readiness wait extended 3min→5min; Install dependencies output un-silenced + SSH keepalive added (commits `2ace975`, `59c7e70`) — both untested against a real full run since landing.
- **Open, unaddressed:** 46min/iteration is mostly fixed overhead (~23min install+download) paid every run regardless of outcome. Options (pre-baked image, source caching) not actioned — needs Johnson's go-ahead.

**Latest attempt — run `28684124570` (3 July 2026, dispatched via API, `main`, default 240s window):**

- Offer selection worked as designed: filtered to reliability ≥0.98, tried offer 1 (RTX 5060 Ti, reliability 0.995) which stayed stuck at `status=loading` for the full 5-minute window and was terminated/skipped; offer 2 (RTX 4070S Ti, reliability 0.981) reached `status=running` at attempt 14/30.
- Failed at **Wait for SSH**: instance reported `running` via the Vast API but sshd never accepted a connection across all 18 retries (90s window) — exit code 1, `ERROR: SSH never came up`.
- All downstream steps (install, upload, Phase 1, render, artifact upload) were skipped as a result. **The mpeg4 mux fix still has not run.**
- Termination succeeded cleanly (3-endpoint fallback, `2422ae2`) — no leaked instance.
- This is the same class of failure that killed 2 of 3 `debug-ffmpeg-mux.yml` dispatches: Vast "running" status does not guarantee sshd is actually reachable yet. The existing reliability filter (`reliability gte 0.98`) does not address this — the failed offer had 0.995 reliability, so a higher floor would not have prevented it.

**Next gate:** redispatch `playcam-poc.yml`. If it fails at Wait for SSH again, this becomes a real problem worth fixing directly (e.g. extend the SSH-wait window past 90s the way `debug-ffmpeg-mux.yml` did — commit `fbae177` extended it 90s→5min there) rather than re-guessing. If it clears SSH and render succeeds: check output quality/duration/join integrity, then confirm the instance actually terminated via `vastai-instance-check.yml`.

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
2. Do not scan unrelated subsystems, re-read the repo broadly, narrate progress, or explain internal reasoning.
3. Before editing, state the exact file list in one line.
4. Make the smallest safe patch; do not redesign architecture or cross frozen boundaries without explicit instruction.
5. Run only named/relevant checks. Commit or dispatch only when the task requires it.
6. Final response format only: changed files; what changed; test/workflow run; result/next gate.
7. Prefer ChatGPT or Codex for contained implementation drafts; reserve Claude for live-repo verification, commits, workflow dispatches, and execution-bound debugging.

## Compact change log

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
