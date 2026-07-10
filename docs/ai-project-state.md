
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

## Flatcam — NEXT: full-clip validation render (active gate)

Sandbox renders were 3s segments only (CPU too slow for full clip, ~1.2s/frame at 4K). Next step is a full-length render of GX010424 (174s) on proper compute, then Johnson watches the whole thing for: pan smoothness across real play sequences, FSM behaviour during stoppages (WIDE_FALLBACK transitions), and whether 0.80 margin holds up across the full pitch range.

Plan (execute in order, each gated on the previous):
1. **Full render of GX010424** — needs a workflow or Vast.ai run (CPU render at ~1.2s/frame = ~3.5h sandbox, unacceptable; GPU box or beefier CPU runner required). Estimate cost before dispatch. Claude drafts Codex prompt for a `flatcam-render.yml` workflow_dispatch (inputs: Drive file id, profile, venue, output artifact) mirroring the existing render workflow's Drive-download/artifact-upload patterns. Frozen: everything except the new YAML.
2. **Johnson full-clip sign-off** — visual gate, no code.
3. **Live-match test** — record a real FFA session on Max 2 flat mode, run pipeline end-to-end, judge production quality.
4. **Only after 3 passes:** decide flatcam's relationship to playcam/360 pipeline (replace vs complement), revisit deferred items (undistort calibration, dynamic zoom v2).

Do NOT: touch undistort/lens strength, re-tune FSM constants, add features beyond the render workflow. Scope discipline per CLAUDE.md.
