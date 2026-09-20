'use strict';
// Local calendar date (YYYY-MM-DD) in Europe/London for an ISO instant. DST-safe via Intl.
const fmt = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit',
});
function localDate(iso) {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  return fmt.format(new Date(ms)); // en-CA => YYYY-MM-DD
}
module.exports = { localDate };

const hourFmt = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', hour: '2-digit', hourCycle: 'h23' });
function localHour(iso) {
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : parseInt(hourFmt.format(new Date(ms)), 10);
}
// Calendar-day arithmetic on YYYY-MM-DD strings (DST-safe: noon UTC anchor).
function addDays(dateStr, n) {
  return new Date(Date.parse(`${dateStr}T12:00:00Z`) + n * 86400000).toISOString().slice(0, 10);
}
module.exports.localHour = localHour;
module.exports.addDays = addDays;
