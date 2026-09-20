'use strict';
// Recent Hevy workouts, fetched server-side. Key lives only in the HEVY_API_KEY env var.
const r1 = (n) => Math.round(n * 10) / 10;

function compactWorkouts(workouts, days, now) {
  const cutoff = now - days * 86400000;
  return (workouts || [])
    .filter((w) => Date.parse(w.start_time) >= cutoff)
    .slice(0, 10)
    .map((w) => ({
      date: w.start_time,
      title: w.title,
      minutes: w.end_time ? Math.round((Date.parse(w.end_time) - Date.parse(w.start_time)) / 60000) : null,
      exercises: (w.exercises || []).map((ex) => {
        const work = (ex.sets || []).filter((s) => s.type !== 'warmup');
        const weighted = work.filter((s) => typeof s.weight_kg === 'number' && s.weight_kg > 0 && typeof s.reps === 'number')
          .sort((a, b) => b.weight_kg - a.weight_kg || b.reps - a.reps);
        const reps = work.map((s) => s.reps).filter((n) => typeof n === 'number');
        return {
          name: ex.title,
          working_sets: work.length,
          top_set: weighted[0] ? { kg: weighted[0].weight_kg, reps: weighted[0].reps } : null,
          best_reps: reps.length ? Math.max(...reps) : null,
          volume_kg: r1(weighted.reduce((t, s) => t + s.weight_kg * s.reps, 0)),
        };
      }),
    }));
}

async function fetchRecentWorkouts({ apiKey, days = 14, fetchImpl = fetch, now = Date.now() } = {}) {
  if (!apiKey) return { available: false, reason: 'HEVY_API_KEY not configured' };
  const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), 8000);
  try {
    const r = await fetchImpl('https://api.hevyapp.com/v1/workouts?page=1&pageSize=10', {
      headers: { 'api-key': apiKey, Accept: 'application/json', 'User-Agent': 'johnson-os-health-webhook/0.2' },
      signal: ctl.signal,
    });
    if (!r.ok) return { available: false, reason: `hevy http ${r.status}` };
    const d = await r.json();
    return { available: true, window_days: days, workouts: compactWorkouts(d.workouts, days, now) };
  } catch (e) {
    return { available: false, reason: 'hevy request failed' };
  } finally { clearTimeout(timer); }
}
module.exports = { fetchRecentWorkouts, compactWorkouts };
