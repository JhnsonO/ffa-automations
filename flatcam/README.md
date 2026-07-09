# flatcam

flatcam is a new isolated follow-camera subsystem for flat, non-360, single-lens GoPro footage where the whole small-sided pitch is visible from a static camera; it renders a 16:9 output crop that pans and zooms toward the motion centroid of players rather than trying to identify the ball. It is intentionally self-contained and isolated from `ball_tracker/` and `playcam/`: there are no imports either direction, and v1 uses pixel-space venue polygons on undistorted frames rather than the 360 pipeline's spherical math.

## Lens profiles

To add a new lens, edit `flatcam/lens_profiles.json` only. Add a profile object with `profile_name`, `resolution`, `fov_deg`, `projection`, `distortion_correction_strength`, `calibration_status`, and `notes`; no code changes are needed for approximate v1 tuning.

## CLI examples

Preview undistortion tuning against straight fence lines:

```bash
python flatcam/undistort.py --profile gopro_max2_msv_4k60 --input frame_or_clip.mp4 --preview --output flatcam/undistort_preview.jpg
```

Create a venue mask in undistorted pixel space:

```bash
python flatcam/venue_mask_flat.py --profile gopro_max2_msv_4k60 --input frame_or_clip.mp4 --name cage_a --point 120,80 --point 3720,80 --point 3720,2080 --point 120,2080
```

Render a segment:

```bash
python flatcam/render_segment_flat.py --input clip.mp4 --profile gopro_max2_msv_4k60 --venue flatcam/venues/cage_a.json --output out.mp4 --csv-out state.csv
```

Generate and render synthetic test footage:

```bash
python flatcam/make_test_clip.py --output flatcam/test_clip.mp4 --profile-out flatcam/test_lens_profiles.json --venue-out flatcam/venues/synthetic.json
python flatcam/render_segment_flat.py --input flatcam/test_clip.mp4 --profile synthetic_flatcam_test --venue flatcam/venues/synthetic.json --output flatcam/test_out.mp4 --csv-out flatcam/state.csv
```
