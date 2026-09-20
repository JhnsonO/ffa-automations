'use strict';
const { base, resource, SCOPE } = require('../lib/oauth');

// Served via vercel.json rewrites:
//  /.well-known/oauth-protected-resource[/mcp]                    -> ?doc=prm
//  /.well-known/oauth-authorization-server, /openid-configuration -> ?doc=as
module.exports = async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Cache-Control', 'public, max-age=300');
  if (req.method === 'OPTIONS') return res.status(204).end();
  if (req.method !== 'GET') { res.setHeader('Allow', 'GET, OPTIONS'); return res.status(405).json({ error: 'method not allowed' }); }
  let b; try { b = base(); } catch (e) { console.error(e.message); return res.status(500).json({ error: 'server misconfigured' }); }
  const doc = (req.query || {}).doc;
  if (doc === 'prm') {
    return res.status(200).json({
      resource: resource(),
      authorization_servers: [b],
      scopes_supported: [SCOPE],
      bearer_methods_supported: ['header'],
      resource_name: 'Johnson OS health check-in',
    });
  }
  if (doc === 'as') {
    return res.status(200).json({
      issuer: b,
      authorization_endpoint: `${b}/oauth/authorize`,
      token_endpoint: `${b}/oauth/token`,
      registration_endpoint: `${b}/oauth/register`,
      response_types_supported: ['code'],
      grant_types_supported: ['authorization_code', 'refresh_token'],
      code_challenge_methods_supported: ['S256'],
      token_endpoint_auth_methods_supported: ['none'],
      scopes_supported: [SCOPE],
      authorization_response_iss_parameter_supported: true,
    });
  }
  return res.status(404).json({ error: 'not found' });
};
