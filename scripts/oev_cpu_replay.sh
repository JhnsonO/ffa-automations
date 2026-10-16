#!/usr/bin/env bash
# OEV-001 CPU replay runner. CPU only: no GPU, RunPod, paid API or dispatch.
#
# Downloads the four September sample_02 event caches (outside Git), verifies
# byte size + SHA256 against oev/cpu_replay/provenance.json, builds the
# video-stitcher cpu_replay example at a given ref and replays every run on
# the historical clock and the media clock. Optionally replays an ablation ref
# (default: the log-only release_site diagnostic 968e156) on the ROI run.
#
# Usage:
#   bash scripts/oev_cpu_replay.sh [--stitcher DIR] [--ref SHA] [--ablation-ref SHA|none]
#                                  [--data DIR] [--out DIR]
#
# --stitcher must be a video-stitcher checkout containing
# crates/reco-autocam/examples/cpu_replay.rs (branch audit/cpu-ball-replay-01).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT/oev/cpu_replay/provenance.json"
STITCHER="${STITCHER:-$PWD/video-stitcher}"
REF=""
ABLATION_REF="968e156b8c86144eaeb8ebea634cf11422259958"
DATA="${DATA:-$PWD/oev-replay-data}"
OUT="${OUT:-$PWD/oev-replay-out}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stitcher) STITCHER="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --ablation-ref) ABLATION_REF="$2"; shift 2 ;;
    --data) DATA="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "unknown flag $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$DATA" "$OUT"

# 1. Fetch + verify inputs.
python3 - "$MANIFEST" "$DATA" <<'PY'
import hashlib, json, pathlib, subprocess, sys
manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())
data = pathlib.Path(sys.argv[2])
for run in manifest["runs"]:
    path = data / f"run{run['run_id']}.jsonl"
    if not path.exists() or path.stat().st_size != run["bytes"]:
        url = f"https://drive.usercontent.google.com/download?id={run['drive_id']}&export=download&confirm=t"
        subprocess.run(["curl", "-sSfL", "-o", str(path), url], check=True)
    size = path.stat().st_size
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ok = size == run["bytes"] and digest == run["sha256"]
    print(f"{run['run_id']}: bytes={size} sha256={digest} {'OK' if ok else 'MISMATCH'}")
    if not ok:
        sys.exit(f"input verification failed for {run['run_id']}")
PY

# 2. Build the replay at the requested ref (default: current checkout).
build_example() {
  local dir="$1" target="$2"
  (cd "$dir" && CARGO_TARGET_DIR="$target" cargo build --release -p reco-autocam \
      --no-default-features --example cpu_replay >&2)
  echo "$target/release/examples/cpu_replay"
}

if [[ -n "$REF" ]]; then
  git -C "$STITCHER" checkout --quiet "$REF"
fi
TESTED_SHA="$(git -C "$STITCHER" rev-parse HEAD)"
DIRTY="$(git -C "$STITCHER" diff HEAD | sha256sum | cut -d' ' -f1)"
echo "video-stitcher tested SHA: $TESTED_SHA (uncommitted diff sha256: $DIRTY)"
BIN="$(build_example "$STITCHER" "$STITCHER/target")"

# 3. Replay every run on both clocks.
for run in $(python3 -c "import json;print(' '.join(r['run_id'] for r in json.load(open('$MANIFEST'))['runs']))"); do
  for clock in historical media production-media; do
    "$BIN" --events "$DATA/run$run.jsonl" --clock "$clock" \
      --out "$OUT/${clock}_$run.json" --trace "$OUT/${clock}_$run.csv" --label "${clock}_$run"
    # Same files under the p2_ prefix used by oev_clock_compare.py.
    cp "$OUT/${clock}_$run.json" "$OUT/p2_${clock}_$run.json"
    cp "$OUT/${clock}_$run.csv" "$OUT/p2_${clock}_$run.csv"
  done
done

# 4. Optional release ablation on the ROI-regression run (historical + media).
if [[ "$ABLATION_REF" != "none" ]]; then
  WT="$OUT/ablation-worktree"
  rm -rf "$WT"
  git -C "$STITCHER" worktree add --force --quiet "$WT" "$ABLATION_REF"
  # The diagnostic ref predates the replay tool: copy in only the harness
  # (example + dev-dependency lines). Tracker code stays exactly the ref's.
  mkdir -p "$WT/crates/reco-autocam/examples"
  cp "$STITCHER/crates/reco-autocam/examples/cpu_replay.rs" "$WT/crates/reco-autocam/examples/"
  cp "$STITCHER/crates/reco-autocam/Cargo.toml" "$WT/crates/reco-autocam/Cargo.toml"
  cp "$STITCHER/Cargo.lock" "$WT/Cargo.lock"
  git -C "$WT" diff --stat -- crates/reco-autocam/src && echo "ablation ref tracker source unchanged by harness"
  ABIN="$(build_example "$WT" "$STITCHER/target-ablation")"
  for clock in historical media; do
    "$ABIN" --events "$DATA/run34236809938.jsonl" --clock "$clock" \
      --out "$OUT/ablation_${clock}_34236809938.json" --trace "$OUT/ablation_${clock}_34236809938.csv" \
      --label "ablation_${clock}_34236809938"
  done
  git -C "$STITCHER" worktree remove --force "$WT"
fi

# 5. Comparisons.
CMP="$ROOT/scripts/oev_replay_compare.py"
python3 "$CMP" "$OUT/historical_34202680125.json" "$OUT/historical_34236809938.json" \
  --markdown-out "$OUT/compare_control_vs_roi.md" >/dev/null
python3 "$CMP" "$OUT/historical_34236809938.json" "$OUT/media_34236809938.json" \
  --markdown-out "$OUT/compare_roi_hist_vs_media.md" >/dev/null
if [[ -f "$OUT/ablation_historical_34236809938.json" ]]; then
  python3 "$CMP" "$OUT/historical_34236809938.json" "$OUT/ablation_historical_34236809938.json" \
    --markdown-out "$OUT/compare_roi_vs_release_ablation.md" >/dev/null
fi
python3 "$ROOT/scripts/oev_clock_compare.py" --dir "$OUT" \
  --runs 34202680125 34228909395 34236809938 34278293284 \
  --markdown-out "$OUT/compare_historical_vs_production_media.md" >/dev/null
echo "OEV_CPU_REPLAY=DONE out=$OUT"
