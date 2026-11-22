// Control envelope geometry. Pure functions, no drawing.
//
// One sentence for a judge: each spoke shows how far one movement reaches above its own resting
// jitter; that ratio has no unit, so eyebrow scores, head degrees and centimetres share one scale.
//
// The ratio is SNR = usable_range / effective_std, the only calibration metric comparable across
// units (perception/calibration.py). The activation threshold and the deadband are drawn on the
// same scale: offset / effective_std. Every spoke also carries its real range in its own unit.

// Rejection reasons from perception/calibration.py disqualifiers() and rest quality, in words.
export const REASONS = {
  insufficient_data: 'Not enough data to measure it.',
  zero_variance: 'It never changed during the session.',
  false_positive_in_held_out_rest: 'It crossed its threshold while the person was resting.',
  snr_below_floor: 'Its movement was less than 10 times its resting jitter.',
  excursion_below_floor: 'Its movement was smaller than the minimum for its unit.',
  rest_suspect: 'Its rest measurements disagreed, so its threshold cannot be trusted.',
}

export const UNIT_LABEL = { score: 'score', deg: '°', cm: 'cm' }

// A real value with its unit: blendshape scores are 0 to 1, angles in degrees, translations in cm.
export function formatInUnit(value, unit) {
  if (unit === 'score') return `${value.toFixed(2)} score`
  if (unit === 'deg') return `${value.toFixed(1)}°`
  return `${value.toFixed(1)} cm`
}

// The spokes of a profile, in binding order (best channel first).
export function spokesFrom(profile) {
  if (!profile) return []
  return profile.bindings.map((b) => ({
    channel: b.channel,
    unit: b.unit,
    role: b.role,
    direction: b.direction,
    range: b.usable_range,
    snr: b.usable_range / b.effective_std,
    thresholdSnr: b.threshold.offset / b.effective_std,
    deadbandSnr: b.deadband.offset / b.effective_std,
  }))
}

// Spokes while calibration is still running: one per channel with progress, in first-seen order.
// Each uses the stream's own snr and peak_deviation. There is no threshold yet (it comes with the
// profile), and every spoke says whether its noise estimate is provisional and on what basis.
export function spokesFromProgress(progress) {
  return progress.map((e) => ({
    channel: e.channel,
    unit: e.unit,
    role: null,
    direction: null,
    range: Math.abs(e.peak_deviation),
    snr: e.snr,
    thresholdSnr: null,
    deadbandSnr: null,
    provisional: e.provisional,
    basis: e.basis,
  }))
}

// What each noise basis means, in words (docs/sim-decisions.md §2.3).
export const BASIS_WORDS = {
  prior_default: 'Provisional: typical noise, not measured yet',
  interrep_quiet: 'Provisional: noise from the quiet gaps between reps',
  final_rest: 'Final: noise from the rest measurement',
}

// The channels that were measured but not bound, with their reasons.
export function unboundFrom(profile) {
  if (!profile) return []
  return profile.enrolment_metrics.ranking
    .filter((r) => r.status !== 'usable')
    .map((r) => ({ channel: r.channel, status: r.status, reasons: r.reasons, redundantWith: r.redundant_with ?? null }))
}

// The smallest "nice" number (1, 2, 2.5 or 5 times a power of ten) at or above value.
// The scale never drops below minimum, so the threshold region stays readable.
export function niceMax(value, minimum = 10) {
  const v = Math.max(value, minimum)
  const power = 10 ** Math.floor(Math.log10(v))
  for (const step of [1, 2, 2.5, 5, 10]) if (step * power >= v - 1e-9) return step * power
  return 10 * power
}

// The rescale target: fits the strongest bound movement.
export function scaleFor(spokes) {
  return niceMax(Math.max(0, ...spokes.map((s) => s.snr)))
}

export function ringsFor(max, count = 4) {
  return Array.from({ length: count }, (_, i) => (max * (i + 1)) / count)
}

// Spoke i of n, in SVG radians: the first spoke points straight up, the rest go clockwise.
export function angleFor(i, n) {
  return -Math.PI / 2 + (2 * Math.PI * i) / n
}

const at = (angle, r) => ({ x: r * Math.cos(angle), y: r * Math.sin(angle) })

// The filled shape through each spoke's value. With fewer than three spokes a polygon has no
// area, so the shape gets a fixed half-width at the centre: one spoke is a thin sliver, two make
// a thin diamond. values are in SNR; radius is the drawing radius of `max`.
export function shapePoints(values, max, radius, halfWidth = radius * 0.035) {
  const n = values.length
  if (n === 0) return []
  const r = values.map((v) => (Math.min(v, max) / max) * radius)
  if (n >= 3) return r.map((len, i) => at(angleFor(i, n), len))
  const side = angleFor(0, n) + Math.PI / 2
  const w1 = at(side, halfWidth)
  const w2 = at(side + Math.PI, halfWidth)
  if (n === 1) return [w1, at(angleFor(0, 1), r[0]), w2]
  return [at(angleFor(0, 2), r[0]), w1, at(angleFor(1, 2), r[1]), w2]
}

export const toPath = (points) => (points.length ? `M ${points.map((p) => `${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(' L ')} Z` : '')

// Animation helpers for the rescale.
export const ease = (t) => (t <= 0 ? 0 : t >= 1 ? 1 : t * t * (3 - 2 * t))
export const lerp = (a, b, t) => a + (b - a) * t
