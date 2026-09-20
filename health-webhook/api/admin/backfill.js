'use strict';
// TEMPORARY: replays hc:raw (oldest -> newest) through the aggregation. Idempotent (HSET per record key).
// POST /api/admin/backfill?dry=1 -> counts only. ?reset=1 first DELs the summed-type day hashes (steps, distance,
// calories, hydration) that the replay will rewrite; only safe while hc:raw still holds every affected day. Remove this file after the one-off run.
const { isAuthorised } = require('../../lib/auth');
const { pipeline, pipelineBatched, k } = require('../../lib/redis');
const { buildDayWrites, writeAggregates, SUM_TYPES } = require('../../lib/aggregate');

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') { res.setHeader('Allow', 'POST'); return res.status(405).json({ error: 'method not allowed' }); }
  if (!isAuthorised(req)) return res.status(401).json({ error: 'unauthorised' });
  const dry = String((req.query || {}).dry || '') === '1';
  try {
    const [len] = await pipeline([['LLEN', k('hc:raw')]]);
    const total = Number(len) || 0;
    const tally = { payloads: 0, records: 0, skipped: 0, hashes_touched: 0, deleted: 0, days: {} };
    const reset = String((req.query || {}).reset || '') === '1';
    // hc:raw is newest-first; page from the oldest end, 3 payloads at a time.
    async function replay(handle) {
      for (let end = total - 1; end >= 0; end -= 3) {
        const start = Math.max(0, end - 2);
        const [rows] = await pipeline([['LRANGE', k('hc:raw'), start, end]]);
        for (const row of (rows || []).slice().reverse()) {
          const body = (JSON.parse(row) || {}).payload; if (body) await handle(body);
        }
      }
    }
    if (reset && !dry) {
      const doomed = new Set();
      await replay(async (body) => {
        for (const key of Object.keys(buildDayWrites(body).hashes)) if (SUM_TYPES.includes(key.split(':')[3])) doomed.add(key);
      });
      if (doomed.size) await pipelineBatched([...doomed].map((key) => ['DEL', k(key)]));
      tally.deleted = doomed.size;
    }
    await replay(async (body) => {
      tally.payloads++;
      const b = buildDayWrites(body);
      tally.records += b.records; tally.skipped += b.skipped;
      for (const key of Object.keys(b.hashes)) {
        const [, , date, type] = key.split(':');
        tally.days[date] = tally.days[date] || {};
        tally.days[date][type] = (tally.days[date][type] || 0) + Object.keys(b.hashes[key]).length;
      }
      if (!dry) tally.hashes_touched += (await writeAggregates(body)).hashes;
    });
    return res.status(200).json({ ok: true, dry, ...tally });
  } catch (e) {
    console.error('backfill failure:', e.message);
    return res.status(502).json({ error: 'backfill failed' });
  }
};
