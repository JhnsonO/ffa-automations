#!/usr/bin/env node
'use strict';
// End-to-end check of a deployed receiver's OAuth + MCP path. Zero dependencies (Node >= 18).
//   BASE_URL=https://<deployment> CHECKIN_PASSPHRASE='...' [VERCEL_BYPASS=<token>] node scripts/e2e.js
// Secrets are read from the environment and never printed. Registers one OAuth client (expires with disuse).
const crypto = require('crypto');
const BASE = (process.env.BASE_URL || '').replace(/\/+$/, '');
const PASS = process.env.CHECKIN_PASSPHRASE;
if (!BASE || !PASS) { console.error('Set BASE_URL and CHECKIN_PASSPHRASE'); process.exit(2); }
const RU = 'https://chatgpt.com/connector_platform_oauth_redirect';
const bypass = process.env.VERCEL_BYPASS ? { 'x-vercel-protection-bypass': process.env.VERCEL_BYPASS } : {};
let failed = 0;
const ok = (c, name, extra = '') => { console.log(`${c ? 'PASS' : 'FAIL'}  ${name}${extra ? '  ' + extra : ''}`); if (!c) failed++; return c; };
async function j(path, opts = {}) {
  const r = await fetch(BASE + path, { redirect: 'manual', ...opts, headers: { ...bypass, ...(opts.headers || {}) } });
  const t = await r.text(); let d; try { d = JSON.parse(t); } catch { d = t; }
  return { s: r.status, d, h: r.headers };
}
const form = (o) => ({ method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams(o).toString() });
const rpc = (method, params, tok) => j('/mcp', { method: 'POST', headers: { 'content-type': 'application/json', ...(tok ? { authorization: 'Bearer ' + tok } : {}) }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });

(async () => {
  let r = await j('/.well-known/oauth-protected-resource');
  if (!ok(r.s === 200 && r.d.resource, 'protected-resource metadata')) return;
  const RES = r.d.resource; const ISS = r.d.authorization_servers[0];
  ok(RES === `${ISS}/mcp`, 'resource = issuer + /mcp', RES);
  ok(RES.startsWith(BASE) || process.env.ALLOW_BASE_MISMATCH, 'PUBLIC_BASE_URL matches the URL being tested', `(${ISS})`);
  r = await j('/.well-known/oauth-authorization-server');
  ok(r.s === 200 && r.d.code_challenge_methods_supported.includes('S256') && r.d.registration_endpoint && r.d.authorization_response_iss_parameter_supported === true && r.d.issuer === ISS, 'authorization-server metadata (S256, DCR, iss, issuer match)');

  r = await j('/oauth/register', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ redirect_uris: [RU], client_name: 'e2e-script', token_endpoint_auth_method: 'none' }) });
  if (!ok(r.s === 201 && r.d.client_id, 'dynamic client registration', `status ${r.s}`)) return;
  const cid = r.d.client_id;

  const verifier = crypto.randomBytes(32).toString('base64url');
  const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
  const q = new URLSearchParams({ response_type: 'code', client_id: cid, redirect_uri: RU, code_challenge: challenge, code_challenge_method: 'S256', state: 'e2e', resource: RES, scope: 'checkin.read' });
  r = await j('/oauth/authorize?' + q);
  const m = r.s === 200 && /name="txn" value="([^"]+)"/.exec(r.d);
  if (!ok(!!m, 'authorize page renders', `status ${r.s}`)) return;
  r = await j('/oauth/authorize', form({ txn: m[1].replace(/&amp;/g, '&'), passphrase: PASS }));
  const loc = r.s === 302 ? new URL(r.h.get('location')) : null;
  if (!ok(!!loc && loc.searchParams.get('code') && loc.searchParams.get('state') === 'e2e' && loc.searchParams.get('iss') === ISS, 'passphrase accepted; redirect carries code, state, iss', `status ${r.s}`)) return;
  r = await j('/oauth/token', form({ grant_type: 'authorization_code', code: loc.searchParams.get('code'), client_id: cid, redirect_uri: RU, code_verifier: verifier, resource: RES }));
  if (!ok(r.s === 200 && r.d.access_token && r.d.refresh_token, 'token exchange (PKCE)', `status ${r.s}`)) return;
  const { access_token: at, refresh_token: rt } = r.d;

  r = await rpc('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'e2e', version: '1' } });
  ok(r.s === 200 && r.d.result && r.d.result.serverInfo, 'MCP initialize');
  r = await rpc('tools/list');
  const tool = r.d.result && r.d.result.tools && r.d.result.tools[0];
  ok(tool && tool.name === 'get_checkin' && tool.securitySchemes && tool.annotations.readOnlyHint === true, 'tools/list: get_checkin, oauth2 securitySchemes, readOnly');
  r = await rpc('tools/call', { name: 'get_checkin', arguments: {} });
  ok(r.d.result && r.d.result.isError === true && /resource_metadata=/.test(JSON.stringify(r.d.result._meta || {})), 'tools/call without token -> auth challenge in _meta');
  r = await rpc('tools/call', { name: 'get_checkin', arguments: {} }, at);
  const c = r.d.result && r.d.result.structuredContent;
  if (ok(r.d.result && r.d.result.isError === false && c, 'tools/call with token -> snapshot')) {
    console.log('      date:', c.date, '| last sync:', c.data_last_synced_utc);
    console.log('      today keys:', Object.keys(c.today).join(', ') || '(none yet)');
    console.log('      baseline steps:', JSON.stringify(c.baseline_7d.steps), '| missing:', c.missing_metrics.join(', ') || 'none');
    console.log('      hevy:', c.hevy && c.hevy.available ? `${c.hevy.workouts.length} recent workouts` : `unavailable (${c.hevy && c.hevy.reason})`);
  }
  r = await j('/oauth/token', form({ grant_type: 'refresh_token', refresh_token: rt, client_id: cid }));
  ok(r.s === 200 && r.d.refresh_token && r.d.refresh_token !== rt, 'refresh token rotates');
  const again = await j('/oauth/token', form({ grant_type: 'refresh_token', refresh_token: rt, client_id: cid }));
  ok(again.s === 400, 'old refresh token rejected');
})().catch((e) => { console.error('ERROR', e.message); failed++; }).finally(() => { console.log(failed ? `\n${failed} FAILED` : '\nALL PASSED'); process.exit(failed ? 1 : 0); });
