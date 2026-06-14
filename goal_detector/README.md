# goal_detector/

Prototype goal-clip detector — **not wired into any live workflow yet.**

## What it's meant to do
Automatically identify goal moments in session footage:
1. Motion detection on the frame to flag a likely shot/goal event.
2. GPT-4o vision confirmation to reduce false positives (audio excluded — multi-pitch noise makes it unreliable).
3. Output: a timestamped list of candidate clips for Kris to review.

## Status
Early prototype. Not functional end-to-end. No GitHub Actions workflow calls it.

## Dependencies
See `requirements.txt` — install with:
```bash
pip install -r goal_detector/requirements.txt
```

## Next steps when picking this up
- Wire a test run against a single uploaded session MP4 from Drive
- Tune the motion threshold to reduce false positives at multi-pitch sessions
- Decide whether output feeds into the clips Google Sheet (most logical integration point)
