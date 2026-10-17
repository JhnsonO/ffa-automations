#!/usr/bin/env bash
# Build the deliverables from before.mp4 / after.mp4 (+ diagnostic overlays).
set -euo pipefail
cd ~/oevrender
mkdir -p out
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
[ -f "$FONT" ] || FONT=$(fc-list | grep -i dejavu | head -1 | cut -d: -f1)

label() { # $1 text -> drawtext filter
  echo "drawtext=fontfile=$FONT:text='$1':x=20:y=18:fontsize=34:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=10"
}
clock="drawtext=fontfile=$FONT:text='%{pts\\:hms}':x=w-tw-20:y=18:fontsize=30:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=8"

# sbs START END OUT [W H] : side-by-side BEFORE | AFTER, synchronised, with
# labels and the source-video clock (pts offset back to the clip's real time).
sbs() {
  local s=$1 e=$2 out=$3 w=${4:-1280} h=${5:-720}
  local d; d=$(python3 -c "print($e-$s)")
  ffmpeg -v error -y -ss "$s" -t "$d" -i before.mp4 -ss "$s" -t "$d" -i after.mp4 -filter_complex \
    "[0:v]setpts=PTS-STARTPTS+$s/TB,scale=$w:$h,$(label 'BEFORE  (corrected timing, old spare-ball logic)'),$clock[a]; \
     [1:v]setpts=PTS-STARTPTS+$s/TB,scale=$w:$h,$(label 'AFTER  (corrected timing + Phase 3 spare-ball logic)'),$clock[b]; \
     [a][b]hstack=inputs=2,setpts=PTS-STARTPTS[v]" \
    -map "[v]" -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p -movflags +faststart "$out"
}

# Diagnostic side-by-side from the overlay renders (already labelled).
dsbs() {
  local s=$1 e=$2 out=$3
  local d; d=$(python3 -c "print($e-$s)")
  ffmpeg -v error -y -ss "$s" -t "$d" -i diag_before.mp4 -ss "$s" -t "$d" -i diag_after.mp4 -filter_complex \
    "[0:v]scale=1280:720[a];[1:v]scale=1280:720[b];[a][b]hstack=inputs=2[v]" \
    -map "[v]" -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p -movflags +faststart "$out"
}

case "${1:-all}" in
  sbs)
    sbs 0 179.7 out/OEV_sample02_BEFORE_vs_AFTER_full.mp4 960 540
    sbs 10 24  out/OEV_W1_10-24s_early_spare_fixation.mp4
    sbs 31 46  out/OEV_W2_31-46s_incorrect_ball.mp4
    sbs 114 144 out/OEV_W3_114-144s_stationary_ball_and_long_pass.mp4
    ;;
  singles)
    ffmpeg -v error -y -i before.mp4 -c copy -movflags +faststart out/OEV_sample02_BEFORE_full_1080p.mp4
    ffmpeg -v error -y -i after.mp4  -c copy -movflags +faststart out/OEV_sample02_AFTER_full_1080p.mp4
    ;;
  diag)
    dsbs 0 179.7 out/OEV_sample02_DIAGNOSTIC_BEFORE_vs_AFTER_full.mp4
    ;;
esac
ls -la out
