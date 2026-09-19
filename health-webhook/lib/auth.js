'use strict';
const crypto = require('crypto');

// Constant-time compare of X-Webhook-Secret against HEALTH_WEBHOOK_SECRET.
// Hashing first gives equal-length buffers so timingSafeEqual never throws.
function isAuthorised(req) {
  const expected = process.env.HEALTH_WEBHOOK_SECRET;
  if (!expected || expected.length < 16) return false; // fail closed if unset/weak
  const given = req.headers['x-webhook-secret'];
  if (typeof given !== 'string' || given.length === 0) return false;
  const a = crypto.createHash('sha256').update(given).digest();
  const b = crypto.createHash('sha256').update(expected).digest();
  return crypto.timingSafeEqual(a, b);
}

module.exports = { isAuthorised };
