# health-webhook

Minimal public receiver for the Android **Health Connect Webhook** app (Johnson OS ingestion).
Vercel serverless + Upstash Redis (REST). Zero npm dependencies. Isolated from the Hevy bridge.

| Route | Method | Purpose |
|---|---|---|
| `/api/ingest` | POST | App posts health JSON here. Requires `X-Webhook-Secret`. |
| `/api/daily` | GET | Per-day rollups (Europe/London days), `?days=7` (max 14). Same header. |
| `/api/admin/backfill` | POST | TEMPORARY. Replays `hc:raw` into daily aggregates; `?dry=1` counts only. Same header. Delete after the one-off run. |
| `/api/latest` | GET | Johnson OS reads back. Same header. `?type=sleep`, or `?raw=1&n=5`, or no params for an index. |

## Storage keys (Redis)
- `hc:latest:<type>` latest payload slice per data type (`received_at`, `count`, `records`)
- `hc:types` set of stored types
- `hc:raw` rolling list of last 50 raw payloads

## Daily aggregates
- One Redis **hash** per type per local day: `hc:day:<YYYY-MM-DD>:<type>`, one field per record, written with multi-field `HSET`; `EXPIRE` refreshed to 90 days on every affected hash. Hash fields make writes idempotent and race-free (re-sent records overwrite their own field).
- Aggregated types: steps, distance, active/total calories, hydration (summed); heart rate, HRV, SpO2, respiratory rate (avg/min/max); resting HR, weight, body fat, VO2 max, lean mass (latest); sleep, exercise (sessions). Reproductive/sensitive types are never aggregated.
- Day allocation (Europe/London, DST-aware): sleep -> date the session ends; exercise, intervals and samples -> local start/sample date. Intervals crossing midnight are not split (known limit).
- Record identity: native `id` if the payload carries one, else `origin|start|end` (never the value, so corrections overwrite). Records with identical origin and timestamps collapse into one.
- Aggregation runs after raw/latest are stored and is guarded: a failure never fails ingest.
- `npm test` runs unit tests (DST boundaries, identity, rollups).

## Env vars (Vercel project)
- `HC_KEY_PREFIX` optional; set to e.g. `hcp:` on Preview so it never touches Production keys (leave unset in Production)
- `HEALTH_WEBHOOK_SECRET` (min 16 chars; endpoint fails closed if unset)
- `KV_REST_API_URL` / `KV_REST_API_TOKEN` (auto-injected by Upstash Marketplace) or `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`

## Deploy
Vercel project root directory: `health-webhook`. Then `BASE_URL=... HEALTH_WEBHOOK_SECRET=... ./scripts/smoke.sh`.

Status codes: 200 stored, 400 malformed, 401 bad/missing secret, 405 wrong method, 413 too large, 502 storage down (app retries).
