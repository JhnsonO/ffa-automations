'use strict';
const test = require('node:test'); const assert = require('node:assert');
const { buildDayWrites, rollup } = require('../lib/aggregate');
const { composeCheckin } = require('../lib/checkin');
const { compactWorkouts } = require('../lib/hevy');
const { addDays } = require('../lib/tz');

function day(payload) {
  const by = {}; const b = buildDayWrites(payload);
  for (const [key, fields] of Object.entries(b.hashes)) { const [, , , type] = key.split(':'); by[type] = rollup(type, fields); }
  return by;
}

test('sleep: split overnight sessions stay overnight; afternoon sessions are naps', () => {
  const b = buildDayWrites({ sleep: [
    { session_end_time: '2026-09-18T23:37:00Z', duration_seconds: 5160, stages: [] },   // 00:37 BST on the 19th
    { session_end_time: '2026-09-19T05:24:00Z', duration_seconds: 18060, stages: [] },  // 06:24 BST
    { session_end_time: '2026-09-19T16:00:00Z', duration_seconds: 3780, stages: [] },   // 17:00 BST nap
  ] });
  assert.deepEqual(Object.keys(b.hashes), ['hc:day:2026-09-19:sleep']);
  const r = rollup('sleep', b.hashes['hc:day:2026-09-19:sleep']);
  assert.equal(r.overnight_seconds, 5160 + 18060);
  assert.equal(r.nap_seconds, 3780);
  assert.equal(r.overnight_start, '2026-09-18T22:11:00.000Z');
  assert.equal(r.overnight_end, '2026-09-19T05:24:00Z');
});

test('composeCheckin: baseline excludes today, tolerates missing metrics, lists them', () => {
  const dates = Array.from({ length: 8 }, (_, i) => addDays('2026-09-20', -i));
  const byDate = Object.fromEntries(dates.map((d) => [d, { steps: { sum: 1000, n: 1 }, heart_rate: { avg: 70, min: 50, max: 150, n: 10 } }]));
  byDate[dates[0]] = { steps: { sum: 9999, n: 1 } };
  byDate[dates[1]].resting_heart_rate = { last: 50, last_time: 't', n: 1 };
  const c = composeCheckin({ dates, byDate, hevy: { available: false, reason: 'x' }, lastSync: 'ts', now: Date.parse('2026-09-20T06:00:00Z') });
  assert.equal(c.date, '2026-09-20');
  assert.equal(c.baseline_7d.steps.avg, 1000);           // today's 9999 not included
  assert.equal(c.baseline_7d.steps.n_days, 7);
  assert.equal(c.baseline_7d.resting_hr.n_days, 1);
  assert.equal(c.baseline_7d.hrv_ms, null);
  assert.ok(c.missing_metrics.includes('hrv_ms') && c.missing_metrics.includes('weight_kg') && !c.missing_metrics.includes('resting_hr'));
  assert.equal(c.today.steps, 9999); assert.equal(c.data_last_synced_utc, 'ts');
});

test('composeCheckin: empty store does not throw', () => {
  const dates = Array.from({ length: 8 }, (_, i) => addDays('2026-09-20', -i));
  const c = composeCheckin({ dates, byDate: Object.fromEntries(dates.map((d) => [d, {}])), hevy: null, lastSync: null, now: 0 });
  assert.deepEqual(c.today, {}); assert.equal(c.baseline_7d.steps, null);
});

test('hevy: compaction keeps recent workouts, drops warmups, computes top set/volume', () => {
  const now = Date.parse('2026-09-20T06:00:00Z');
  const out = compactWorkouts([
    { title: 'Push', start_time: '2026-09-18T06:00:00Z', end_time: '2026-09-18T07:00:00Z', exercises: [{ title: 'Bench', sets: [
      { type: 'warmup', weight_kg: 20, reps: 10 }, { type: 'normal', weight_kg: 60, reps: 8 }, { type: 'normal', weight_kg: 60, reps: 6 }] }] },
    { title: 'Old', start_time: '2026-06-01T06:00:00Z', exercises: [] },
  ], 14, now);
  assert.equal(out.length, 1);
  assert.deepEqual(out[0].exercises[0], { name: 'Bench', working_sets: 2, top_set: { kg: 60, reps: 8 }, best_reps: 8, volume_kg: 840 });
  assert.equal(out[0].minutes, 60);
});
