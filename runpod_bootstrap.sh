#!/usr/bin/env bash
# TEST-ONLY stride-3 adapter for the successful v4 micro-damping experiment.
#
# Reuses the exact v4 wrapper from commit 8febf7a, then adapts only the
# source-patch anchors needed by stride-enabled Reco b2fc622f. Product logic
# remains the v4 ball hysteresis + containment + camera dynamics + 4deg
# micro-damping. Production/main is untouched.
set -euo pipefail

V4_FFA_SHA="8febf7a90e048170b7c677e6b341545089d5b774"
V4_SCRIPT="/tmp/runpod_bootstrap_v4_stride1_exact.sh"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${V4_FFA_SHA}/runpod_bootstrap.sh" \
  -o "$V4_SCRIPT"
test -s "$V4_SCRIPT"

python3 - "$V4_SCRIPT" <<'PY'
from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text()
marker = 'exec "$V3_SCRIPT"\n'
if s.count(marker) != 1:
    raise SystemExit(f"expected one v4 final exec marker, found {s.count(marker)}")

adapter = r'''python3 - "$V3_SCRIPT" <<'PY_STRIDE'
from pathlib import Path
import sys

p = Path(sys.argv[1])
t = p.read_text()

old_sha = 'RECO_SHA="f27cbb6d0d65fcf9a11fb4d82d119ae214695318"'
new_sha = 'RECO_SHA="b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085"'
if t.count(old_sha) != 1:
    raise SystemExit(f"expected one v4 Reco SHA marker, found {t.count(old_sha)}")
t = t.replace(old_sha, new_sha, 1)

# Stride Reco moved detection into an analysis-frame branch. Preserve the v4
# filter at that branch and write the stabilised state back to last_world_state
# so the two render-only frames between analyses reuse the filtered signal.
start = t.index('world_match = "')
end = t.index('\n\nstate_marker = ', start)
new_world_patch = r'''world_match = "                let world_state = match detection_result {\n"
if s.count(world_match) != 1:
    raise SystemExit(
        f"expected exactly one stride world_state match marker, found {s.count(world_match)}"
    )
s = s.replace(
    world_match,
    "                let mut world_state = match detection_result {\n",
    1,
)

world_done = "                };\n                (world_state, session.detection.last_detections.clone())\n"
if s.count(world_done) != 1:
    raise SystemExit(
        f"expected exactly one stride world_state completion marker, found {s.count(world_done)}"
    )
s = s.replace(
    world_done,
    "                };\n"
    "                stabilize_world_ball(&mut world_state, &mut ball_signal_filter_state, source_index);\n"
    "                session.last_world_state = world_state.clone();\n"
    "                (world_state, session.detection.last_detections.clone())\n",
    1,
)'''
t = t[:start] + new_world_patch + t[end:]

# The stride loop no longer has panner_frame_idx; put the same final-camera
# guard state beside the sparse-pose queues instead.
start = t.index('state_marker = ')
end = t.index('\n\nrender_call = ', start)
new_state_patch = r'''state_marker = "        let mut sparse_finalized = false;\n"
if s.count(state_marker) != 1:
    raise SystemExit(
        f"expected exactly one stride sparse_finalized marker, found {s.count(state_marker)}"
    )
s = s.replace(
    state_marker,
    state_marker
    + "        let mut ball_containment_guard_state = BallContainmentGuardState::default();\n",
    1,
)'''
t = t[:start] + new_state_patch + t[end:]

t = t.replace(
    'TEST_ONLY_RECO_PATCH=lookahead_ball_containment_04_micro_damping',
    'TEST_ONLY_RECO_PATCH=lookahead_ball_containment_04_micro_damping_stride3',
)

p.write_text(t)
print(
    "stride adapter prepared: Reco=b2fc622f, v4 filter on sparse analysis frames, "
    "stabilised last_world_state retained for render-only frames"
)
PY_STRIDE
exec "$V3_SCRIPT"
'''

s = s.replace(marker, adapter, 1)
p.write_text(s)
PY

chmod +x "$V4_SCRIPT"
echo "TEST_ONLY_STRIDE_ADAPTER=v4_micro_damping_stride3 reco_sha=b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085"
exec "$V4_SCRIPT"
