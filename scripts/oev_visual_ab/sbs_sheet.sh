#!/usr/bin/env bash
# sbs_sheet.sh CLIP.mp4 CLIP_START OUT.png t1 t2 t3 t4  (rows of BEFORE|AFTER)
in=$1; s0=$2; out=$3; shift 3
sel=""
for t in "$@"; do n=$(python3 -c "print(round(($t-$s0)*60000/1001))"); sel="${sel:+$sel+}eq(n\\,$n)"; done
nice -n 15 ffmpeg -v error -y -i "$in" -vf "select='$sel',scale=1280:-1,tile=1x$#" -frames:v 1 -vsync vfr "$out"
