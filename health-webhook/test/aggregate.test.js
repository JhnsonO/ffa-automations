'use strict';
const test = require('node:test'); const assert = require('node:assert');
const { buildDayWrites, rollup } = require('../lib/aggregate');

const merge = (a, b) => { for (const [k, f] of Object.entries(b.hashes)) a[k] = { ...(a[k] || {}), ...f }; return a; };

test('sleep belongs to the local date it ends (spans midnight)', () => {
  const b = buildDayWrites({ sleep: [{ session_end_time: '2026-09-19T06:30:00Z', duration_seconds: 28800,
    stages: [{ stage: 'deep', start_time: '2026-09-18T22:30:00Z', end_time: '2026-09-19T00:30:00Z', duration_seconds: 7200 }] }] });
  assert.deepEqual(Object.keys(b.hashes), ['hc:day:2026-09-19:sleep']);
});
test('sleep ending 00:30 BST on 25 Oct lands on the 25th', () => {
  const b = buildDayWrites({ sleep: [{ session_end_time: '2026-10-24T23:30:00Z', duration_seconds: 3600, stages: [] }] });
  assert.deepEqual(Object.keys(b.hashes), ['hc:day:2026-10-25:sleep']);
});
test('exercise uses local start date', () => {
  const b = buildDayWrites({ exercise: [{ type: 'RUNNING', start_time: '2026-09-19T23:30:00Z', end_time: '2026-09-20T00:30:00Z', duration_seconds: 3600 }] });
  assert.deepEqual(Object.keys(b.hashes), ['hc:day:2026-09-20:exercise']); // 00:30 BST on the 20th
});
test('corrected interval overwrites (no double count); value not part of identity', () => {
  const acc = {};
  merge(acc, buildDayWrites({ steps: [{ count: 100, start_time: '2026-09-19T00:00:00Z', end_time: '2026-09-19T12:00:00Z' }] }));
  merge(acc, buildDayWrites({ steps: [{ count: 250, start_time: '2026-09-19T00:00:00Z', end_time: '2026-09-19T12:00:00Z' }] }));
  assert.equal(rollup('steps', acc['hc:day:2026-09-19:steps']).sum, 250);
});
test('native id preferred; distinct origins do not collide', () => {
  const acc = {};
  const rec = (origin, count) => ({ count, origin, start_time: '2026-09-19T08:00:00Z', end_time: '2026-09-19T09:00:00Z' });
  merge(acc, buildDayWrites({ steps: [rec('samsung', 100), rec('fit', 40)] }));
  assert.equal(rollup('steps', acc['hc:day:2026-09-19:steps']).sum, 140);
  const b = buildDayWrites({ steps: [{ id: 'abc', count: 5, start_time: '2026-09-19T08:00:00Z', end_time: '2026-09-19T09:00:00Z' }] });
  assert.ok(Object.keys(b.hashes['hc:day:2026-09-19:steps'])[0] === 'id:abc');
});
test('heart rate aggregates and samples roll up; resting HR / weight use latest', () => {
  const b = buildDayWrites({
    heart_rate: [{ time: '2026-09-19T10:00:00Z', avg: 60, min: 55, max: 70, bpm: 60 }, { time: '2026-09-19T10:01:00Z', bpm: 80 }],
    resting_heart_rate: [{ bpm: 50, time: '2026-09-19T06:00:00Z' }, { bpm: 52, time: '2026-09-19T09:00:00Z' }],
    weight: [{ kilograms: 74.2, time: '2026-09-19T07:00:00Z' }],
  });
  assert.deepEqual(rollup('heart_rate', b.hashes['hc:day:2026-09-19:heart_rate']), { avg: 70, min: 55, max: 80, n: 2 });
  assert.equal(rollup('resting_heart_rate', b.hashes['hc:day:2026-09-19:resting_heart_rate']).last, 52);
  assert.equal(rollup('weight', b.hashes['hc:day:2026-09-19:weight']).last, 74.2);
});
test('malformed records skipped; missing metrics tolerated; sensitive types ignored', () => {
  const b = buildDayWrites({ steps: [{ count: 'x' }, null, { count: 5 }], menstruation_flow: [{ flow: 1, time: '2026-09-19T00:00:00Z' }], heart_rate: [{ time: 'bad', bpm: 60 }] });
  assert.equal(b.records, 0); assert.equal(b.skipped, 4);
  assert.deepEqual(b.hashes, {});
  assert.equal(rollup('weight', {}), null);
});

test('cumulative day-so-far windows (same start, growing end) count once; latest wins', () => {
  const acc = {};
  for (const [end, count] of [['2026-09-19T10:00:00Z', 5000], ['2026-09-19T11:00:00Z', 5600], ['2026-09-19T23:00:00Z', 16341]]) {
    merge(acc, buildDayWrites({ steps: [{ count, start_time: '2026-09-18T23:00:00Z', end_time: end }] }));
  }
  assert.equal(rollup('steps', acc['hc:day:2026-09-19:steps']).sum, 16341);
});
test('distinct-start intervals still sum', () => {
  const b = buildDayWrites({ steps: [{ count: 10, start_time: '2026-09-19T08:00:00Z', end_time: '2026-09-19T09:00:00Z' }, { count: 20, start_time: '2026-09-19T09:00:00Z', end_time: '2026-09-19T10:00:00Z' }] });
  assert.equal(rollup('steps', b.hashes['hc:day:2026-09-19:steps']).sum, 30);
});
