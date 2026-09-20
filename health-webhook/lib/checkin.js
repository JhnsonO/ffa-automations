'use strict';
const { getDailyRollups } = require('./daily');
const { fetchRecentWorkouts } = require('./hevy');
const { pipeline, k } = require('./redis');

const r1 = (n) => (n === null || n === undefined ? null : Math.round(n * 10) / 10);
const r2 = (n) => (n === null || n === undefined ? null : Math.round(n * 100) / 100);
const mins = (s) => Math.round(s / 60);

// One day's rollups -> compact summary. Absent metrics are simply omitted.
function summariseDay(r) {
  const o = {};
  if (r.steps) o.steps = Math.round(r.steps.sum);
  if (r.distance) o.distance_km = r2(r.distance.sum / 1000);
  if (r.active_calories) o.active_kcal = Math.round(r.active_calories.sum);
  if (r.total_calories) o.total_kcal = Math.round(r.total_calories.sum);
  if (r.heart_rate) o.heart_rate = { avg: r1(r.heart_rate.avg), min: r.heart_rate.min, max: r.heart_rate.max };
  if (r.resting_heart_rate) o.resting_hr = r.resting_heart_rate.last;
  if (r.heart_rate_variability) o.hrv_ms = r1(r.heart_rate_variability.avg);
  if (r.oxygen_saturation) o.spo2_pct = r1(r.oxygen_saturation.avg);
  if (r.respiratory_rate) o.respiratory_rate = r1(r.respiratory_rate.avg);
  if (r.weight) o.weight_kg = r.weight.last;
  if (r.body_fat) o.body_fat_pct = r.body_fat.last;
  if (r.vo2_max) o.vo2_max = r.vo2_max.last;
  if (r.sleep) {
    const s = r.sleep;
    o.sleep = {
      overnight_hours: r2(s.overnight_seconds / 3600),
      overnight_stages_minutes: Object.fromEntries(Object.entries(s.overnight_stages || {}).map(([n, d]) => [n, mins(d)])),
      fell_asleep_utc: s.overnight_start, woke_utc: s.overnight_end,
      nap_minutes: mins(s.nap_seconds),
    };
  }
  if (r.exercise) {
    o.exercise = r.exercise.sessions.map((x) => ({
      type: x.t, start_utc: x.s, minutes: x.d === null ? null : mins(x.d),
      distance_km: x.m === null ? null : r2(x.m / 1000), steps: x.n,
    }));
  }
  return o;
}

// Baseline extractors: one number (or null) per day.
const BASE = {
  steps: (r) => r.steps && r.steps.sum,
  heart_rate_avg: (r) => r.heart_rate && r.heart_rate.avg,
  resting_hr: (r) => r.resting_heart_rate && r.resting_heart_rate.last,
  hrv_ms: (r) => r.heart_rate_variability && r.heart_rate_variability.avg,
  spo2_pct: (r) => r.oxygen_saturation && r.oxygen_saturation.avg,
  weight_kg: (r) => r.weight && r.weight.last,
  body_fat_pct: (r) => r.body_fat && r.body_fat.last,
  sleep_overnight_hours: (r) => r.sleep && r.sleep.overnight_seconds > 0 ? r.sleep.overnight_seconds / 3600 : null,
  exercise_minutes: (r) => (r.steps || r.heart_rate) ? ((r.exercise ? r.exercise.sessions : []).reduce((t, x) => t + (x.d || 0), 0) / 60) : null,
};
const WATCHED = { resting_heart_rate: 'resting_hr', heart_rate_variability: 'hrv_ms', weight: 'weight_kg', body_fat: 'body_fat_pct', vo2_max: 'vo2_max', oxygen_saturation: 'spo2_pct' };

function baseline(dates, byDate) {
  const out = { days_considered: dates.length, from: dates[dates.length - 1], to: dates[0] };
  for (const [name, fn] of Object.entries(BASE)) {
    const vals = dates.map((d) => fn(byDate[d] || {})).filter((v) => typeof v === 'number' && Number.isFinite(v));
    out[name] = vals.length ? { avg: r2(vals.reduce((a, b) => a + b, 0) / vals.length), n_days: vals.length } : null;
  }
  return out;
}

// Pure composer (unit-tested). dates newest first; dates[0] = today (may be partial).
function composeCheckin({ dates, byDate, hevy, lastSync, now }) {
  const [today, yesterday, ...prior] = dates;
  const priorSeven = [yesterday, ...prior].slice(0, 7);
  const missing = Object.entries(WATCHED)
    .filter(([type]) => !dates.some((d) => byDate[d] && byDate[d][type]))
    .map(([, label]) => label);
  return {
    generated_at: new Date(now).toISOString(),
    timezone: 'Europe/London',
    date: today,
    data_last_synced_utc: lastSync || null,
    notes: [
      'today is a partial day; steps/exercise for today are so far',
      'today.sleep is last night (sessions ending before 12:00 local are overnight; later ones are naps)',
      'baseline_7d covers the 7 days before today',
    ],
    today: summariseDay(byDate[today] || {}),
    yesterday: summariseDay(byDate[yesterday] || {}),
    baseline_7d: baseline(priorSeven, byDate),
    missing_metrics: missing,
    hevy,
  };
}

async function buildCheckin() {
  const [{ dates, byDate }, [lastSync]] = await Promise.all([
    getDailyRollups(8),
    pipeline([['GET', k('hc:last_sync')]]),
  ]);
  const hevy = await fetchRecentWorkouts({ apiKey: process.env.HEVY_API_KEY });
  return composeCheckin({ dates, byDate, hevy, lastSync, now: Date.now() });
}
module.exports = { composeCheckin, buildCheckin, summariseDay, baseline };
