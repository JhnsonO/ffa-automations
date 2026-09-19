# health-webhook

Minimal public receiver for the Android **Health Connect Webhook** app (Johnson OS ingestion).
Vercel serverless + Upstash Redis (REST). Zero npm dependencies. Isolated from the Hevy bridge.

| Route | Method | Purpose |
|---|---|---|
| `/api/ingest` | POST | App posts health JSON here. Requires `X-Webhook-Secret`. |
| `/api/latest` | GET | Johnson OS reads back. Same header. `?type=sleep`, or `?raw=1&n=5`, or no params for an index. |

## Storage keys (Redis)
- `hc:latest:<type>` latest payload slice per data type (`received_at`, `count`, `records`)
- `hc:types` set of stored types
- `hc:raw` rolling list of last 50 raw payloads

## Env vars (Vercel project)
- `HEALTH_WEBHOOK_SECRET` (min 16 chars; endpoint fails closed if unset)
- `KV_REST_API_URL` / `KV_REST_API_TOKEN` (auto-injected by Upstash Marketplace) or `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`

## Deploy
Vercel project root directory: `health-webhook`. Then `BASE_URL=... HEALTH_WEBHOOK_SECRET=... ./scripts/smoke.sh`.

Status codes: 200 stored, 400 malformed, 401 bad/missing secret, 405 wrong method, 413 too large, 502 storage down (app retries).
