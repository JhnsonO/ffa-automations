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

## Playcam Phase 3 — Action Zone (gate: 3A/3A.1/3A.2/3A.3 done, diagnostic-only; 3B not started)

**Status:** Design approved (3A only, by Johnson). `playcam/action_zone.py` built and iterated locally across four sub-phases, all verified against the same existing Phase 1 artifact (run `28700918611`, no paid compute, no renderer/ball_tracker/Phase 2.5 changes). Not yet exercised against any other clip.

**Architecture (design doc §B):** two signals per frame — `action_zone_yaw` (raw motion-weighted candidate, always computed and reported even when gated) and `target_yaw = centroid_yaw + clamp(w·confidence·(action_zone_yaw+lead−centroid_yaw), ±15°)` (the bounded value that would actually drive the camera). Hard gates: confidence<0.35 or wide mode → `target_yaw` collapses exactly to `centroid_yaw`.

**Sub-phase results (all against run `28700918611`, 964 samples / 482s):**
- **3A (baseline):** ca>0.5 on 39.3% of frames; longest continuous "counterattack" run 15.0s; cap saturation 21.5%; mean active bias 7.03°.
- **3A.1 (lone-detection candidates made ineligible, not just penalized):** ca>0.5 dropped to 26.2% (purely subtractive — 0 new triggers). Longest run unchanged at 15.0s.
- **3A.2 (sep_growth identity-continuity guard — only credit growth if candidate overlaps ≥50% with prior frame's actual best_sub):** confirmed the 15.0s run's identity does swap mid-run (`84;117`→`126;127`→`115;124;131`, 10/30 zero-overlap transitions) — bug was real, but fixing it changed **0** frames' ca>0.5 status. sep_growth wasn't the actual driver.
- **3A.3 (diagnostic instrumentation, no scoring change):** root cause of over-triggering found. Weighted contribution breakdown across the 253 ca>0.5 frames: coherence 46%, static-main-cluster credit (`1−main_speed_norm`) 37%, subgroup speed only 9% (0/253 frames have sub_speed_norm>0.7), sep_growth 8%. **The scorer is not detecting fast breakaways — it's detecting any 2+ players moving in loosely the same direction while the rest of the pitch is momentarily calm** (main_speed_norm<0.3 in 97% of high-score frames, which is just normal instantaneous football, not a rare event).

**Venue mask verified NOT a blocker for this data:** `--venue-profile` is always passed by `playcam-poc.yml`; `play_location.jsonl` shows `excluded_count>0` on 99.6% of frames (4,586 of 13,500 raw detections filtered), and off-pitch detections are filtered before the `players` field is ever written — structurally impossible for a counterattack-flagged frame to include an off-pitch player.

**Not yet done:** no weight/threshold changes (explicitly held back pending Johnson's call), no 3B grid tuning, no rendering, `action_zone.py` not yet committed to the repo as of this entry (see below — committed this session).

**3B grid test (4 July 2026, still against run `28700918611`, no new dispatch, no commit):** Tested 6 configs (baseline, A1→2.5, A2→0.6, A3→0.3, combo of all three, combo+threshold 0.6). Result: **ca>0.5% is essentially flat (26.2%→26.2%, only 26.0% at threshold 0.6) across every config.** Root cause: `comp_sub_speed_norm` never exceeds **0.366** anywhere in this clip's 964 samples (712/964 frames are exactly 0.0), against a 30°/s norm cap — so no amount of A1/A2/A3 reweighting can make counterattack detection genuinely speed-driven, because **this clip contains no frame with real breakaway speed to reward.** Mean ca score did shift down (0.224→0.195 for the full combo) but the same 253 frames stayed above the 0.5 threshold either way. Cap saturation (~18.6–19.0%) and max bias (15.0°, i.e. always cap-clamped at least once) were also unmoved by these weights — that saturation is driven by confidence×raw-delta magnitude, not by A1/A2/A3. No instability detected (max frame-to-frame `action_zone_yaw` jump ≤14°, no jumps >20° in any config). **Conclusion: 3B weight tuning cannot be validated on this clip — it needs a labeled clip that actually contains a genuine counterattack/breakaway before A1/A2/A3/threshold values can be trusted.** No repo code changed this pass (grid tested in a scratch copy only); `action_zone.py`'s weights are unchanged from the 3A.3 commit.

**Next gate:** Johnson to decide: (a) source/label a clip with a real breakaway before further 3B tuning, since the current clip structurally cannot validate any speed-weighted config, or (b) accept the current weights as-is for now and move on. Do not commit new A1/A2/A3/threshold values based on this clip alone.

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

- **2026-07-04 (Playcam Phase 3 Action Zone):** Design approved for 3A only. Built `playcam/action_zone.py` (committed this session) through 3A→3A.3: lone-detection candidates made ineligible, sep_growth given an identity-continuity guard, and diagnostic instrumentation added. Root cause of over-triggering found: coherence + static-main-cluster terms (83% of positive signal) drive false positives, not subgroup speed (9%) or growth (8%) — scorer currently reads ordinary spaced-out play as "counterattack." Venue mask confirmed active and not a blocker. No weights changed, no 3B tuning started. See Playcam Phase 3 — Action Zone section above for full breakdown.
- **2026-07-04 (redispatch):** Run `28700595596` cancelled (predated Pass 1 speed fix). Orphan check dispatched. Redispatched as `28700918611` (head `c51987d`) — first run with max-frames fix + GPU allowlist + cpu_cores=16 all active.
- **2026-07-04 (speed pass 1):** `playcam-poc.yml` (`dcbff31`) — `cpu_cores` floor 8→16; excluded Blackwell RTX 50-series GPUs (`RTX 5090/5080/5070/5060/5050`) from offer selection, since the pinned `pytorch/pytorch:2.1.0-cuda11.8` image can't use sm_120 and run `28699519056` silently fell back to CPU inference instead of failing. Note: this exclusion is new code, not ported from an existing gopro360 mechanism — checked both `gopro360-upload.yml` and `gopro360-chapter-upload.yml`, neither has GPU-arch filtering. Price/speed ranking + reputation (pass 2) deferred until a clean GPU-matched run gives a timing baseline. Not yet dispatched.
- **2026-07-04 (root cause):** Found real bug — `play_location.py`'s `DEFAULT_MAX_FRAMES=200` silently capped every Phase 1 run to ~100s regardless of input duration; workflow never overrode it. Fixed (`271b701`) by computing `--max-frames` from `duration_seconds`×`sample_fps`. Redispatched full 482.48s clip as run `28700595596`. Prior "blocked on source" conclusion was wrong — retracted.
- **2026-07-04 (correction):** `equirect_full.mp4` and `equirect_trim.mp4` confirmed (checksum/size) to be the same source recording, not distinct clips — explains why 28699519056 matched run #14's transition pattern. Follow→wide→follow validation blocked on source availability, not a pipeline defect.
- **2026-07-04 (final):** Run `28699519056` verified — real duration 99.6s (moov header was misleading at 482s). Single wide→follow transition only; follow→wide→follow not proven on this clip. No snap/lag defects found. **ROOT CAUSE FOUND (retract prior "blocked on source" conclusion):** `equirect_full.mp4`/`equirect_trim.mp4` are indeed the same source recording (checksum/size confirmed), but the real reason both runs stopped at 99.6s is a code defect, not a footage limitation. `playcam/play_location.py` has `DEFAULT_MAX_FRAMES = 200` and the workflow never overrode `--max-frames`. At 2fps sampling that caps every run to the first ~100s of any input, regardless of actual clip/window length — confirmed from run `28699519056`'s own logs: download/trim correctly produced the full 482.48s clip, but Phase 1 logged `sampling 200 frames (every 15 frames, ~2.0 fps effective)` and silently dropped everything after ~100s. This also explains run #14's matching 99.6s/2989-frame output — same cap, not the same window.

**Fix (`271b701`):** Phase 1 step in `playcam-poc.yml` now computes `--max-frames = ceil(duration_seconds * sample_fps) + 50` from the workflow's own inputs, so it covers the full requested window instead of capping at 200.

Run `28700595596` CANCELLED (predated the `dcbff31` GPU/CPU-floor fix — snapshotted at `271b701` only, would still have risked a Blackwell card). `vastai-instance-check.yml` dispatched to confirm no orphan from the cancel. Run `28700918611` (head `c51987d`, has both `271b701` max-frames fix and `dcbff31` GPU allowlist/cpu_cores=16) reported by Johnson as finished 4 July 2026; outcome/metrics NOT reviewed by Claude — Johnson is doing the analysis directly with ChatGPT. **Next gate:** GPU selected, whether the full clip contains a genuine second stoppage/restart, and the follow→wide→follow validation result are all unconfirmed pending Johnson's findings. Do not assume success or failure until reported.
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
