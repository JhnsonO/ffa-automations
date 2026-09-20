'use strict';
const { pipeline, k } = require('./redis');
const { rollup, AGGREGATED_TYPES } = require('./aggregate');
const { localDate, addDays } = require('./tz');

// Rollups for the last `days` Europe/London calendar days, newest first.
async function getDailyRollups(days) {
  const today = localDate(new Date().toISOString());
  const dates = Array.from({ length: days }, (_, i) => addDays(today, -i));
  const [present] = await pipeline([['SMEMBERS', k('hc:types')]]);
  const types = AGGREGATED_TYPES.filter((t) => (present || []).includes(t));
  const pairs = dates.flatMap((d) => types.map((t) => [d, t]));
  const results = pairs.length ? await pipeline(pairs.map(([d, t]) => ['HGETALL', k(`hc:day:${d}:${t}`)])) : [];
  const byDate = Object.fromEntries(dates.map((d) => [d, {}]));
  pairs.forEach(([d, t], i) => {
    const flat = results[i] || []; const entries = {};
    for (let j = 0; j < flat.length; j += 2) entries[flat[j]] = flat[j + 1];
    const r = rollup(t, entries); if (r) byDate[d][t] = r;
  });
  return { dates, byDate };
}
module.exports = { getDailyRollups };
