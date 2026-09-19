#!/usr/bin/env bash
# Usage: BASE_URL=https://xxx.vercel.app HEALTH_WEBHOOK_SECRET=... ./scripts/smoke.sh
set -u
: "${BASE_URL:?}"; : "${HEALTH_WEBHOOK_SECRET:?}"
code() { curl -s -o /tmp/smoke.out -w "%{http_code}" "$@"; }
ok=0; chk() { if [ "$2" = "$3" ]; then echo "PASS $1 ($2)"; else echo "FAIL $1 (got $2, want $3)"; ok=1; fi; }
P='{"timestamp":"2026-09-19T12:00:00Z","app_version":"smoke","steps":[{"count":123,"start_time":"2026-09-19T10:00:00Z","end_time":"2026-09-19T11:00:00Z"}],"heart_rate":[{"time":"2026-09-19T11:00:00Z","avg":62,"min":55,"max":70,"bpm":62}]}'
chk "no secret"     "$(code -X POST -H 'Content-Type: application/json' -d "$P" $BASE_URL/api/ingest)" 401
chk "wrong secret"  "$(code -X POST -H 'Content-Type: application/json' -H 'X-Webhook-Secret: nope' -d "$P" $BASE_URL/api/ingest)" 401
chk "malformed"     "$(code -X POST -H 'Content-Type: application/json' -H "X-Webhook-Secret: $HEALTH_WEBHOOK_SECRET" -d '{"steps":"x"}' $BASE_URL/api/ingest)" 400
chk "valid ingest"  "$(code -X POST -H 'Content-Type: application/json; charset=utf-8' -H "X-Webhook-Secret: $HEALTH_WEBHOOK_SECRET" -d "$P" $BASE_URL/api/ingest)" 200
chk "read latest"   "$(code -H "X-Webhook-Secret: $HEALTH_WEBHOOK_SECRET" "$BASE_URL/api/latest?type=steps")" 200
chk "read no secret" "$(code "$BASE_URL/api/latest?type=steps")" 401
exit $ok
