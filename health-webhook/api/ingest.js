'use strict';
const { isAuthorised } = require('../lib/auth');
const { validatePayload, MAX_BODY_BYTES } = require('../lib/validate');
const { pipeline, k } = require('../lib/redis');
const { writeAggregates } = require('../lib/aggregate');

const RAW_KEEP = 50; // rolling list of raw payloads

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method not allowed' });
  }
  if (!isAuthorised(req)) return res.status(401).json({ error: 'unauthorised' });

  const declared = Number(req.headers['content-length'] || 0);
  if (declared > MAX_BODY_BYTES) return res.status(413).json({ error: 'payload too large' });

  let body = req.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch { return res.status(400).json({ error: 'invalid JSON' }); }
  }
  if (Buffer.isBuffer(body)) {
    try { body = JSON.parse(body.toString('utf8')); } catch { return res.status(400).json({ error: 'invalid JSON' }); }
  }
  const raw = JSON.stringify(body ?? null);
  if (raw.length > MAX_BODY_BYTES) return res.status(413).json({ error: 'payload too large' });

  const v = validatePayload(body);
  if (!v.ok) return res.status(400).json({ error: v.error });

  const receivedAt = new Date().toISOString();
  const cmds = [
    ['LPUSH', k('hc:raw'), JSON.stringify({ received_at: receivedAt, payload: body })],
    ['LTRIM', k('hc:raw'), 0, RAW_KEEP - 1],
  ];
  const names = Object.keys(v.types);
  for (const name of names) {
    cmds.push(['SET', k(`hc:latest:${name}`), JSON.stringify({
      received_at: receivedAt,
      payload_timestamp: body.timestamp,
      app_version: body.app_version,
      count: v.types[name].length,
      records: v.types[name],
    })]);
    cmds.push(['SADD', k('hc:types'), name]);
  }

  try {
    await pipeline(cmds);
  } catch (e) {
    console.error('storage failure:', e.message);
    return res.status(502).json({ error: 'storage unavailable' }); // non-2xx => app retries
  }
  // Daily aggregates: best-effort. A failure here must never fail ingest (would trigger app retries).
  let aggregated = null;
  try {
    aggregated = await writeAggregates(body);
  } catch (e) {
    console.error('aggregation failure:', e.message);
  }
  return res.status(200).json({ ok: true, stored_types: names, received_at: receivedAt, aggregated: aggregated ? { records: aggregated.records, skipped: aggregated.skipped } : false });
};
