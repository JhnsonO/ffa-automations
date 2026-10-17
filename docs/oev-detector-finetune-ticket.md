# OEV-005: fine-tune the ball detector on FFA footage

**Status:** proposed, not started (17 Oct 2026). Suggested model: Opus.
**Supersedes:** further hand-written tracker rules (OEV-004 step 3 crop
verifier and the parked shape prior). Research summary: specialist sports
systems fix false targets by training the detector on in-domain footage with
hard negatives, and by using motion across frames; tracker rules are a safety
net only.

## Why
- The current detector is COCO-pretrained (YOLO26m, class 32 "sports ball").
  It has never been shown FFA camera angles, knees, boots, bibs, fence features
  or spare balls as "not a ball".
- OEV-004 steps 1-2 (body attachment + size prior,
  `video-stitcher@experiment/active-ball-selection-01` `311c702`) cut
  body-zone tracking 15.0 s -> 4.6 s on sample_02 with 166/166 labelled real-ball
  frames retained, but moved 0/38 audited false-object frames (fence, boots,
  brief upper-body hits). The size prior is fitted to one camera position.
- The parked shape prior (`experiment/active-ball-shape-prior-01` `8f2273a`)
  fixes 10/38 but may drop motion-blurred balls (unverified). Not to be merged.

## Data
- Source: full-session GoPro clips on Johnson's Drive (e.g. GX010254, GX010255,
  GX010256, GX010510; 23-36 GB each, 4K) plus the sample_01..05 180 s clips.
  Use several sessions/pitches/lighting conditions; hold out at least one whole
  session (never seen in training) for evaluation, plus sample_02.
- Drive files can be downloaded in the sandbox with
  `curl -L "https://drive.usercontent.google.com/download?id=<ID>&export=download&confirm=t"`
  (worked for a 71 MB file; confirm for multi-GB, stream + sample frames
  rather than storing whole files).
- Sample frames sparsely (e.g. 1 frame / 2-3 s, more around play) from both
  cameras at native resolution. Target ~3,000-5,000 frames to start.

## Labelling (Opus, two passes)
1. **Pre-label** with the current YOLO26m (ball + person) at a low confidence
   threshold so near-misses are included.
2. **Pass 1 (Opus):** for each ball candidate crop (with surrounding context),
   label BALL / NOT_BALL (knee-clothing, boot, hand/head, fence/structure,
   line/marking, other) / SPARE_BALL (a real ball not in play: still a ball for
   the detector) / UNSURE. Also mark visible balls with no candidate (missed
   detections) so recall is trained too.
3. **Pass 2 (Opus, independent):** re-label a shuffled copy without seeing
   pass 1. Agreement -> accepted. Disagreement or UNSURE -> Johnson reviews a
   crop sheet (or drop the item).
4. Report pass-1/pass-2 agreement rate; record it as a label-quality measure.
- Seed hard negatives from known cases: sample_02 knee stretches
  (`PHASE3_suspects`-style metric), fence spots at panorama (-0.588, 0.118) and
  (-0.536, 0.116), audit FALSE_OBJECT frames (`OEV_Audit_Complete.zip`).
- Spare balls stay labelled "ball" for the detector; spare handling remains the
  tracker's job (Phase 3 static sites).

## Training
- Fine-tune YOLO26m from the current weights; classes: ball, person (keep
  person quality, the tracker and panner depend on it). Same input size and
  export format the pipeline uses today, so it is a drop-in model swap.
- Paid GPU training: state cost/runtime estimate and get Johnson's approval
  before starting.

## Evaluation (in this order)
1. Held-out frames: ball precision/recall and person AP vs current YOLO26m.
2. False-object rate on the hard-negative set (knees, boots, fence).
3. Real-ball recall on the audit `REAL_PLAY_BALL` frames.
4. One GPU harness A/B on sample_02 (current model vs fine-tuned, both with
   Phase 3 + #10 clock + #12 ROI fix; OEV-004 on and off): body-zone suspect
   seconds, false-object stills, full losses, ball-in-view. ~GBP 0.30 per arm,
   approval required.
5. Johnson's visual judgement decides.

## Out of scope
Tracker rules, panner, renderer, production merge, Phase 3 changes. A
motion-based detector (TrackNet / WASB style, multi-frame) is the next option
only if fine-tuning alone leaves material failures.

## Risks
- Labelling cost: Opus vision over thousands of crops is token-heavy; batch
  crops into sheets.
- Label errors teach the model wrong; the two-pass agreement check is the guard.
- A model swap changes every downstream number; re-baseline OEV-004 and Phase 3
  on the new detections before judging them.
- Licensing: Reco/OEV AGPL review still open (commercial blocker, not a
  training blocker).
