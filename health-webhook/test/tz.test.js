'use strict';
const test = require('node:test'); const assert = require('node:assert');
const { localDate } = require('../lib/tz');

test('GMT/BST day allocation around spring-forward (29 Mar 2026)', () => {
  assert.equal(localDate('2026-03-28T23:30:00Z'), '2026-03-28'); // GMT 23:30
  assert.equal(localDate('2026-03-29T00:30:00Z'), '2026-03-29'); // GMT 00:30
  assert.equal(localDate('2026-03-29T23:30:00Z'), '2026-03-30'); // BST 00:30 next day
});
test('GMT/BST day allocation around fall-back (25 Oct 2026, 25-hour day)', () => {
  assert.equal(localDate('2026-10-24T23:30:00Z'), '2026-10-25'); // BST 00:30
  assert.equal(localDate('2026-10-25T00:59:00Z'), '2026-10-25'); // BST 01:59
  assert.equal(localDate('2026-10-25T01:30:00Z'), '2026-10-25'); // GMT 01:30 (repeated hour)
  assert.equal(localDate('2026-10-25T23:30:00Z'), '2026-10-25'); // GMT 23:30 still 25th
  assert.equal(localDate('2026-10-26T00:00:00Z'), '2026-10-26');
});
test('invalid input', () => assert.equal(localDate('nope'), null));
