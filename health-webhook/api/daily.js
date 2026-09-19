'use strict';
const { isAuthorised } = require('../lib/auth');
const { pipeline, k } = require('../lib/redis');
const { rollup, AGGREGATED_TYPES } = require('../lib/aggregate');
const { localDate } = require('../lib/tz');

// GET /api/daily?days=7 -> per-day rollups (Europe/London days), newest first. Missing metrics are omitted.
module.exports = async function handler(req, res) {
  if (req.method !== 'GET') { res.setHeader('Allow', 'GET'); return res.status(405).json({ error: 'method not allowed' }); }
  if (!isAuthorised(req)) return res.status(401).json({ error: 'unauthorised' });
  const days = Math.min(Math.max(parseInt((req.query || {}).days, 10) || 7, 1), 14);
  try {
    const [present] = await pipeline([['SMEMBERS', k('hc:types')]]);
    const types = AGGREGATED_TYPES.filter((t) => (present || []).includes(t));
    const dates = [];
    for (let i = 0; i < days; i++) dates.push(localDate(new Date(Date.now() - i * 86400000).toISOString()));
    const pairs = dates.flatMap((d) => types.map((t) => [d, t]));
    const results = pairs.length ? await pipeline(pairs.map(([d, t]) => ['HGETALL', k(`hc:day:${d}:${t}`)])) : [];
    const out = Object.fromEntries(dates.map((d) => [d, {}]));
    pairs.forEach(([d, t], i) => {
      const flat = results[i] || []; const entries = {};
      for (let j = 0; j < flat.length; j += 2) entries[flat[j]] = flat[j + 1];
      const r = rollup(t, entries); if (r) out[d][t] = r;
    });
    return res.status(200).json({ days: out });
  } catch (e) {
    console.error('daily failure:', e.message);
    return res.status(502).json({ error: 'storage unavailable' });
  }
};
