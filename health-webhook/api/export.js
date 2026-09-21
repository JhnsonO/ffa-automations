'use strict';
const crypto = require('crypto');
const { buildCheckin } = require('../lib/checkin');

function authorised(req) {
  const expected = process.env.CHECKIN_EXPORT_KEY;
  if (!expected) return false;
  const header = String(req.headers.authorization || '');
  const supplied = header.toLowerCase().startsWith('bearer ') ? header.slice(7).trim() : '';
  if (!supplied) return false;
  const a = Buffer.from(supplied);
  const b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

module.exports = async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    return res.status(405).json({ error: 'method not allowed' });
  }
  if (!process.env.CHECKIN_EXPORT_KEY) {
    console.error('CHECKIN_EXPORT_KEY is not configured');
    return res.status(500).json({ error: 'server misconfigured' });
  }
  if (!authorised(req)) return res.status(401).json({ error: 'unauthorized' });
  try {
    return res.status(200).json(await buildCheckin());
  } catch (error) {
    console.error('checkin export failure:', error.message);
    return res.status(503).json({ error: 'check-in temporarily unavailable' });
  }
};
