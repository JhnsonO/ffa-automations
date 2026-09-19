'use strict';

// Tiny Upstash Redis REST client (no dependencies). Works with Vercel Marketplace
// env names (KV_REST_API_*) or Upstash's own (UPSTASH_REDIS_REST_*).
function creds() {
  const url = process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN;
  if (!url || !token) throw new Error('Redis env vars not configured');
  return { url: url.replace(/\/$/, ''), token };
}

async function pipeline(commands) {
  const { url, token } = creds();
  const res = await fetch(`${url}/pipeline`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(commands),
  });
  if (!res.ok) throw new Error(`Redis HTTP ${res.status}`);
  const out = await res.json();
  const failed = out.find((r) => r && r.error);
  if (failed) throw new Error(`Redis error: ${failed.error}`);
  return out.map((r) => r.result);
}

// Optional key prefix so a Preview deployment can share a database with Production
// without touching its data (Production leaves HC_KEY_PREFIX unset).
const k = (name) => (process.env.HC_KEY_PREFIX || '') + name;

// Run many commands in bounded batches (keeps each request small).
async function pipelineBatched(commands, size = 100) {
  const out = [];
  for (let i = 0; i < commands.length; i += size) {
    out.push(...(await pipeline(commands.slice(i, i + size))));
  }
  return out;
}

module.exports = { pipeline, pipelineBatched, k };
