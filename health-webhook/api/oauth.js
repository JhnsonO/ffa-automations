'use strict';
const O = require('../lib/oauth');
const { pipeline, k } = require('../lib/redis');

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ipOf = (req) => String(req.headers['x-forwarded-for'] || '').split(',')[0].trim() || 'unknown';
function params(req) {
  const b = req.body;
  if (b && typeof b === 'object' && !Buffer.isBuffer(b)) return b;
  if (typeof b === 'string') return Object.fromEntries(new URLSearchParams(b));
  if (Buffer.isBuffer(b)) return Object.fromEntries(new URLSearchParams(b.toString('utf8')));
  return {};
}
const oauthErr = (res, status, error, desc) => {
  res.setHeader('Cache-Control', 'no-store');
  return res.status(status).json({ error, error_description: desc });
};

function page(res, status, body) {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://chatgpt.com https://chat.openai.com; frame-ancestors 'none'");
  res.setHeader('X-Frame-Options', 'DENY');
  return res.status(status).send(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Johnson OS</title>
<style>body{font:16px system-ui;max-width:420px;margin:12vh auto;padding:0 20px;color:#111}input,button{font:inherit;padding:10px;width:100%;box-sizing:border-box;margin-top:10px}button{background:#111;color:#fff;border:0;border-radius:6px}.e{color:#b00020}</style>${body}`);
}
function form(txn, msg) {
  return `<h2>Johnson OS</h2><p>ChatGPT is asking for <b>read-only</b> access to your daily health &amp; training check-in summary.</p>${msg ? `<p class="e">${esc(msg)}</p>` : ''}
<form method="post" action="/oauth/authorize"><input type="hidden" name="txn" value="${esc(txn)}"><input type="password" name="passphrase" placeholder="Passphrase" autocomplete="current-password" autofocus required><button type="submit">Approve</button></form>`;
}
function redirectTo(res, uri, extra) {
  const u = new URL(uri);
  for (const [key, v] of Object.entries(extra)) if (v !== undefined && v !== null) u.searchParams.set(key, v);
  u.searchParams.set('iss', O.base()); // RFC 9207: iss on every authorization response, incl. errors
  res.setHeader('Cache-Control', 'no-store'); res.setHeader('Location', u.toString());
  return res.status(302).end();
}

async function register(req, res) {
  if (req.method !== 'POST') return oauthErr(res, 405, 'invalid_request', 'POST required');
  if (!(await O.rateLimit('reg', ipOf(req), O.dcrPerHour(), 3600))) return oauthErr(res, 429, 'slow_down', 'too many registrations');
  const b = params(req);
  const uris = b.redirect_uris;
  if (!Array.isArray(uris) || uris.length === 0 || uris.length > 5 || !uris.every((u) => typeof u === 'string' && O.isAllowedRedirect(u))) {
    return oauthErr(res, 400, 'invalid_redirect_uri', 'redirect_uris must be allowed https ChatGPT callback URLs');
  }
  const method = b.token_endpoint_auth_method;
  if (method !== undefined && method !== 'none') return oauthErr(res, 400, 'invalid_client_metadata', 'only token_endpoint_auth_method "none" (PKCE public client) is supported');
  if (!(await O.reserveClientSlot())) { res.setHeader('Retry-After', '3600'); return oauthErr(res, 503, 'temporarily_unavailable', 'client registration limit reached'); }
  const clientId = O.random(24);
  const rec = { redirect_uris: uris, client_name: typeof b.client_name === 'string' ? b.client_name.slice(0, 80) : 'client', created: O.now() };
  await O.saveClient(clientId, rec);
  res.setHeader('Cache-Control', 'no-store');
  return res.status(201).json({
    client_id: clientId, client_id_issued_at: rec.created, client_name: rec.client_name, redirect_uris: uris,
    token_endpoint_auth_method: 'none', grant_types: ['authorization_code', 'refresh_token'], response_types: ['code'], scope: O.SCOPE,
  });
}

async function validateAuthRequest(q) {
  const client = await O.loadClient(q.client_id);
  if (!client || typeof q.redirect_uri !== 'string' || !client.redirect_uris.includes(q.redirect_uri)) return { fatal: 'Unknown client or redirect URI.' };
  const state = typeof q.state === 'string' ? q.state.slice(0, 512) : undefined;
  const bad = (error, desc) => ({ redirectErr: { error, error_description: desc, state } });
  if (q.response_type !== 'code') return bad('unsupported_response_type', 'response_type must be code');
  if (typeof q.code_challenge !== 'string' || !/^[A-Za-z0-9_-]{43,128}$/.test(q.code_challenge) || q.code_challenge_method !== 'S256') return bad('invalid_request', 'PKCE with S256 is required');
  if (q.resource !== undefined && q.resource !== O.resource()) return bad('invalid_target', 'unknown resource');
  if (q.scope !== undefined && !String(q.scope).split(' ').every((s) => s === O.SCOPE)) return bad('invalid_scope', 'unsupported scope');
  return { ok: { cid: q.client_id, ru: q.redirect_uri, cc: q.code_challenge, st: state, res: O.resource() } };
}

async function authorize(req, res) {
  if (req.method === 'GET') {
    const v = await validateAuthRequest(req.query || {});
    if (v.fatal) return page(res, 400, `<h2>Cannot continue</h2><p>${esc(v.fatal)}</p>`);
    if (v.redirectErr) return redirectTo(res, req.query.redirect_uri, v.redirectErr);
    return page(res, 200, form(O.signTxn(v.ok)));
  }
  if (req.method !== 'POST') { res.setHeader('Allow', 'GET, POST'); return page(res, 405, '<h2>Method not allowed</h2>'); }
  const p = params(req);
  const txn = O.verifyTxn(p.txn);
  if (!txn) return page(res, 400, '<h2>Session expired</h2><p>Start the connection again from ChatGPT.</p>');
  const client = await O.loadClient(txn.cid);
  if (!client || !client.redirect_uris.includes(txn.ru)) return page(res, 400, '<h2>Cannot continue</h2>');
  if (!(await O.rateLimit('auth', ipOf(req), 8, 900))) return page(res, 429, '<h2>Too many attempts</h2><p>Try again in 15 minutes.</p>');
  if (!O.passphraseOk(p.passphrase)) return page(res, 401, form(p.txn, 'Wrong passphrase.'));
  const code = O.random(32);
  await pipeline([['SET', k(`oauth:code:${O.sha256hex(code)}`), JSON.stringify({ cid: txn.cid, ru: txn.ru, cc: txn.cc, res: txn.res, exp: O.now() + O.CODE_TTL }), 'EX', O.CODE_TTL]]);
  return redirectTo(res, txn.ru, { code, state: txn.st });
}

async function mintTokens(clientId) {
  const refresh = O.random(32);
  await pipeline([['SET', k(`oauth:refresh:${O.sha256hex(refresh)}`), JSON.stringify({ cid: clientId }), 'EX', O.REFRESH_TTL]]);
  return { access_token: O.issueAccessToken(clientId), token_type: 'Bearer', expires_in: O.ACCESS_TTL, refresh_token: refresh, scope: O.SCOPE };
}

async function token(req, res) {
  if (req.method !== 'POST') return oauthErr(res, 405, 'invalid_request', 'POST required');
  if (!(await O.rateLimit('tok', ipOf(req), 60, 900))) return oauthErr(res, 429, 'slow_down', 'too many requests');
  const p = params(req);
  const client = await O.loadClient(p.client_id);
  if (!client) return oauthErr(res, 401, 'invalid_client', 'unknown client_id');
  if (p.resource !== undefined && p.resource !== O.resource()) return oauthErr(res, 400, 'invalid_target', 'unknown resource');
  let out;
  if (p.grant_type === 'authorization_code') {
    if (typeof p.code !== 'string') return oauthErr(res, 400, 'invalid_request', 'code required');
    const [raw] = await pipeline([['GETDEL', k(`oauth:code:${O.sha256hex(p.code)}`)]]); // single use
    const rec = raw ? JSON.parse(raw) : null;
    if (!rec || rec.exp <= O.now() || rec.cid !== p.client_id || rec.ru !== p.redirect_uri || !O.pkceMatches(p.code_verifier, rec.cc)) {
      return oauthErr(res, 400, 'invalid_grant', 'code invalid, expired, or PKCE/redirect mismatch');
    }
    out = await mintTokens(p.client_id);
  } else if (p.grant_type === 'refresh_token') {
    if (typeof p.refresh_token !== 'string') return oauthErr(res, 400, 'invalid_request', 'refresh_token required');
    const [raw] = await pipeline([['GETDEL', k(`oauth:refresh:${O.sha256hex(p.refresh_token)}`)]]); // rotation
    const rec = raw ? JSON.parse(raw) : null;
    if (!rec || rec.cid !== p.client_id) return oauthErr(res, 400, 'invalid_grant', 'refresh token invalid or already used');
    out = await mintTokens(p.client_id);
  } else {
    return oauthErr(res, 400, 'unsupported_grant_type', 'authorization_code or refresh_token');
  }
  try { await O.touchClient(p.client_id); } catch (e) { console.error('client touch failed:', e.message); }
  res.setHeader('Cache-Control', 'no-store'); res.setHeader('Pragma', 'no-cache');
  return res.status(200).json(out);
}

// Routed by vercel.json rewrites: /oauth/register|authorize|token -> /api/oauth?op=...
module.exports = async function handler(req, res) {
  const op = (req.query || {}).op;
  if (op === 'register' || op === 'token') {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    if (req.method === 'OPTIONS') return res.status(204).end();
  }
  try {
    if (op === 'register') return await register(req, res);
    if (op === 'authorize') return await authorize(req, res);
    if (op === 'token') return await token(req, res);
    return res.status(404).json({ error: 'not found' });
  } catch (e) {
    console.error('oauth failure:', e.message);
    return oauthErr(res, 500, 'server_error', 'temporarily unavailable');
  }
};
