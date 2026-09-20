'use strict';
const { localDate } = require('./tz');
const { pipelineBatched, k } = require('./redis');

const TTL_SECONDS = 90 * 24 * 3600;
const HSET_CHUNK = 400; // fields per HSET command

// Field -> value name per type. Reproductive/sensitive types are deliberately not aggregated.
const SUM = { steps: 'count', distance: 'meters', active_calories: 'calories', total_calories: 'calories', hydration: 'liters' };
const STAT = { heart_rate: 'bpm', heart_rate_variability: 'rmssd_millis', oxygen_saturation: 'percentage', respiratory_rate: 'rate' };
const POINT = { resting_heart_rate: 'bpm', weight: 'kilograms', body_fat: 'percentage', vo2_max: 'ml_per_kg_per_min', lean_body_mass: 'kilograms' };
const SESSION = ['sleep', 'exercise'];
const AGGREGATED_TYPES = [...Object.keys(SUM), ...Object.keys(STAT), ...Object.keys(POINT), ...SESSION];

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

// Record identity: native id if the payload carries one, else origin + timestamps
// (never the value, so a corrected record overwrites rather than double-counts).
function ident(r, parts) {
  const id = r.id ?? r.record_id ?? (r.metadata && r.metadata.id);
  if (id !== undefined && id !== null && id !== '') return `id:${id}`;
  const origin = r.data_origin ?? r.origin ?? r.source ?? (r.metadata && (r.metadata.data_origin || r.metadata.origin));
  return (origin ? `${origin}|` : '') + parts.join('|');
}

function mapRecord(type, r) {
  if (type in SUM) {
    const v = num(r[SUM[type]]);
    if (v === null || !r.start_time) return null;
    return { date: localDate(r.start_time), field: ident(r, [r.start_time, r.end_time || '']), value: String(v) };
  }
  if (type in STAT) {
    const avg = num(r.avg) ?? num(r[STAT[type]]);
    if (avg === null || !r.time) return null;
    const mn = num(r.min) ?? avg; const mx = num(r.max) ?? avg;
    return { date: localDate(r.time), field: ident(r, [r.time]), value: JSON.stringify([avg, mn, mx]) };
  }
  if (type in POINT) {
    const v = num(r[POINT[type]]);
    if (v === null || !r.time) return null;
    return { date: localDate(r.time), field: ident(r, [r.time]), value: JSON.stringify([r.time, v]) };
  }
  if (type === 'sleep') {
    const end = r.session_end_time; const dur = num(r.duration_seconds);
    if (!end || dur === null || Number.isNaN(Date.parse(end))) return null;
    const stages = Array.isArray(r.stages) ? r.stages : [];
    const starts = stages.map((s) => Date.parse(s && s.start_time)).filter((n) => !Number.isNaN(n));
    const startMs = starts.length ? Math.min(...starts) : Date.parse(end) - dur * 1000;
    const st = {};
    for (const s of stages) { const d = num(s && s.duration_seconds); if (d !== null && s.stage) st[s.stage] = (st[s.stage] || 0) + d; }
    return { date: localDate(end), field: ident(r, [new Date(startMs).toISOString()]), value: JSON.stringify({ e: end, d: dur, st }) };
  }
  if (type === 'exercise') {
    if (!r.start_time || !r.end_time) return null;
    return {
      date: localDate(r.start_time), field: ident(r, [r.start_time, r.end_time, r.type || '']),
      value: JSON.stringify({ t: r.type || null, s: r.start_time, e: r.end_time, d: num(r.duration_seconds), m: num(r.distance_meters), n: num(r.steps) }),
    };
  }
  return null;
}

// Pure: payload -> { hashes: { 'hc:day:<date>:<type>': { field: value } }, records, skipped }
function buildDayWrites(body) {
  const hashes = {}; let records = 0; let skipped = 0;
  for (const type of AGGREGATED_TYPES) {
    const arr = body[type];
    if (!Array.isArray(arr)) continue;
    for (const r of arr) {
      const m = r && typeof r === 'object' ? mapRecord(type, r) : null;
      if (!m || !m.date) { skipped++; continue; }
      const key = `hc:day:${m.date}:${type}`;
      (hashes[key] = hashes[key] || {})[m.field] = m.value;
      records++;
    }
  }
  return { hashes, records, skipped };
}

// One multi-field HSET per chunk + EXPIRE refresh on every affected hash.
async function writeAggregates(body) {
  const { hashes, records, skipped } = buildDayWrites(body);
  const cmds = [];
  for (const [key, fields] of Object.entries(hashes)) {
    const entries = Object.entries(fields);
    for (let i = 0; i < entries.length; i += HSET_CHUNK) {
      cmds.push(['HSET', k(key), ...entries.slice(i, i + HSET_CHUNK).flat()]);
    }
    cmds.push(['EXPIRE', k(key), TTL_SECONDS]);
  }
  if (cmds.length) await pipelineBatched(cmds);
  return { hashes: Object.keys(hashes).length, records, skipped, commands: cmds.length };
}

const parse = (s) => { try { return JSON.parse(s); } catch { return null; } };
const r2 = (n) => Math.round(n * 100) / 100;

// Read side: field->value map for one type-day -> summary.
function rollup(type, entries) {
  const vals = Object.values(entries || {});
  if (!vals.length) return null;
  if (type in SUM) { const a = vals.map(Number); return { sum: r2(a.reduce((x, y) => x + y, 0)), n: a.length }; }
  if (type in STAT) {
    const a = vals.map(parse).filter(Boolean);
    return { avg: r2(a.reduce((x, y) => x + y[0], 0) / a.length), min: Math.min(...a.map((x) => x[1])), max: Math.max(...a.map((x) => x[2])), n: a.length };
  }
  if (type in POINT) {
    const a = vals.map(parse).filter(Boolean).sort((x, y) => Date.parse(x[0]) - Date.parse(y[0]));
    const last = a[a.length - 1]; return { last: last[1], last_time: last[0], n: a.length };
  }
  const sessions = vals.map(parse).filter(Boolean).sort((x, y) => Date.parse(x.e) - Date.parse(y.e));
  if (type === 'sleep') return { sessions: sessions.length, total_seconds: sessions.reduce((x, s) => x + (s.d || 0), 0), stages: sessions.reduce((acc, s) => { for (const [n, d] of Object.entries(s.st || {})) acc[n] = (acc[n] || 0) + d; return acc; }, {}), last_end: sessions[sessions.length - 1].e };
  return { sessions };
}

module.exports = { buildDayWrites, writeAggregates, rollup, AGGREGATED_TYPES, TTL_SECONDS };
