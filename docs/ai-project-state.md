
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

**Merged to main:** `895fcd2` (2 files, +620/-0, feature branch `flatcam-strength-segment-test`). Dispatched: see next line for run ID once available.

**Next gate:** Johnson watches the 5 labelled segment renders (0.0/0.1/0.2/0.3/0.5) on real GX010424 footage and judges: (1) pitch lines/fences straighter, (2) players look natural, (3) no visible pan acceleration/distortion near edges, (4) runtime acceptable. Corner-diagonal caveat above applies most at 0.3–0.5. That verdict decides whether the strength knob gets a new value or the rectilinear-renderer direction (archived above) gets reopened — no code change without that decision.
