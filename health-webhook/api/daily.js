'use strict';
const { isAuthorised } = require('../lib/auth');
const { getDailyRollups } = require('../lib/daily');

// GET /api/daily?days=7 -> per-day rollups (Europe/London days), newest first. Missing metrics are omitted.
module.exports = async function handler(req, res) {
  if (req.method !== 'GET') { res.setHeader('Allow', 'GET'); return res.status(405).json({ error: 'method not allowed' }); }
  if (!isAuthorised(req)) return res.status(401).json({ error: 'unauthorised' });
  const days = Math.min(Math.max(parseInt((req.query || {}).days, 10) || 7, 1), 14);
  try {
    const { byDate } = await getDailyRollups(days);
    return res.status(200).json({ days: byDate });
  } catch (e) {
    console.error('daily failure:', e.message);
    return res.status(502).json({ error: 'storage unavailable' });
  }
};
