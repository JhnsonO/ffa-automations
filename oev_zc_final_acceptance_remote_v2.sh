#!/usr/bin/env bash
# Final acceptance for OEV option-2 CUDA/Vulkan shared-buffer path.
# Runs on ONE throwaway RTX 4090 pod. Production refs/volumes are read-only.
# 1) Real-content HEVC Main10 4K -> NVDEC/P010 option-2 correctness.
# 2) Same-pod 180-frame option2 vs --no-zero-copy performance A/B.
set -euo pipefail
RUN=/tmp/oev_acceptance
RECO_SHA=61aced9687d2c441c48d11e29d3aa28df18b3beb
WGPU_SHA=d74e00f2415e55c0f09a87b0497d66d8192a44bb
RECO_REPO=https://github.com/JhnsonO/video-stitcher
MODEL=/runpod-volume/oev-runtime/models/yolo26m.onnx
LEFT_ID=1CkrbSnjtCuHvTuYtQTkLFg9UVCY8hPC2
RIGHT_ID=1rjjSUBc-0oSPhG6MZ-5BkPMqL6kb3YcA
mkdir -p "$RUN/frames"; cd "$RUN"; : >summary.log; : >timing.log
logsum(){ echo "$*" | tee -a summary.log; }
frames_ok(){ local f=$1 n=$2; [ -s "$f" ] || return 1; [ "$(ffprobe -v error -select_streams v:0 -count_frames -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "$f" 2>/dev/null || true)" = "$n" ]; }
now(){ python3 -c 'import time; print(time.time())'; }
wall(){ python3 - "$1" "$2" <<'PY'
import sys
print(f'{float(sys.argv[2])-float(sys.argv[1]):.3f}')
PY
}

