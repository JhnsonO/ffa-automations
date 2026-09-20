'use strict';
// Read-only MCP server (Streamable HTTP, single JSON responses). One tool: get_checkin.
// initialize / tools/list are public (they expose no data); tools/call requires an OAuth access token and
// otherwise returns a tool error carrying _meta["mcp/www_authenticate"] so ChatGPT launches the linking UI.
const O = require('../lib/oauth');
const { buildCheckin } = require('../lib/checkin');

const VERSIONS = ['2025-06-18', '2025-03-26', '2024-11-05'];
const SEC = [{ type: 'oauth2', scopes: [O.SCOPE] }];
const TOOL = {
  name: 'get_checkin',
  title: 'Daily health & training check-in',
  description: 'Read-only snapshot for the morning check-in: last night\'s sleep, today so far and yesterday (steps, heart rate, resting HR, HRV, exercise, weight where available), a 7-day baseline, data freshness, missing metrics, and recent Hevy strength workouts. Takes no arguments.',
  inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  securitySchemes: SEC,
  _meta: { securitySchemes: SEC },
};

const rpcErr = (id, code, message) => ({ jsonrpc: '2.0', id: id ?? null, error: { code, message } });
const ok = (id, result) => ({ jsonrpc: '2.0', id, result });

function authError(desc) {
  const challenge = `Bearer resource_metadata="${O.base()}/.well-known/oauth-protected-resource", error="invalid_token", error_description="${desc}"`;
  return { isError: true, content: [{ type: 'text', text: 'Authentication required: connect Johnson OS to continue.' }], _meta: { 'mcp/www_authenticate': [challenge] } };
}

async function handle(m, req) {
  if (!m || typeof m !== 'object' || m.jsonrpc !== '2.0' || typeof m.method !== 'string') return rpcErr(m && m.id, -32600, 'invalid request');
  const isNote = m.id === undefined || m.id === null;
  switch (m.method) {
    case 'initialize': {
      const want = m.params && m.params.protocolVersion;
      return ok(m.id, {
        protocolVersion: VERSIONS.includes(want) ? want : VERSIONS[0],
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: 'johnson-os-health', version: '0.2.0' },
      });
    }
    case 'ping': return ok(m.id, {});
    case 'tools/list': return ok(m.id, { tools: [TOOL] });
    case 'tools/call': {
      if (!m.params || m.params.name !== TOOL.name) return rpcErr(m.id, -32602, 'unknown tool');
      const h = String(req.headers.authorization || '');
      const tok = h.toLowerCase().startsWith('bearer ') ? h.slice(7).trim() : '';
      let claims = null;
      try { claims = tok ? O.verifyAccessToken(tok) : null; } catch (e) { console.error('token verify:', e.message); }
      if (!claims) return ok(m.id, authError(tok ? 'The access token is invalid or expired' : 'No access token provided'));
      try {
        const data = await buildCheckin();
        return ok(m.id, { isError: false, content: [{ type: 'text', text: JSON.stringify(data) }], structuredContent: data });
      } catch (e) {
        console.error('checkin failure:', e.message);
        return ok(m.id, { isError: true, content: [{ type: 'text', text: 'Check-in temporarily unavailable.' }] });
      }
    }
    default:
      if (isNote && m.method.startsWith('notifications/')) return null;
      return rpcErr(m.id, -32601, 'method not found');
  }
}

module.exports = async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, Mcp-Session-Id, MCP-Protocol-Version');
  res.setHeader('Access-Control-Expose-Headers', 'WWW-Authenticate');
  if (req.method === 'OPTIONS') return res.status(204).end();
  if (req.method !== 'POST') { res.setHeader('Allow', 'POST, OPTIONS'); return res.status(405).json({ error: 'method not allowed' }); }
  let body = req.body;
  if (typeof body === 'string' || Buffer.isBuffer(body)) { try { body = JSON.parse(String(body)); } catch { return res.status(400).json(rpcErr(null, -32700, 'parse error')); } }
  if (!body || typeof body !== 'object') return res.status(400).json(rpcErr(null, -32600, 'invalid request'));
  try { O.base(); } catch (e) { console.error(e.message); return res.status(500).json(rpcErr(null, -32603, 'server misconfigured')); }
  const batch = Array.isArray(body);
  const out = [];
  for (const m of batch ? body : [body]) { const r = await handle(m, req); if (r) out.push(r); }
  if (!out.length) return res.status(202).end();
  return res.status(200).json(batch ? out : out[0]);
};
