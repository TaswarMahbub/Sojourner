// Display formatting for the attempt metrics object that sim/attempt.js emits. Display only:
// every number here was computed by sim/metrics.js from the world model and collision (I2);
// nothing is recomputed. Every value carries a unit and a fixed number of decimals, so the
// columns do not change width between attempts (handoff T8.2).

const fixed = (v, digits) => (v === null || v === undefined ? null : v.toFixed(digits))

// Why an attempt ended, in words.
export const ENDED_BY = {
  room_entry: 'Entered the room',
  reset: 'Reset before clearing',
}

// The five session metrics, as rows: { key, label, value, unit, note }. value is null when the
// metric does not exist for this attempt (for example time to clear, when it never cleared).
export function metricRows(result) {
  const threshold = fixed(result.near_miss_threshold_m, 2)
  return [
    {
      key: 'time_to_clear',
      label: 'Time to clear the doorway',
      value: fixed(result.time_to_clear_s, 1),
      unit: 's',
      note: result.time_to_clear_s === null ? 'Did not clear' : 'From the first accepted motion to fully inside the room',
    },
    {
      key: 'collisions',
      label: 'Collisions',
      value: String(result.collisions),
      unit: result.collisions === 1 ? 'contact' : 'contacts',
      note: `Near misses within ${threshold} m: ${result.near_misses}`,
    },
    {
      key: 'path_efficiency',
      label: 'Path efficiency',
      value: fixed(result.path_efficiency, 2),
      unit: '× shortest path',
      note: result.path_efficiency === null
        ? 'Only for an attempt that cleared'
        : `${fixed(result.distance_m, 2)} m travelled, shortest legal path ${fixed(result.optimal_path_m, 2)} m`,
    },
    {
      key: 'cues_per_m',
      label: 'Cues per metre',
      value: fixed(result.cues_per_m, 2),
      unit: 'cues/m',
      note: `${result.cues} ${result.cues === 1 ? 'cue' : 'cues'} over ${fixed(result.distance_m, 2)} m`,
    },
    {
      key: 'hesitation',
      label: 'Hesitation (median)',
      value: fixed(result.hesitation_median_s, 2),
      unit: 's',
      note: result.hesitation_s.length === 0
        ? 'No cue followed a moment when one was available'
        : `Dwell before each committed cue, ${result.hesitation_s.length} ${result.hesitation_s.length === 1 ? 'cue' : 'cues'}: ${result.hesitation_s.map((h) => h.toFixed(2)).join(', ')} s`,
    },
  ]
}

// The badge text for where the attempt's commands came from (sim-decisions §4): a synthetic
// attempt must never read as a hardware one.
export function provenanceLabel(result) {
  const source = result.provenance?.source
  if (source === 'hardware') return 'HARDWARE COMMANDS'
  if (source === 'synthetic') return 'SYNTHETIC COMMANDS'
  return 'UNKNOWN SOURCE'
}
