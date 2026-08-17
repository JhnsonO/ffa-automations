#!/usr/bin/env python3
from pathlib import Path

p = Path('docs/ai-project-state.md')
s = p.read_text()

marker = '## Ball-state bridging, panner containment, and VLM recovery direction — ACTIVE (17 Aug 2026)'
if marker in s:
    raise SystemExit('state section already present; refusing duplicate update')

old_title = '# FFA / OEV — Current Status (updated 16 Aug 2026)'
if old_title in s:
    s = s.replace(old_title, '# FFA / OEV — Current Status (updated 17 Aug 2026)', 1)
elif '# FFA / OEV — Current Status (updated 17 Aug 2026)' not in s:
    raise SystemExit('unexpected state-doc title/date; inspect before updating')

old_panner = '- **Panner-lag / ball-out-of-frame (`cluster_alpha`/EMA/lookahead) — OPEN, top product-quality priority.** Ball outside visible frame 51–57% of tracking time, model-independent. Not yet actioned.'
new_panner = '- **Ball-state recovery / panner containment — ACTIVE, major progress 17 Aug.** Backward bridging now keeps `world.ball` populated on ~98.6% of `sample_02` stride-1 frames despite raw YOLO missing ~45.4%; the remaining dominant failure is camera containment when a known ball is not given enough authority. A direct ball-containment override is currently in a test-only A/B and is **not yet product-validated**.'
if old_panner in s:
    s = s.replace(old_panner, new_panner, 1)
else:
    raise SystemExit('expected stale panner summary line not found; inspect state doc before update')

summary_anchor = '- **OEV/Reco licensing (AGPL-3.0) — OPEN, commercial blocker before customer-facing launch.** Full review needed before OEV is sold; not a technical blocker for current testing. See "Frame-stride testing + production rollout — FINAL HANDOFF" below for detail.\n'
vlm_summary = '- **VLM-assisted ball recovery — NEXT ARCHITECTURE TICKET, not implemented yet.** Keep YOLO/tracker/bridge as the cheap primary path; invoke a contextual reasoner only for genuine unresolved/ambiguous ball state. The VLM must return a candidate hypothesis (`A/B/C/none`), never control the camera directly or mutate trusted state without validation. Design the interface so a commercial API backend can later be replaced by an OEV-owned specialist model.\n'
if summary_anchor not in s:
    raise SystemExit('summary insertion anchor not found')
s = s.replace(summary_anchor, summary_anchor + vlm_summary, 1)

