'use strict';
const { isAuthorised } = require('../lib/auth');
const { pipeline, k } = require('../lib/redis');

// GET /api/latest            -> list of stored types with received_at
// GET /api/latest?type=sleep -> latest stored payload for that type
// GET /api/latest?raw=1&n=5  -> last n raw payloads (max 50)
module.exports = async function handler(req, res) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method not allowed' });
  }
  if (!isAuthorised(req)) return res.status(401).json({ error: 'unauthorised' });

  const q = req.query || {};
  try {
    if (q.raw) {
      const n = Math.min(Math.max(parseInt(q.n, 10) || 5, 1), 50);
      const [rows] = await pipeline([['LRANGE', k('hc:raw'), 0, n - 1]]);
      return res.status(200).json({ raw: (rows || []).map((r) => JSON.parse(r)) });
    }
    if (q.type) {
      if (!/^[a-z][a-z0-9_]{0,63}$/.test(String(q.type))) return res.status(400).json({ error: 'invalid type' });
      const [val] = await pipeline([['GET', k(`hc:latest:${q.type}`)]]);
      if (!val) return res.status(404).json({ error: 'no data for type' });
      return res.status(200).json(JSON.parse(val));
    }
    const [types] = await pipeline([['SMEMBERS', k('hc:types')]]);
    const names = (types || []).sort();
    const vals = names.length ? await pipeline(names.map((t) => ['GET', k(`hc:latest:${t}`)])) : [];
    const index = names.map((t, i) => {
      const o = vals[i] ? JSON.parse(vals[i]) : null;
      return { type: t, received_at: o && o.received_at, count: o && o.count };
    });
    return res.status(200).json({ types: index });
  } catch (e) {
    console.error('read failure:', e.message);
    return res.status(502).json({ error: 'storage unavailable' });
  }
};