# Exact implementation build and benchmark inputs.
apt-get update -qq
apt-get install -y -qq --no-install-recommends git curl aria2 ffmpeg build-essential pkg-config cmake clang libclang-dev libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libavfilter-dev ca-certificates >setup.log 2>&1
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y >>setup.log 2>&1
export PATH=/root/.cargo/bin:$PATH
[ -s /tmp/drive_token.txt ] || { logsum 'FINAL_ACCEPTANCE=HARNESS_ERROR missing Drive token'; exit 2; }
[ -s "$MODEL" ] || { logsum 'FINAL_ACCEPTANCE=HARNESS_ERROR model missing'; exit 2; }
TOKEN=$(cat /tmp/drive_token.txt)
for spec in "left.mp4:$LEFT_ID" "right.mp4:$RIGHT_ID"; do
  f=${spec%%:*}; id=${spec##*:}
  aria2c -q -x8 -s8 --file-allocation=none --header="Authorization: Bearer $TOKEN" --dir="$RUN" --out="$f" "https://www.googleapis.com/drive/v3/files/$id?alt=media"
  test -s "$f"
done
unset TOKEN; rm -f /tmp/drive_token.txt

git clone -q "$RECO_REPO" /tmp/video-stitcher
cd /tmp/video-stitcher; git checkout -q "$RECO_SHA"; [ "$(git rev-parse HEAD)" = "$RECO_SHA" ]
for c in wgpu wgpu-core wgpu-hal wgpu-types; do
  awk -v name="\"$c\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name){p=1} p && /^source/{print; exit}' Cargo.lock | grep -q "$WGPU_SHA" || { echo "bad $c resolution"; exit 3; }
done
cargo build --release --locked -p reco-cli --features cuda >"$RUN/build.log" 2>&1
RECO=/tmp/video-stitcher/target/release/reco; "$RECO" --version >"$RUN/reco_version.txt"; cd "$RUN"

# Proven NVIDIA Vulkan ICD reconstruction.
CUDA_LIB_DIR=$(find /usr/local -maxdepth 1 -type d -name 'cuda-12.*' 2>/dev/null | sort -V | tail -1)
export LD_LIBRARY_PATH="${CUDA_LIB_DIR}/lib64:${LD_LIBRARY_PATH:-}"
cat >/tmp/nvidia_egl_icd.json <<'JSON'
{"file_format_version":"1.0.1","ICD":{"library_path":"libEGL_nvidia.so.0","api_version":"1.4.312"}}
JSON
export VK_DRIVER_FILES=/tmp/nvidia_egl_icd.json VK_ICD_FILENAMES=/tmp/nvidia_egl_icd.json; unset DISPLAY
vulkaninfo 2>&1 | grep -E 'deviceName|deviceType' | head -4 | tee gpu_env.log
grep -q 'NVIDIA GeForce RTX 4090' gpu_env.log || { logsum 'FINAL_ACCEPTANCE=HARNESS_ERROR not RTX4090'; exit 4; }

# One calibration for the original pair; Main10 derivatives preserve ordering.
curl -fsSL 'https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json' -o lens.json
RUST_LOG=warn,reco_core=info "$RECO" calibrate left.mp4 right.mp4 --left-profile lens.json --right-profile lens.json -o match.json >calibrate.log 2>&1
[ -s match.json ]
python3 - <<'PY'
import json
p='match.json'; m=json.load(open(p))
m['field_roi']={'left':[[0.1227,0.9611],[0.0573,0.6846],[0.1802,0.6285],[0.2645,0.5769],[0.4382,0.4864],[0.4988,0.4658],[0.5942,0.4474],[0.7835,0.4175],[0.9285,0.3785],[1.0,1.0],[0.1227,1.0]],'right':[[0.0391,0.4206],[0.0818,0.4101],[0.1839,0.4070],[0.2783,0.4070],[0.3448,0.4083],[0.4100,0.4161],[0.4684,0.4319],[0.6239,0.4801],[0.7368,0.5200],[0.7980,0.5465],[0.7454,0.9011],[0.7454,1.0],[0.0,1.0]]}
json.dump(m,open(p,'w'))
PY

# TEST 1 — actual HEVC Main10 bitstream at native 3840x2160, generated from real GoPro frames.
logsum '=== TEST1_P010 ==='
make10(){ local i=$1 o=$2; ffmpeg -hide_banner -loglevel error -y -t 3 -i "$i" -an -vf format=p010le -c:v hevc_nvenc -profile:v main10 -preset p4 -rc constqp -qp 18 "$o" || ffmpeg -hide_banner -loglevel error -y -t 3 -i "$i" -an -vf format=yuv420p10le -c:v libx265 -preset ultrafast -x265-params log-level=error "$o"; }
make10 left.mp4 left10.mp4; make10 right.mp4 right10.mp4
for f in left10.mp4 right10.mp4; do ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,profile,pix_fmt,width,height -of default=nw=1 "$f"; done | tee p010_probe.log
[ "$(grep -c 'profile=Main 10' p010_probe.log)" -eq 2 ]; [ "$(grep -Ec 'pix_fmt=(yuv420p10le|p010le)' p010_probe.log)" -eq 2 ]; [ "$(grep -c 'width=3840' p010_probe.log)" -eq 2 ]; [ "$(grep -c 'height=2160' p010_probe.log)" -eq 2 ]
P010_ARGS=(stitch left10.mp4 right10.mp4 -c match.json -o p010_option2.mp4 --model "$MODEL" --tracking field --panner-preset broadcast --lookahead 0 --detection-interval 1 --events p010_events.jsonl --width 1920 --height 1080 --max-frames 60)
set +e; RUST_LOG=warn,reco_core=info,reco_detect=info,reco_autocam=info "$RECO" "${P010_ARGS[@]}" >p010.log 2>&1; P010_RC=$?; set -e
P010_OK=1
[ "$P010_RC" -eq 0 ] || P010_OK=0; frames_ok p010_option2.mp4 60 || P010_OK=0
grep -q 'GPU zero-copy (CUDA shared buffer/Vulkan)' p010.log || P010_OK=0
grep -q '10bit=true' p010.log || P010_OK=0
grep -q 'R16Unorm' p010.log || P010_OK=0; grep -q 'Rg16Unorm' p010.log || P010_OK=0; grep -q 'pitch=7680' p010.log || P010_OK=0
grep -q 'GpuResident detection: CUDA path' p010.log || P010_OK=0
[ "$P010_OK" -eq 1 ] && logsum 'P010_RUNTIME=PASS' || logsum "P010_RUNTIME=FAIL rc=$P010_RC"
for n in 0 30 59; do ffmpeg -hide_banner -loglevel error -y -i p010_option2.mp4 -vf "select=eq(n\,$n)" -vsync 0 -frames:v 1 "frames/p010_${n}.png"; done

# TEST 2 — exact same binary/pod/input/settings, only --no-zero-copy differs.
logsum '=== TEST2_RTX4090_SAME_POD_AB ==='
WARM=(stitch left.mp4 right.mp4 -c match.json --model "$MODEL" --tracking field --panner-preset broadcast --lookahead 0.1 --detection-interval 1 --width 1920 --height 1080 --max-frames 12)
RUST_LOG=warn "$RECO" "${WARM[@]}" -o warm_option2.mp4 >/dev/null 2>&1 || true
RUST_LOG=warn "$RECO" "${WARM[@]}" -o warm_nozero.mp4 --no-zero-copy >/dev/null 2>&1 || true
rm -f warm_option2.mp4 warm_nozero.mp4
COMMON=(stitch left.mp4 right.mp4 -c match.json --model "$MODEL" --tracking field --panner-preset broadcast --lookahead 0.1 --detection-interval 1 --width 1920 --height 1080 --max-frames 180)
T0=$(now); set +e; RUST_LOG=warn,reco_core=info,reco_detect=info,reco_autocam=info "$RECO" "${COMMON[@]}" -o ab_option2.mp4 --events ab_option2.jsonl >ab_option2.log 2>&1; OPT_RC=$?; set -e; T1=$(now); OPT_WALL=$(wall "$T0" "$T1")
T2=$(now); set +e; RUST_LOG=warn,reco_core=info,reco_detect=info,reco_autocam=info "$RECO" "${COMMON[@]}" -o ab_nozero.mp4 --events ab_nozero.jsonl --no-zero-copy >ab_nozero.log 2>&1; NOZ_RC=$?; set -e; T3=$(now); NOZ_WALL=$(wall "$T2" "$T3")
AB_OK=1
[ "$OPT_RC" -eq 0 ] || AB_OK=0; [ "$NOZ_RC" -eq 0 ] || AB_OK=0; frames_ok ab_option2.mp4 180 || AB_OK=0; frames_ok ab_nozero.mp4 180 || AB_OK=0
grep -q 'GPU zero-copy (CUDA shared buffer/Vulkan)' ab_option2.log || AB_OK=0
grep -q 'ORT: CUDA execution provider enabled' ab_option2.log || AB_OK=0
python3 - "$OPT_WALL" "$NOZ_WALL" >ab_metrics.txt <<'PY'
import sys
opt,noz=map(float,sys.argv[1:]); print(f'option2_wall_s={opt:.3f}'); print(f'option2_wall_fps={180/opt:.3f}'); print(f'no_zero_copy_wall_s={noz:.3f}'); print(f'no_zero_copy_wall_fps={180/noz:.3f}'); print(f'speedup_x={noz/opt:.3f}'); print(f'improvement_pct={(noz/opt-1)*100:.1f}')
PY
cat ab_metrics.txt | tee -a summary.log
SPEEDUP=$(awk -F= '/^speedup_x=/{print $2}' ab_metrics.txt); python3 - "$SPEEDUP" <<'PY' || AB_OK=0
import sys
raise SystemExit(0 if float(sys.argv[1])>1.05 else 1)
PY
[ "$AB_OK" -eq 1 ] && logsum 'RTX4090_SAME_POD_AB=PASS' || logsum "RTX4090_SAME_POD_AB=FAIL option2_rc=$OPT_RC nozero_rc=$NOZ_RC"
for spec in 'ab_option2.mp4:option2' 'ab_nozero.mp4:nozero'; do f=${spec%%:*}; t=${spec##*:}; ffmpeg -hide_banner -loglevel error -y -i "$f" -vf 'select=eq(n\,90)' -vsync 0 -frames:v 1 "frames/${t}_90.png"; done

if [ "$P010_OK" -eq 1 ] && [ "$AB_OK" -eq 1 ]; then logsum 'FINAL_ACCEPTANCE=PASS'; exit 0; fi
logsum 'FINAL_ACCEPTANCE=FAIL'; exit 20
