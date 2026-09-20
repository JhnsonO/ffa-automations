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
const CLIENT_TTL = 365 * 86400;
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

const pkceMatches = (verifier, challenge) =>
  typeof verifier === 'string' && /^[A-Za-z0-9._~-]{43,128}$/.test(verifier) &&
  crypto.createHash('sha256').update(verifier).digest('base64url') === challenge;

module.exports = {
  SCOPE, ACCESS_TTL, REFRESH_TTL, CODE_TTL, CLIENT_TTL, base, resource, sha256hex, random,
  issueAccessToken, verifyAccessToken, signTxn, verifyTxn, isAllowedRedirect, loadClient, rateLimit, passphraseOk, pkceMatches, now,
};