detail_anchor = '---\n\n## Frame-stride 3 multi-sample validation — REGRESSION FOUND; stride-1 control rendered (16 Aug 2026)'
detail = '''---

## Ball-state bridging, panner containment, and VLM recovery direction — ACTIVE (17 Aug 2026)

**Current product-quality finding:** the project has now separated **ball-state knowledge** from **camera framing**. Raw YOLO26m ball recall on `sample_02` remains poor (~54.6% present / ~45.4% missing), but the merged backward bridge reconstructs most of those gaps. On the clean stride-1 bridging run `31980187523`, `world.ball=None` fell to **150 / 10,775 frames = 1.39%**, with **4,714 Bridged frames** and longest remaining `None` gap **71 frames (~1.18s at 60fps)**. This is the strongest evidence so far that detector misses and camera misses are now separate problems.

**Backward bridge status — MERGED / proven on the test sample:** `JhnsonO/video-stitcher` main includes the genuine-anchor backward bridge at `c8b0d74b537d192c7de8d2856de64620a82830cf`. `TrackState::Bridged` interpolates between genuine Tracking anchors found inside lookahead; Bridged states cannot recursively anchor more bridge states. The bridge is deliberately not a detector replacement: long gaps with no plausible future anchor can still remain unknown.

**Responsive panner tuning run — completed, useful but insufficient:** run `31997785854` completed successfully on `sample_02`, 180s, stride 1, YOLO26m, lookahead 1.5s with the agreed small-pitch tuning (`ball_weight=0.70`, `dead_zone_rad=0.06`, `cluster_alpha=0.08`, `velocity_alpha=0.08`, `max_velocity_rad_per_sec=0.31`, dynamic FOV 38/44/58 degrees, no v4 damping). Event reanalysis still found the tracked ball outside the horizontal rendered frustum on **17.71% of known-ball frames**; combined tracker-derived ball non-containment was ~**18.86%**. Treat this as a horizontal telemetry approximation, not a full 2D visibility metric. Several multi-second misses occurred while YOLO and the tracker were actively Tracking the ball, proving that more detector recall alone cannot solve the remaining camera failure.

**Why the panner can ignore a known ball:** the current `FieldPanner` starts from the densest player cluster, gives the ball only a weighted pull while it is considered close enough to that cluster, and its lookahead target prefers future player-cluster context whenever a cluster exists. Therefore `world.ball` can be correct while the resolved camera target remains far away. Product priority is now explicit: **ball containment first, action/player context second, smoothness third, aesthetic composition fourth.**

**Direct containment experiment — IN PROGRESS, not validated / not merged:** test branch `test/oev-ball-containment-override-01`, branch head `5394502f4a77a99e4e26225f4b800db3c0279a52`, workflow run `32000972073`. Production `video-stitcher/main` remains unchanged. The test-only runtime patch adds an opt-in containment state for trusted `Tracking`/`Bridged` balls: enter when ball offset reaches **0.80 x current half-FOV**, leave only after it returns inside **0.45 x half-FOV** (hysteresis); during containment the target is the ball directly, player-cluster composition/cluster lookahead and the normal dead-zone do not get to hold the camera elsewhere, and FOV can widen toward the configured 58-degree ceiling. Do **not** report this as successful until the run completes and the video + telemetry are reviewed. If it works, promote the behavior through normal Reco code review rather than keeping a runtime patch.

**Next major ball-recovery architecture — VLM/context fallback, NOT IMPLEMENTED YET:** stop treating every YOLO miss as a VLM call. The intended hierarchy is `YOLO/detector -> candidate generation -> tracker/coast/backward bridge -> uncertainty trigger -> contextual reasoner -> validation gate -> trusted WorldState -> panner`. Candidate generation must remain separate from trusted ball-state mutation (same architectural correction already required for tiled/high-res reacquisition).

**VLM v1 contract:** invoke only when deterministic tracking/bridge remains genuinely uncertain or several plausible candidates compete. Exploit OEV's offline nature by supplying selected frames **before and after** the uncertain interval, previous trusted ball state, next trusted ball state when available, low-confidence/high-resolution ball candidates, full-frame/panorama context, and player positions/movement already available from Reco. Prefer a constrained answer such as **candidate A / B / C / none + confidence** rather than asking a general model to invent arbitrary ball coordinates.

**Validation gate:** a VLM answer is a **hypothesis only**. Before mutating `WorldState`, validate trajectory/velocity plausibility, consistency with later trusted observations, field bounds, stationary/spare-ball rejection, candidate motion and any other deterministic evidence available. Log every invocation, candidate set, context window, backend response, confidence, accept/reject reason and resulting state transition. The VLM must never directly control the camera.

**Backend abstraction / cost path:** introduce a provider-neutral `BallRecoveryReasoner`-style interface. An API VLM can be the first experimental backend; if credentials/provider wiring is undesirable, implement a deterministic mock/replay backend first so the recovery pipeline is testable without network calls. Long term, the intended commercial cost path is to train/fine-tune a compact OEV-specific temporal/candidate-ranking model on accumulated hard recovery cases and reserve a larger VLM for rare escalations — do **not** attempt to train a general foundation VLM from scratch.

**Commercial/IP design constraint:** keep the implementation independently derived and document architectural decisions. Do not use a competitor patent as an implementation specification or deliberately recreate a disclosed competitor pipeline. A formal freedom-to-operate review is still required before commercial launch; this is separate from the current technical prototype work.

**Immediate next actions:** (1) finish and visually/telemetrically evaluate containment run `32000972073`; (2) if containment works, convert it from test-only patch to reviewed Reco behavior and re-run the same sample; (3) start the VLM fallback ticket with the provider-neutral reasoner interface, uncertainty trigger, candidate package and validation gate; (4) keep the first VLM experiment isolated from panner tuning so ball-recovery quality can be measured independently.

## Frame-stride 3 multi-sample validation — REGRESSION FOUND; stride-1 control rendered (16 Aug 2026)'''
if detail_anchor not in s:
    raise SystemExit('detail insertion anchor not found')
s = s.replace(detail_anchor, detail, 1)

p.write_text(s)
