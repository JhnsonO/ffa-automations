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
