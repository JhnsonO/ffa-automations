'use strict';

const MAX_BODY_BYTES = 900 * 1024; // Upstash free tier max request ~1 MB
const MAX_TYPES = 64;
const TYPE_RE = /^[a-z][a-z0-9_]{0,63}$/;

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

// Health Connect Webhook JSON: { timestamp, app_version, <snake_case_type>: [ {...}, ... ] }
// Returns { ok: true, types: { name: [records] } } or { ok: false, error }.
function validatePayload(body) {
  if (!isObj(body)) return { ok: false, error: 'body must be a JSON object' };

  if (typeof body.timestamp !== 'string' || Number.isNaN(Date.parse(body.timestamp))) {
    return { ok: false, error: 'timestamp must be an ISO-8601 string' };
  }
  if (typeof body.app_version !== 'string' || body.app_version.length === 0 || body.app_version.length > 64) {
    return { ok: false, error: 'app_version must be a non-empty string' };
  }

  const types = {};
  for (const [key, val] of Object.entries(body)) {
    if (key === 'timestamp' || key === 'app_version') continue;
    if (!TYPE_RE.test(key)) return { ok: false, error: `invalid data type key: ${key.slice(0, 40)}` };
    if (!Array.isArray(val)) return { ok: false, error: `${key} must be an array` };
    if (!val.every(isObj)) return { ok: false, error: `${key} must contain only objects` };
    if (val.length > 0) types[key] = val;
  }
  if (Object.keys(types).length > MAX_TYPES) return { ok: false, error: 'too many data types' };

  return { ok: true, types };
}

module.exports = { validatePayload, MAX_BODY_BYTES };
