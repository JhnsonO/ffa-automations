#!/usr/bin/env bash
# $1 = arm name, $2 = replay_render binary
set -u
cd ~/oevrender
VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.json "$2" --left ~/oevsrc/left.mp4 --right ~/oevsrc/right.mp4 \
  -c ~/oevsrc/match.json --replay ~/oevdata/run34236809938.jsonl \
  -o "$1.mp4" --events "$1_events.jsonl" \
  --panner-preset v4_micro_damping --panner-config overlay_8aside.json \
  --lookahead 1.5 --max-frames 10775 --width 1920 --height 1080 > "$1.log" 2>&1
echo "exit=$?" >> "$1.log"
