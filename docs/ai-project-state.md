# FFA 360 / Playcam — AI Project State

**Last reconciled:** 3 July 2026 (run #12 failed - libopenh264 ABI mismatch + termination 404, both fixed, not yet redispatched)

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

**Status: run #12 COMPLETE (failed at render, new cause); 2 more fixes pushed; NOT yet redispatched — awaiting go-ahead given cost/time so far.**

- `extract_crop_frame()` was previously called but undefined (`NameError`); fixed via `playcam/crop_utils.py` (commits `468f743`, `8b88333`, `cd44f91`). Verified working: Phase 2.5 now processes all 2989 frames with no NameError.
- `set -o pipefail` added to both remote SSH pipes (commit `4c035d4`) — previously a `| tail -60` pipe was swallowing failing exit codes and showing false-green runs.
- ffmpeg mux attempt 1: `-c:v libx264 -preset fast` failed (`Unrecognized option 'preset'`) — build has no `--enable-libx264`/`--enable-gpl`. Switched to `libopenh264` (`54c4081`).
- **Run #12 result (commit `2ace975`, dispatched by Johnson 20:05–20:51 UTC, 46m10s total):**
  - Install deps 15m12s, Drive download 7m38s, Phase 1 12m30s (200/200 samples clean), Phase 2.5 render 8m17s → **failed**.
  - ffmpeg mux attempt 2 (`libopenh264`) also failed: `Incorrect library version loaded` — conda ffmpeg's linked openh264 ABI doesn't match the instance's installed `.so`. Different failure class from attempt 1, not a repeat.
  - **Fixed** (`1412735`): switched mux to `-c:v mpeg4 -q:v 3` — ffmpeg's built-in encoder, no external shared-library dependency, eliminates this failure class entirely.
  - **Instance-leak confirmed and root-caused**: termination step logged `HTTP 404` on both attempts against `cloud.vast.ai/api/v1/instances/{id}/` — the "fix" pushed earlier this session (`2ace975`) was itself wrong; that single endpoint 404s in production. instance `43731958` was left orphaned (later swept by the hourly `vastai-orphan-cleanup.yml` cron, not by this workflow's own termination step). **Fixed** (`2422ae2`): both the in-loop offer-cleanup and the final termination step now try the same 3-endpoint fallback sequence (`console.vast.ai/api/v0` → `cloud.vast.ai/api/v0` → `cloud.vast.ai/api/v1`) already proven working in `vastai-orphan-cleanup.yml` — do not reduce this back to a single endpoint without new evidence.
  - Verified via `vastai-instance-check.yml`: 0 instances live on the account as of this reconciliation.
- Per-offer readiness wait extended 3min → 5min (18→30 attempts, `2ace975`).
- Install dependencies output un-silenced + SSH keepalive added (`59c7e70`).
- **Cost/time concern raised by Johnson**: 46 min per iteration on a 120s test clip is disproportionate; ~23 min of that (install + Drive download) is fixed overhead paid on every run regardless of what's being tested. Not yet addressed — candidate options (pre-baked Docker image to skip cold install, caching the Drive source) are unactioned, need Johnson's go-ahead before implementing.

**Next gate:** await `debug-ffmpeg-mux.yml` result (isolated, cheap CPU-only test of the `mpeg4` mux command before committing to another full ~46min GPU run). First attempt (run `28683324261`) failed at SSH-wait — the offer query was missing the `bad_cpu` exclusion (`E5-`/`E3-`/etc.) that `playcam-poc.yml` already applies; it picked a Xeon E5-2686 that never came up. Fixed (`bcfe43c`) and redispatched. Termination on that failed run worked correctly (`console.vast.ai/api/v0`, confirmed in log) — endpoint fallback fix is holding. Once mux is confirmed working in isolation, redispatch `playcam-poc.yml` for a full run; then check render output quality/duration/join integrity, then confirm instance actually terminates via `vastai-instance-check.yml`.

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
