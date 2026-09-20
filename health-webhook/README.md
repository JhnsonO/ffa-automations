# health-webhook

Minimal public receiver for the Android **Health Connect Webhook** app (Johnson OS ingestion).
Vercel serverless + Upstash Redis (REST). Zero npm dependencies. Isolated from the Hevy bridge.

| Route | Method | Purpose |
|---|---|---|
| `/api/ingest` | POST | App posts health JSON here. Requires `X-Webhook-Secret`. |
| `/api/daily` | GET | Per-day rollups (Europe/London days), `?days=7` (max 14). Same header. |
| `/mcp` | POST | Read-only MCP server, one tool `get_checkin`. OAuth-protected (see below). |
| `/.well-known/oauth-*`, `/oauth/{register,authorize,token}` | | Minimal single-user OAuth 2.1 server (PKCE S256, DCR, refresh rotation) for the MCP endpoint. |
| `/api/latest` | GET | Johnson OS reads back. Same header. `?type=sleep`, or `?raw=1&n=5`, or no params for an index. |

## Storage keys (Redis)
- `hc:latest:<type>` latest payload slice per data type (`received_at`, `count`, `records`)
- `hc:types` set of stored types
- `hc:raw` rolling list of last 50 raw payloads

## Daily aggregates
- One Redis **hash** per type per local day: `hc:day:<YYYY-MM-DD>:<type>`, one field per record, written with multi-field `HSET`; `EXPIRE` refreshed to 90 days on every affected hash. Hash fields make writes idempotent and race-free (re-sent records overwrite their own field).
- Aggregated types: steps, distance, active/total calories, hydration (summed); heart rate, HRV, SpO2, respiratory rate (avg/min/max); resting HR, weight, body fat, VO2 max, lean mass (latest); sleep, exercise (sessions). Reproductive/sensitive types are never aggregated.
- Day allocation (Europe/London, DST-aware): sleep -> date the session ends; exercise, intervals and samples -> local start/sample date. Intervals crossing midnight are not split (known limit).
- Record identity: native `id` if present, else `origin|timestamps` (never the value, so corrections overwrite). Summed types (steps, distance, calories, hydration) use `origin|start` only: the app sends cumulative day-so-far windows (same start, growing end), so the latest window replaces the earlier one. Records with identical identity collapse into one.
- Aggregation runs after raw/latest are stored and is guarded: a failure never fails ingest.
- `npm test` runs unit tests (DST boundaries, identity, rollups).

## Env vars (Vercel project)
- `HC_KEY_PREFIX` optional; set to e.g. `hcp:` on Preview so it never touches Production keys (leave unset in Production)
- `HEALTH_WEBHOOK_SECRET` (min 16 chars; endpoint fails closed if unset)
- `KV_REST_API_URL` / `KV_REST_API_TOKEN` (auto-injected by Upstash Marketplace) or `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`

## Deploy
Vercel project root directory: `health-webhook`. Then `BASE_URL=... HEALTH_WEBHOOK_SECRET=... ./scripts/smoke.sh`.

Status codes: 200 stored, 400 malformed, 401 bad/missing secret, 405 wrong method, 413 too large, 502 storage down (app retries).

## MCP check-in (Phase 2)
`get_checkin` returns last night's sleep (overnight vs naps), today-so-far and yesterday, a 7-day baseline, data freshness, missing metrics, and recent Hevy workouts. Health data never leaves Upstash except in that response.
- ChatGPT cannot send API keys or custom headers to MCP servers, so `/mcp` uses OAuth 2.1: protected-resource + authorization-server metadata, DCR (public clients, redirect URIs restricted to chatgpt.com), authorization code + PKCE S256 with a passphrase consent page, `iss` on every authorization response, `resource` audience binding, 1h HMAC-signed access tokens, rotating 30d refresh tokens. Passphrase attempts are rate limited.
- `initialize`/`tools/list` are public (no data); `tools/call` without a valid token returns a tool error with `_meta["mcp/www_authenticate"]`, which triggers ChatGPT's linking UI.
- Env vars: `PUBLIC_BASE_URL` (https origin, no trailing slash), `OAUTH_SIGNING_SECRET` (>=32 chars), `CHECKIN_PASSPHRASE` (>=16 chars), `HEVY_API_KEY` (optional; Hevy section degrades gracefully), `ALLOW_LOCALHOST_REDIRECT=1` only for MCP Inspector testing.
- DCR abuse controls: registration is rate limited per IP (`OAUTH_DCR_PER_HOUR`, default 5), hard-capped on live clients (`OAUTH_CLIENT_CAP`, default 25, 503 when full), unused clients expire after 24h, and active clients use a sliding 30-day TTL refreshed on every token grant (so the daily check-in keeps its client alive). Expired clients are pruned from a sorted-set index. CIMD is OpenAI's preferred registration method and could replace DCR later.
- End-to-end check of any deployment: `node scripts/e2e.js https://<deployment>`: prompts for the passphrase and optional Vercel bypass token with hidden input (nothing lands in shell history). Non-interactive: env vars, or an untracked `.env.e2e` via `node --env-file=.env.e2e scripts/e2e.js <url>`. Secrets are never printed.
- ChatGPT connector URL: `<PUBLIC_BASE_URL>/mcp`, authentication: OAuth.
