'use strict';
// Minimal single-user OAuth 2.1 authorization server for the MCP endpoint
// (authorization code + PKCE S256, DCR, refresh rotation, HMAC-signed access tokens).
const crypto = require('crypto');
const { pipeline, k } = require('./redis');

const SCOPE = 'checkin.read';
const ACCESS_TTL = 3600;
const REFRESH_TTL = 30 * 86400;
const CODE_TTL = 120;
const TXN_TTL = 600;
// DCR abuse controls: unused clients expire fast, active ones slide, total is hard-capped.
const UNUSED_CLIENT_TTL = 86400;        // registered but never exchanged a code
const ACTIVE_CLIENT_TTL = 30 * 86400;   // sliding: refreshed on every successful token grant
const posInt = (v, d) => (Number(v) > 0 ? Math.floor(Number(v)) : d);
const clientCap = () => posInt(process.env.OAUTH_CLIENT_CAP, 25);
const dcrPerHour = () => posInt(process.env.OAUTH_DCR_PER_HOUR, 5);
const ALLOWED_REDIRECT_HOSTS = ['chatgpt.com', 'chat.openai.com'];

function base() {
  const b = process.env.PUBLIC_BASE_URL;
  if (!b || !/^https:\/\/[^/]+$/.test(b.replace(/\/+$/, ''))) throw new Error('PUBLIC_BASE_URL must be an https origin');
  return b.replace(/\/+$/, '');
}
const resource = () => `${base()}/mcp`;
function secret() {
  const s = process.env.OAUTH_SIGNING_SECRET;
  if (!s || s.length < 32) throw new Error('OAUTH_SIGNING_SECRET missing or too short');
  return s;
}
const sha256hex = (s) => crypto.createHash('sha256').update(s).digest('hex');
const random = (n = 32) => crypto.randomBytes(n).toString('base64url');
const now = () => Math.floor(Date.now() / 1000);

function sign(obj) {
  const p = Buffer.from(JSON.stringify(obj)).toString('base64url');
  return `${p}.${crypto.createHmac('sha256', secret()).update(p).digest('base64url')}`;
}
function unsign(tok) {
  if (typeof tok !== 'string') return null;
  const parts = tok.split('.'); if (parts.length !== 2) return null;
  const expect = crypto.createHmac('sha256', secret()).update(parts[0]).digest();
  let got; try { got = Buffer.from(parts[1], 'base64url'); } catch { return null; }
  if (got.length !== expect.length || !crypto.timingSafeEqual(got, expect)) return null;
  try { return JSON.parse(Buffer.from(parts[0], 'base64url').toString('utf8')); } catch { return null; }
}

function issueAccessToken(clientId) {
  const t = now();
  return sign({ typ: 'at', iss: base(), aud: resource(), sub: 'owner', scope: SCOPE, client_id: clientId, iat: t, exp: t + ACCESS_TTL });
}
function verifyAccessToken(token) {
  const p = unsign(token);
  if (!p || p.typ !== 'at' || p.iss !== base() || p.aud !== resource() || typeof p.exp !== 'number' || p.exp <= now()) return null;
  if (!String(p.scope || '').split(' ').includes(SCOPE)) return null;
  return p;
}
const signTxn = (o) => sign({ ...o, typ: 'txn', exp: now() + TXN_TTL });
function verifyTxn(tok) { const p = unsign(tok); return p && p.typ === 'txn' && p.exp > now() ? p : null; }

function isAllowedRedirect(uri) {
  let u; try { u = new URL(uri); } catch { return false; }
  if (u.hash) return false;
  if (u.protocol === 'https:' && ALLOWED_REDIRECT_HOSTS.includes(u.hostname)) return true;
  return process.env.ALLOW_LOCALHOST_REDIRECT === '1' && (u.hostname === 'localhost' || u.hostname === '127.0.0.1');
}

async function loadClient(id) {
  if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{16,64}$/.test(id)) return null;
  const [v] = await pipeline([['GET', k(`oauth:client:${id}`)]]);
  return v ? JSON.parse(v) : null;
}

async function rateLimit(bucket, ip, limit, windowSec) {
  const key = k(`oauth:rl:${bucket}:${sha256hex(String(ip)).slice(0, 16)}`);
  const [n] = await pipeline([['INCR', key], ['EXPIRE', key, windowSec, 'NX']]);
  return Number(n) <= limit;
}

function passphraseOk(given) {
  const expected = process.env.CHECKIN_PASSPHRASE;
  if (!expected || expected.length < 16 || typeof given !== 'string' || !given) return false;
  return crypto.timingSafeEqual(crypto.createHash('sha256').update(given).digest(), crypto.createHash('sha256').update(expected).digest());
}

// Index of live clients: sorted set scored by expiry time, so expired entries are pruned on demand.
async function reserveClientSlot() {
  const [, n] = await pipeline([['ZREMRANGEBYSCORE', k('oauth:clients'), '-inf', now()], ['ZCARD', k('oauth:clients')]]);
  return Number(n) < clientCap();
}
const saveClient = (id, rec) => pipeline([
  ['SET', k(`oauth:client:${id}`), JSON.stringify(rec), 'EX', UNUSED_CLIENT_TTL],
  ['ZADD', k('oauth:clients'), now() + UNUSED_CLIENT_TTL, id],
]);
const touchClient = (id) => pipeline([
  ['EXPIRE', k(`oauth:client:${id}`), ACTIVE_CLIENT_TTL],
  ['ZADD', k('oauth:clients'), now() + ACTIVE_CLIENT_TTL, id],
]);

const pkceMatches = (verifier, challenge) =>
  typeof verifier === 'string' && /^[A-Za-z0-9._~-]{43,128}$/.test(verifier) &&
  crypto.createHash('sha256').update(verifier).digest('base64url') === challenge;

module.exports = {
  SCOPE, ACCESS_TTL, REFRESH_TTL, CODE_TTL, UNUSED_CLIENT_TTL, ACTIVE_CLIENT_TTL, dcrPerHour, reserveClientSlot, saveClient, touchClient, base, resource, sha256hex, random,
  issueAccessToken, verifyAccessToken, signTxn, verifyTxn, isAllowedRedirect, loadClient, rateLimit, passphraseOk, pkceMatches, now,
};
