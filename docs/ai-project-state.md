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

**Reframe (4 July 2026) — Action Zone is an action-centre detector, not a counterattack detector:** Johnson corrected the framing after the 3B grid result: the goal is "where is the live football action, right now," not rare breakaway detection. `counterattack_score` is now reported as a secondary `breakaway_score` (optional boost only); a new `action_intensity_score` (density + overall activity + sustained-attention persistence, with switch-of-play and breakaway as small secondary/optional terms) was added in a scratch analysis script — **not yet ported into the committed `action_zone.py`**, pending Johnson's review.

**Analysis run (same artifact `28700918611`, 964 frames, zero paid compute):** compared `action_zone_yaw` against an independent `activity_weighted_yaw` proxy (conf×speed circular mean over all players, deliberately simpler than the production scorer so the comparison isn't circular) — no human visual label exists for this dataset, so this is a numeric proxy, not visual ground truth.

- **Helps** (action_zone_yaw closer to the activity centre than centroid_yaw): 48.2% of frames, mean improvement 11.1°.
- **Hurts**: 34.6% of frames, mean regression 7.1°.
- **Matches** (within 1.5°): 17.1%.
- **Notable pattern in "hurts" frames:** `breakaway_score` is consistently high (0.74–0.92) in the worst-hurting frames, and in several of them `centroid_yaw` was already near-perfect (d_centroid 1–3°) before the breakaway boost pulled `action_zone_yaw` 34–39° away. This is direct evidence that the current breakaway boost (`BETA_C=1.5` in the per-player weighting) can actively degrade an already-good centroid signal — consistent with Johnson's instinct that breakaway should be a minor optional boost, not something that can override a good baseline. **`BETA_C` not changed — flagged for Johnson's decision, not touched without approval.**
- `action_intensity_score` mean was 0.902 but showed little spread on this clip (`density_norm` saturates to 1.0 on ~all frames because this session is a crowded small-sided game) — the density term isn't discriminative on this particular dataset; may behave differently on a less-crowded or wider-pitch clip.

**3B.1 committed (4 July 2026, `b1ea4a9`):** `playcam/action_zone.py` updated — `counterattack_score` renamed to `breakaway_score` (secondary diagnostic, not KPI), `action_intensity_score` added to CSV output (density + activity + persistence, switch/breakaway as minor terms), `BETA_C` reduced 1.5→0.5. Re-run against same artifact `28700918611` (964 frames, zero paid compute, no dispatch):

- Helps: 50.2% (was 48.2%), mean improvement 11.2°.
- Hurts: 31.5% (was 34.6%), mean regression 6.4° (was 7.1°); worst-hurt frame regression also dropped (max ~34.8° vs ~38.8° before).
- Matches: 18.3% (was 17.1%).
- mean|bias| 6.01° (was 6.36°); cap saturation 17.1% (was 18.8%); max frame-to-frame `action_zone_yaw` jump 6.8° with zero jumps >20° (was 13.9–14.0°, also zero >20° jumps).
- mean `breakaway_score` unchanged at 0.224 (now reported as secondary only); mean `action_intensity_score` 0.902–0.922 depending on run, still saturated on `density_norm` for this crowded-session clip.

**Blocker:** none — change verified end-to-end. `action_intensity_score`'s density term remains non-discriminative on this specific clip (small-sided, high player density throughout); a less-crowded clip would be needed to properly stress-test that term. Renderer, `ball_tracker/`, venue mask, and wide/follow FSM untouched.

**Per-term ablation on top-10 worst-hurt frames (4 July 2026, analysis-only, zero paid compute, artifact `28700918611` reused):** confirmed `BETA_C` (breakaway), not `BETA_M` (motion), is the dominant driver of the remaining worst-hurt frames — retracts the earlier uncommitted-scratch hypothesis that motion-weight was the likely real driver. Ablating each weight term on `zone_yaw_raw` for the top 10 worst-hurt frames (regression 23.7–31.8°) gave average signed contribution toward the error: `BETA_C` +4.7°/frame (hurts, consistent direction in 9/10 frames), per-player `conf` +1.4°/frame (mild hurt), `BETA_S` −1.6°/frame (net helps), `BETA_M` −3.1°/frame (helps, consistent direction in 10/10 frames — removing it always made these frames worse). `action_intensity_score` does not feed `action_zone_yaw` at all (separate output field) — 0% contribution by construction. **Conditional `BETA_M` sweep NOT run** — its prerequisite (BETA_M being dominant) was not met.

**3B.2 committed (4 July 2026, `e534695`):** `BETA_C` 0.5→0.0 per Johnson's approval. `breakaway_score` is unchanged as a computed/reported diagnostic field; it no longer multiplies any player's per-frame weight (`c_mult` is now always 1.0). Verified locally against the same artifact (`28700918611`, zero paid compute): script runs clean, 964/964 rows produced, `breakaway_score` still populated (253 frames >0.5) confirming the field is intact and diagnostic-only.

**Blocker cleared:** `wide_safety_camera.py` (`2d8270e`) — added optional `--yaw-source-csv` flag. When omitted, behaviour is byte-for-byte unchanged (verified: same mode-change count and wide-fraction as pre-adapter). When supplied with an `action_zone.py` CSV, `cluster_yaw` is overridden by nearest-timestamp `target_yaw` before mode/FOV/hysteresis logic runs; FSM itself untouched (confirmed identical mode-change count/wide-fraction between default and CSV-override runs on the same input — only the yaw value differs, mean diff 5.3°, max 15.0° matching `BIAS_MAX_DEG`). `ball_tracker/`, venue mask, and FSM logic not touched.

**Next gate:** Johnson to approve a 60–90s render window (segment of `equirect_full.mp4`) for the first A/B pair — baseline (`person_centroid_yaw`, no flag) vs action-zone (`--yaw-source-csv` pointing at a fresh `action_zone.py` CSV run with `BETA_C=0.0`), same FOV/FSM settings both sides. Not yet dispatched — no paid compute spent on this gate.

**3B.4 — aligned 90s-window artifact + CSV (4 July 2026):** `playcam-poc.yml` (`46611f2`) got an additive `skip_render` input (default `false`, unchanged behaviour) so Phase 1 + `wide_safety_timeline.jsonl` could be produced for the exact planned A/B window without triggering any render. Dispatched with `equirect_file_id=1inJzAUL0ho-O6hbm1EKsT_EEBJQLXLLS`, `start=0`, `duration=90`, `sample_fps=2`, `skip_render=true` — run `28718698231` SUCCESS, artifact `8085215720` (0.2MB, no video, confirming skip_render worked). **Incident:** two duplicate dispatches also fired from a `gh.sh` HTTP-code reporting bug (calls that printed "ERROR 400" actually succeeded server-side); run `28718695535` was caught and cancelled at the Vast.ai-launch step (before real compute), orphan-check run `28718721164` dispatched as a safety net. Only `28718698231`'s output is used below.

**Verified alignment:** `play_location.jsonl` 180 frames, t=0.000–89.590s (2.0fps, matches `sample_fps=2`); `wide_safety_timeline.jsonl` 2689 dense frames, t=0.000–89.600s; `metrics_summary.json` duration 89.6s, 1 mode change (wide→follow at t=26.53s). Ran `action_zone.py` (repo `BETA_C=0.0`) directly against this window's `play_location.jsonl` + `wide_safety_timeline.jsonl` (zero paid compute) → `action_zone_comparison.csv`, 180 rows, t=0.0–89.59s, frame 0–2685, mode-change count from the CSV's own mode column matches `metrics_summary.json` exactly (1). `breakaway_score` still diagnostic (59/180 frames >0.5, zero weighting effect per `BETA_C=0.0`). Video file, timestamp base, frame range, venue profile (`st_margarets.json`), and wide_safety_timeline are all confirmed aligned to this single window — no CSV from a different artifact was used.

**Blocker:** none. **Next gate:** Johnson to approve dispatching the actual A/B render pair (baseline vs `--yaw-source-csv action_zone_comparison.csv`) on this exact 0–90s window using `wide_safety_camera.py` (`2d8270e`) directly (not through `playcam-poc.yml`, since Phase 1 output already exists) — cost/runtime for two short renders to be quoted before dispatch.

**3B.5 — action_zone_csv_path wiring (4 July 2026, `3e2e28c`):** `playcam-poc.yml` got an additive `action_zone_csv_path` input (default `''`, unchanged behaviour). When set, "Upload playcam to instance" SCPs that CSV to the instance as `action_zone_comparison.csv`, and "Run Phase 2.5" runs a second `wide_safety_camera.py` pass with `--yaw-source-csv` producing `wide_safety_render_actionzone.mp4` + `wide_safety_timeline_actionzone.jsonl` (guarded so it's skipped whenever `skip_render=true`). Artifact-copy step SCPs both new files back with the existing missing-file-safe fallback pattern. `ball_tracker/`, venue mask, FSM logic, and `action_zone.py` scoring all untouched.

**Verified:** full-workflow YAML parses valid (the `on:`→`True` top-level key is a known PyYAML quirk, not an error); all 10 embedded shell blocks pass `bash -n`; ran the exact new command line locally against the aligned window's `play_location.jsonl` + `action_zone_comparison.csv` — output matches the baseline's mode-change count (1) and wide-fraction (29.6%, matches `metrics_summary.json`), confirming only the yaw source differs and the CSV is correctly consumed.

**Blocker before dispatch:** `action_zone_csv_path` is a path the workflow SCPs *from the repo checkout* — the verified `action_zone_comparison.csv` (generated in 3B.4) is not yet committed anywhere in the repo. Needs a small commit (e.g. `playcam/analysis/action_zone_comparison_28718698231.csv`) before the A/B dispatch can reference it.

**Next gate:** Johnson's go/no-go on (a) committing that CSV to the repo and (b) the A/B render dispatch itself — cost/runtime to be quoted at that point.

**3B.6 — A/B render dispatch #1 FAILED, transient (4 July 2026):** CSV committed (`f890514`, `playcam/analysis/action_zone_comparison_28718698231.csv`, no other files touched). Dispatched `28720296760` with the quoted inputs (`skip_render=false`, `action_zone_csv_path` set) — exactly one run confirmed created (single `204`, verified via `gh.sh latest`). **Failed at "Wait for SSH"** (18/18 retries, instance `43841758` reported `running` but sshd never came up) — matches the same transient host-flakiness pattern seen in run #13 (3 July), not a code defect; the `action_zone_csv_path`/render wiring itself was never exercised. Termination confirmed clean (`console.vast.ai/api/v0`, `{"success": true}`, no leak). **Debug budget: 1 of 3 cycles used.**

**3B.7 — A/B pair PRODUCED, goal missed on screen (4 July 2026):** Johnson re-ran run `28720296760` (same run ID, re-run failed jobs) — now SUCCESS. Artifact `8085881110` (265MB) verified: contains BOTH renders — `wide_safety_render.mp4` (baseline, 132MB) and `wide_safety_render_actionzone.mp4` (133MB) plus both timelines, aligned to the 0–90s window. Timeline comparison (Claude, zero paid compute): identical single mode change (wide→follow at t=26.53s) both sides; smoothed-yaw divergence mean 3.3°, max 15.0° (at t≈80s, matching `BIAS_MAX_DEG`); sustained >8° divergence t≈27.4–81.9s; peak pan rate 25.2°/s at t≈38.7s and t≈43.5s both within cap. **Johnson's adjudication: a goal in the clip was missed on screen** — consistent with the known player-led-camera limit (camera follows cluster centroid, not ball; goal-mouth action with a fast ball can fall outside the 85° follow FOV). **Diagnosis blocked on:** which render(s) missed it and the approximate timestamp of the goal. **3B.8 — missed-goal root cause PROVEN (4 July 2026, analysis-only, zero paid compute, artifact `8085881110`):** Goal located at t≈18.5–19.0s (frames 555–570; tight −44° cluster disperses 10°→25° at t=19.02, restart→follow at 26.53s is the post-goal kickoff). Attack ran t=14.0–18.5s to yaw −45°..−57° (left end). BOTH renders missed it identically — timelines byte-equal until 27.4s, both in wide mode (yaw 0, FOV 100); goal mouth ≈−55° beyond the −50° frame edge (visually confirmed from render frames). Signals in window: `centroid_yaw` correct (−45 by t=16); `action_zone_yaw` wrong by ~50° (+2.6..+5.6 — all-player mean cancelled by static right-side residue tracks 11,12 at +51..+68° plus 14,6; EMA α=0.35 lag compounds); flagged breakaway subgroup was 11;12 (the bystanders) — structural: attack was the MAIN cluster and candidate scoring only evaluates non-main clusters, so an attacking main cluster can never be selected. Binding failure: concentration score 0.288–0.428 (clip global minimum, below 0.45 follow threshold) in exactly this window because the same static players diluted cluster density (clsz 3–5 of 8–9) — FSM held centre-locked wide during the goal; wide gates Action Zone to zero and BIAS_MAX 15° could not cover a 45° error regardless. First frame camera should have moved: 420 (t=14.01, centroid −16→−22, clsz 7→6; unambiguous by frame 435/t=14.51). ~4.5s lead time unused. **Confirmed design limit of player-led wide-safety, not a tuning miss.** **Next gate:** Johnson to arbitrate fix direction (wide-mode framing vs score density definition vs breakaway candidate eligibility) — no weights, thresholds, or code changed this session.

**3B.9 — wide-follow fix (Option 1) implemented, DISPATCHED UNVERIFIED (4 July 2026, `74b8969`):** `playcam/wide_safety_camera.py` only. Wide mode no longer centre-locks `target_yaw` to `venue["wide_yaw"]` (0°) — it now rate-limits a pursuit of the current `cluster_yaw` at 10°/s (`--wide-yaw-max-speed`, new arg), clamped to `venue["wide_yaw"] ± 45°` (`--wide-yaw-range`, new arg). Wide-pursuit state re-syncs to the live yaw whenever follow mode is active, so a follow→wide flip resumes from the camera's actual position, not a stale one. Follow-mode branch, `concentration_score()`, hysteresis thresholds (0.45/0.30/1.5s), the `--yaw-source-csv` Action Zone adapter, venue mask, and `ball_tracker/` are all untouched — confirmed by diff (1 file, 49 insertions/9 deletions) and full `playcam/*.py` compile pass. Unit-level smoke test against the real `28718698231` Phase 1 artifact (t=0–90s window, same data as the missed-goal run) confirms: mode stays "wide" throughout the 0–26.5s stretch (identical mode-change timing to the pre-fix run — hysteresis genuinely untouched), and `target_yaw` reaches −44.6° by t=18.02s (vs. locked 0° before) — with FOV 100° this covers roughly −95° to +5°, which contains the −55°..−57° goal-mouth yaw with margin, before the modelled goal at t≈18.5–19.0s.

**Dispatch:** run `28722245049`, same aligned window as the missed-goal run (`equirect_file_id=1inJzAUL0ho-O6hbm1EKsT_EEBJQLXLLS`, `start=0`, `duration=90`, `sample_fps=2`, `skip_render=false`), no `action_zone_csv_path` (isolated Option-1 test, not an Action Zone A/B). **Note:** `gh.sh dispatch` returned HTTP 400 twice before a manually-constructed identical curl succeeded (204) — confirmed via `gh.sh latest` that the two 400s created no runs (no duplicate/orphan risk this time, unlike the earlier 3B.4 false-negative pattern); `gh.sh`'s dispatch payload construction may have a live bug worth checking in a future session, not yet diagnosed.

**Verified (5 July 2026):** run `28722245049` SUCCESS, artifact `8086248669` (137MB) reviewed. Option 1 behaved exactly as designed on the window it targeted: wide mode pursued the cluster (smoothed_yaw −43.1° at t=18.5s, FOV 100 → frame ≈ −93°..+7°), identical single mode change wide→follow at 26.53s, hysteresis untouched.

**3B.10 — goal STILL missed; 3B.8 goal location was WRONG (5 July 2026):** Johnson's visual adjudication of the new render: goal missed again, and the goal is actually at **t≈60s**, not t≈18.5–19s. The 3B.8 timestamp was inferred from cluster-dispersal patterns in the timeline, not from watching the goal — that inference located the wrong event and is hereby invalidated (dispersal at 18.5s was some other stoppage/contest). Do not locate goals from cluster statistics again; require Johnson's watched timestamp or a ball signal. At t≈60s the camera was in **FOLLOW** mode, smoothed_yaw ≈ −1° to +2°, FOV 85 (frame ≈ ±42.5°); frame extraction at t=59/61s confirms midfield framing with a goal frame clipped at the left edge. `target_yaw` (centroid) sat at −5..+3° through 55–64s — the cluster centroid barely moved during the goal because most players stayed central. Post-event signature: target_yaw spike to +27.7° at t=64.5s (restart drift). **Conclusion: this miss is the core structural limit of centroid-led framing in FOLLOW mode — the ball diverges from the player mass at exactly the moments that matter (shots/goals) and no amount of wide-mode or smoothing tuning addresses it. Option 1 was a correct fix to a real defect (wide centre-lock), but it was not the defect that hid this goal.**

**3B.11 — static-residue fix VERIFIED offline, CONFIG-only, zero paid compute (5 July 2026, `9e5b14a`):** Goal confirmed by Johnson at t≈60s, **left side** (not t≈18.5s as 3B.8 wrongly inferred from cluster stats — that inference method is retired, see above). `playcam/action_zone.py` `BETA_S` 0.6→1.0 (one CONFIG line only, no logic touched, per file's own tuning contract). Diagnosed cause: at 0.6, static high-confidence bystanders (e.g. tracks 34/36/37 at yaw +21..+53°, near-zero roll-mean speed) still retained ~35–45% weight in the circular-mean signal, diluting the correct leftward pull from tracks 10/26/31 (yaw −27..−58°). Verified against the real `play_location.jsonl` from artifact `8086248669`: at BETA_S=1.0, `target_yaw` during the 58–61.5s goal window improves from −2..−9° to −8..−15° (peak −15.0 at t=58.6s), and full-clip stability *improves* (sign-flip count 22→17, i.e. fewer oscillations, not just a stronger pull) with no change to clamp-hit rate or wide-mode gating (checked 14–19.5s still correctly zeroes to `wide_mode_zero_influence`). **Honest limit: does not fully close the gap.** Only one frame (58.6s) crosses the −12.5° threshold needed for an 85°-FOV frame edge to reach the −55° goal mouth; most of the window still falls short by 5–10°. Root constraint is the documented `BIAS_MAX_DEG=±15°` design clamp (design doc §B) combined with a centroid that never moves off ~0° — this fix maximises the signal within that clamp but cannot exceed it. Loosening `BIAS_MAX_DEG` itself is a design-doc change, not a tuning knob, and was not touched.

**Blocker:** none for this commit. Whether to (a) accept this as a partial improvement and stop, (b) pursue a further direction (goal-end bias / leading-edge / ball nudge / raising `BIAS_MAX_DEG` with explicit approval) is a product decision for Johnson. **Next gate:** if Johnson wants visual confirmation, a paid A/B render (baseline centroid vs `--yaw-source-csv` from this fixed `action_zone.py`) on the same 0–90s window — cost/runtime to be quoted before dispatch, not yet run.

**3B.12 — follow-mode attack-edge assist tested offline: FAILED coverage bar; jitter filter PASSED (5 July 2026, analysis-only, zero paid compute, nothing pushed):**

*Edge-assist trigger (switch_of_play + persistent-edge-track, `BETA_C` kept at 0 throughout) — result: does not meet the bar.* Grid-tested across bias-cap/widen-FOV/hold-time combinations, scored against goal coverage + false-trigger time + worst frame jump on the full `play_location.jsonl` from artifact `8086248669`. First-pass thresholds over-triggered badly (67–78s of 90s flagged active, jumps up to 74°). Tightened pass (net-displacement-based edge signal, breakaway dropped as a trigger input — independently reconfirms 3B.2's finding that it's too noisy, firing on 33% of frames) still failed: the only "goal coverage" any candidate produced was 3 frames (57.1/57.6/58.6s) already covered by the 3B.11 `BETA_S` fix alone, zero net-new coverage in the 59–61s window Johnson identifies as the actual goal. Root cause is timing, not tuning: the one legitimate sustained run in the data (track 26, high-confidence monotonic drift) doesn't cross any reasonable net-displacement threshold until t≈61.6s, peaking at t≈63.1s — after the goal, not before/during it. `switch_of_play_score` spikes to 0.803 at t=59.56s but is a lone-frame spike (neighbours 0.406/0.114) — debouncing it away (needed elsewhere to stop nervous camera behaviour) kills the one frame that looked relevant; not debouncing it is equally unusable, since un-debounced ≥0.7 fires 17/180 frames (9%) spread evenly across the whole 90s clip regardless of any goal. **Conclusion: no signal currently in `play_location.jsonl` both fires in time for this goal and stays quiet the rest of the clip. This is not a parameter-tuning gap.**

*Jitter filter — result: works, ready to ship independently.* The +27.7°/+31.9° single-frame spikes (t≈64.5s and 3–4 similar events elsewhere: t=4.5, 38.5, 43.5, 85.6) are all lone-frame detector glitches — confirmed by checking the surrounding frames, each spike sits between two consistent low values. A median-of-3 filter (needs one sample of look-ahead, ~0.5s latency) cleanly suppresses all of them and was checked against the 57–64s goal window itself to confirm it does not flatten genuine signal there (max unintended change 1.2°, mostly <0.3°). This is a real, separate, low-risk fix, decoupled from the failed edge-assist work above.

**Blocker:** the coverage objective (get this specific goal in frame) is not achievable via the tested signals without either an exact re-timestamp of the goal (possible the true shot moment is closer to 61–63s, where track 26's run does build up — current estimate is Johnson's recollection, ±2–3s) or a fundamentally different, untested signal (e.g. acceleration/jerk onset rather than net displacement over a window, which lags by construction). **Next gate:** superseded 5 July 2026 by the Phase 3C plan below (GitHub issues #5–#12). Jitter filter shipping is now issue #11 (awaiting Johnson's go); follow-mode coverage work is parked pending the scorecard/bake-off (#8/#9).

## Playcam Phase 3C — Labeled test set + scorecard plan (adopted 5 July 2026)

**Why:** 3B.8–3B.12 demonstrated that single-clip adjudication drives per-clip tuning. 3B.10 proved centroid-led follow is structurally blind at goals (ball diverges from player mass). Plan approved by Johnson; tracked as GitHub issues (the shared to-do list — check `gh.sh issue list`).

**Decisions (Johnson, 5 July 2026):**

- Product bar: watchable + catches most goals, and must beat a static wide shot.
- Ball tracker → playcam nudge is ALLOWED (confidence-gated bias, never a hijack). The reverse (player evidence setting ball-tracker yaw) remains forbidden per the ball-tracker product invariant.
- A paid labeler will produce virtual-camera labels (timestamp, desired_yaw, desired_fov, confidence, event/action metadata; 1 Hz normal play, 2–4 Hz attacks/transitions).
- **Tuning freeze:** no `action_zone.py` / `wide_safety_camera.py` weight or threshold changes until the scorecard (issue #8) exists. `BETA_C` stays 0.0 permanently.
- Pilot-first: 2 clips (the missed-goal 0–90s window + one never-tuned-on fresh clip) before the wider 4–6 clip set.
- Ball/pose signal prep is paid; only proceeds if the free-signal bake-off misses goals (issue #10 gate).

**Issue map:** #5 labeling tool · #6 preview videos · #7 pilot labels · #8 scorecard script · #9 free-signal bake-off (centroid / Action Zone / static-wide baseline) · #10 paid-signal decision gate (ball nudge, pose) · #11 median-of-3 jitter filter (awaiting Johnson's go) · #12 expand to 4–6 labeled clips.

**Key constraint for #5:** label yaw convention and timestamp base must match `play_location.jsonl` exactly — Claude must verify against real Phase 1 data before labels are trusted.

**Open inputs needed from Johnson:** (a) pick the fresh pilot clip (ideally containing a watched, timestamped goal); (b) go/no-go on #11.

**Infra note (5 July 2026, `6c5905e`):** `scripts/gh.sh` extended with `issue create` / `issue list` subcommands. The known `dispatch` false-error bug (3B.9) remains undiagnosed.

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
