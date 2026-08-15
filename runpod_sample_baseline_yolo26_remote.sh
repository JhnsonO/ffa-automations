#!/usr/bin/env bash
# TEST-ONLY wrapper for experiment/small-pitch-panner-01.
# Fetches the exact mainline sample runner this experiment was based on,
# changes only the panner overlay to lock horizontal FOV at 44 degrees,
# then executes the normal runner. Production/main remains untouched.
set -euo pipefail
cd /tmp/oev_run

BASE_SHA="aad4aedcf8aa6f949c4b80bbbab5580dd322d24a"
BASE_URL="https://raw.githubusercontent.com/JhnsonO/ffa-automations/${BASE_SHA}/runpod_sample_baseline_yolo26_remote.sh"
BASE_SCRIPT="/tmp/oev_run/runpod_sample_baseline_yolo26_base.sh"

curl -fsSL "$BASE_URL" -o "$BASE_SCRIPT"
test -s "$BASE_SCRIPT"

python3 - "$BASE_SCRIPT" <<'PY'
from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text()
old = '  echo "{\\"cluster_alpha\\": ${CLUSTER_ALPHA_OVERRIDE}}" > panner_overlay.json\n'
new = r'''  printf '{"cluster_alpha": %s, "fov_tight": 44.0, "fov_wide": 44.0, "fov_default": 44.0}\n' "$CLUSTER_ALPHA_OVERRIDE" > panner_overlay.json''' + "\n"
if s.count(old) != 1:
    raise SystemExit(f"expected exactly one panner-overlay line, found {s.count(old)}")
s = s.replace(old, new)
old_log = '  echo "Panner overlay active: cluster_alpha=${CLUSTER_ALPHA_OVERRIDE} (panner_overlay.json)" | tee -a stitch.log\n'
new_log = '  echo "Panner overlay active: cluster_alpha=${CLUSTER_ALPHA_OVERRIDE}, fixed_fov=44.0deg (panner_overlay.json)" | tee -a stitch.log\n'
if s.count(old_log) != 1:
    raise SystemExit(f"expected exactly one panner-overlay log line, found {s.count(old_log)}")
p.write_text(s.replace(old_log, new_log))
PY

chmod +x "$BASE_SCRIPT"
echo "TEST_ONLY_PANNER_PROFILE=small_pitch_01 cluster_alpha=${CLUSTER_ALPHA_OVERRIDE:-unset} fixed_fov=44.0 lookahead=${LOOKAHEAD:-unset}"
exec "$BASE_SCRIPT"
