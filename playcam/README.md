# playcam chunked pipeline

Chunk-native orchestrator for `play_location.py` (Phase 1) + `smooth_camera_path.py`
(Phase 2). Built to work around GitHub-runner disk/timeout limits during
development -- on Vast.ai (the intended production target) chunking is still
useful for reliability and GPU throughput, just not required by disk limits.

## Usage

```
python3 playcam/chunked_pipeline.py \
    --file-id <drive_file_id> \
    --start 30 --total-duration 90 --chunk-duration 20 --overlap 3 \
    --venue-profile playcam/venue_profiles/st_margarets.json \
    --output playcam/output/full_render.mp4
```

## How it works

1. Download each chunk with a trailing `--overlap` (default 3s) so Phase 1
   track association has context right up to the boundary.
2. Run Phase 1 per chunk, shift timestamps to global time, trim the overlap
   tail before merging (no duplicate timestamps at joins).
3. Merge all chunks into one continuous global `play_location.jsonl`.
4. Run Phase 2 smoothing **once** over the full merged timeline -- this is
   what makes joins smooth; the kinematic ease doesn't know the source was
   chunked.
5. Re-download each chunk (exact span, no overlap) and render it clean
   against the global camera timeline.
6. Concatenate all chunk renders via ffmpeg concat demuxer (`-c copy`, no
   re-encode).

## Known limitation (as of 2026-07-03)

`chunked_pipeline.py` chains download + Phase 1 + render in sequence per
chunk within one Python process. On the GitHub-runner sandbox this risks
the environment's own command timeout when steps are chained back-to-back
(observed once: a render silently truncated to 6.4s instead of 20s when
cut off mid-loop). The 2-chunk/40s join test that validated this
architecture was run as manually-separated stages (each download/Phase1/
render as its own isolated call), not a single invocation of this script.
Before relying on `chunked_pipeline.py` end-to-end unattended, either
confirm it completes within whatever timeout budget the target environment
gives a single job, or split it into separate dispatched steps.

## Audio concat caveat

Concatenating rendered chunks via ffmpeg's concat demuxer can emit a
"Non-monotonic DTS" warning on the audio stream at each join. ffmpeg
auto-corrects it and the 2-chunk join test showed no audible glitch or
gap, but this is auto-correction, not guaranteed-clean timestamps. On
Vast.ai, switch to a concat/mux method that regenerates clean timestamps
(e.g. re-muxing with `-fflags +genpts` or decoding/re-encoding the audio
track at the join) rather than relying on ffmpeg's auto-correction.

## Validated

2-chunk (40s) join test, 2026-07-03: duration ~39.6s, no visible camera
snap/pause at the boundary (confirmed by eye and by velocity data --
continuous deceleration through the join, never exceeding the configured
cap), audio continuous (no silence gaps, no audible glitch on playback),
timestamps monotonic with zero duplicates, velocity cap never exceeded,
output resolution/fps correct (1920x1080 / 29.97fps) throughout.
