#!/usr/bin/env bash
set -euo pipefail
python3 -m py_compile scripts/oev_harness.py scripts/oev_analyze_events.py scripts/oev_dispatch_and_wait.py
python3 scripts/oev_harness.py validate
python3 scripts/oev_analyze_events.py --self-test
echo 'OEV_HARNESS_CHECK=PASS'
