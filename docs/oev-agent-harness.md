# OEV Agent Harness

The harness makes follow-cam experiments a repeatable **edit -> test -> render -> compare** loop without creating a new dispatcher workflow for every experiment.

## What is permanent

- `oev/harness_profiles.json` — canonical test inputs, known-good reference run, FPS/ball class, and human-review windows.
- `scripts/oev_check.sh` — cheap syntax/config/analyzer checks.
- `scripts/oev_dispatch_and_wait.py` — dispatches the proven RunPod baseline workflow and waits for completion. It retries only failures in the RunPod allocation/preflight step; code/build/render failures are not hidden by retries.
- `scripts/oev_analyze_events.py` — deterministic event telemetry and reference comparison.
- `.github/workflows/oev-agent-harness.yml` — one orchestration workflow.
- `oev/harness_request.json` — tiny control file used by agents to trigger a run from the permanent `oev/harness-control` branch.

The proven `.github/workflows/oev-runpod-sample-baseline.yml` remains the GPU execution engine. The harness wraps it instead of duplicating or rewriting its RunPod lifecycle.

## Normal agent loop

1. Make the experiment change on its feature/test branch. Do not touch production Reco merely to run an A/B.
2. Run/verify the cheap harness checks when changing harness code: `bash scripts/oev_check.sh`.
3. Update `oev/harness_request.json` on `oev/harness-control`:
   - `mode`: `run`
   - `profile`: normally `sample_02_180_quality`
   - `experiment_ref`: the branch to test
   - `nonce`: change this every request (timestamp/run label is fine)
4. That single push triggers `OEV — Agent Harness`.
5. The harness dispatches the proven RunPod workflow, automatically redispatches genuine RunPod allocation/preflight failures up to the profile limit, downloads the candidate artifact and the configured known-good reference, and writes a telemetry report to the Actions job summary + `oev-harness-report-<run_id>` artifact.
6. Review the rendered `followcam.mp4` at the profile's human-review windows before product acceptance. Telemetry is diagnostic, not visual acceptance.

## Canonical quality profile

`sample_02_180_quality` currently pins the established comparison setup:

- sample set `GX010197-seed1384188843`
- `sample_02`, 180 seconds
- EU-RO-1
- YOLO26m
- stride-1 baseline workflow path
- lookahead 1.5s
- the established containment-v2 panner overlay
- known-good reference run `32011232949`

The review windows encode the recurring visual failures around 7–14s, 22s, 36–41s, 1:26, 1:36, 1:59–2:09 and 2:14–2:19.

## Metrics generated automatically

- raw detector ball presence
- `world.ball=None`
- Tracking / Bridged / Coasting / Lost counts
- known-ball horizontal out-of-frame rate
- trusted Tracking/Bridged horizontal out-of-frame rate
- large single-frame ball trajectory jumps
- longest horizontal OOF intervals
- per-review-window telemetry
- dormant-ball tracker log counts when those diagnostics exist
- delta vs the configured known-good run

## Triggering manually

`OEV — Agent Harness` also supports `workflow_dispatch`, so a human can choose `run` or `check`, profile, experiment ref and optional reference override from GitHub Actions without editing the control file.
