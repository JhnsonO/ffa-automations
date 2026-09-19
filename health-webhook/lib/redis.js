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

module.exports = { pipeline };
