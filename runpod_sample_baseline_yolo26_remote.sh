#!/usr/bin/env bash
# TEST-ONLY stride-3 adapter for the successful v4 180s sample runner.
# Fetches the exact v4 stride-1 runner and changes only detector cadence plus
# experiment labelling. All v4 panner/containment/micro-damping settings stay
# identical for a clean stride-1 vs stride-3 comparison.
set -euo pipefail
cd /tmp/oev_run

V4_FFA_SHA="8febf7a90e048170b7c677e6b341545089d5b774"
V4_RUNNER="/tmp/oev_run/runpod_sample_baseline_yolo26_v4_stride1_exact.sh"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${V4_FFA_SHA}/runpod_sample_baseline_yolo26_remote.sh" \
  -o "$V4_RUNNER"
test -s "$V4_RUNNER"

python3 - "$V4_RUNNER" <<'PY'
from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text()
marker = 'exec "$V3_RUNNER"\n'
if s.count(marker) != 1:
    raise SystemExit(f"expected one v4 runner final exec marker, found {s.count(marker)}")

# The v4 wrapper operates on the v3 wrapper, not on the final base runner that
# contains STITCH_ARGS. Inject a tiny patch into the v3 wrapper immediately
# before its one actual BASE_SCRIPT execution. The following containment-metrics
# command makes that execution boundary unique; the other BASE_SCRIPT mentions
# are fetch/test/python/chmod plumbing and must not be patched.
adapter = r"""python3 - "$V3_RUNNER" <<'PY_STRIDE'
from pathlib import Path
import sys

p = Path(sys.argv[1])
t = p.read_text()
metrics_marker = "python3 - <<'PY' 2>&1 | tee containment_metrics.log\n"
run_marker = '"$BASE_SCRIPT"\n\n' + metrics_marker
if t.count(run_marker) != 1:
    raise SystemExit(f"expected one v3 base execution -> metrics boundary, found {t.count(run_marker)}")

stride_patch = r'''python3 - "$BASE_SCRIPT" <<'PY_FRAME_STRIDE'
from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text()
old = "  --detection-interval 1\n  --events events.jsonl\n"
new = "  --detection-interval 1\n  --frame-stride 3\n  --events events.jsonl\n"
if s.count(old) != 1:
    raise SystemExit(f"expected one final stitch cadence marker, found {s.count(old)}")
s = s.replace(old, new, 1)
if s.count("--frame-stride 3") != 1:
    raise SystemExit(f"expected exactly one --frame-stride 3 after patch, found {s.count('--frame-stride 3')}")
p.write_text(s)
print("stride-3 final base runner prepared: --detection-interval 1 + --frame-stride 3")
PY_FRAME_STRIDE
'''
base_exec_and_metrics = '"$BASE_SCRIPT"\n\n' + metrics_marker
t = t.replace(run_marker, stride_patch + base_exec_and_metrics, 1)

t = t.replace(
    'lookahead_ball_containment_04_micro_damping',
    'lookahead_ball_containment_04_micro_damping_stride3',
)
t = t.replace(
    'v4_micro_damping_same_180s_sample',
    'v4_micro_damping_stride3_same_180s_sample',
)

p.write_text(t)
print("stride-3 runner adapter prepared: unique base execution boundary patched")
PY_STRIDE
exec "$V3_RUNNER"
"""

s = s.replace(marker, adapter, 1)
p.write_text(s)
PY

chmod +x "$V4_RUNNER"
echo "TEST_ONLY_RUNNER=v4_micro_damping_stride3 same_sample=sample_01 duration_s=180"
exec "$V4_RUNNER"
