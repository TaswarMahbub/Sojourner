// Calibration profile: PROVISIONAL, profile schema 1.
//
// This record is NOT part of docs/contracts.md. It is produced by build_profile() in
// perception/calibration.py and carries its own schema_version, independent of the contract's.
// The checks below follow that function as of profile schema 1 and cover only the fields the
// control envelope reads. Fields this file does not use are allowed, because the format is still
// changing; an unknown value in a field it does use is rejected.

import { decodeJson } from './json.js'
import { isFiniteNumber, isPlainObject } from './cameraFrame.js'

export const PROFILE_RECORD_TYPE = 'calibration_profile'
export const PROFILE_SCHEMA_VERSION = 1

export const UNITS = ['score', 'deg', 'cm']
export const ROLES = ['proportional_primary', 'proportional_secondary', 'discrete', 'scan_switch']
export const RANKING_STATUSES = ['usable', 'redundant', 'disqualified']
export const TIERS = {
  1: 'proportional',
  2: 'discrete',
  3: 'scanning',
}

const isString = (v) => typeof v === 'string' && v.length > 0
const isPositive = (v) => isFiniteNumber(v) && v > 0
const isNonNegative = (v) => isFiniteNumber(v) && v >= 0

function checkBinding(b, i, errors) {
  const at = `bindings[${i}]`
  if (!isPlainObject(b)) {
    errors.push(`${at}: must be an object`)
    return
  }
  if (!isString(b.channel)) errors.push(`${at}.channel: must be a non-empty string`)
  if (!ROLES.includes(b.role)) errors.push(`${at}.role: must be one of ${ROLES.join(', ')}`)
  if (!UNITS.includes(b.unit)) errors.push(`${at}.unit: must be one of ${UNITS.join(', ')}`)
  if (b.direction !== 1 && b.direction !== -1) errors.push(`${at}.direction: must be 1 or -1`)
  if (!isFiniteNumber(b.rest_mean)) errors.push(`${at}.rest_mean: must be a finite number`)
  if (!isPositive(b.effective_std)) errors.push(`${at}.effective_std: must be a number above 0`)
  if (!isPositive(b.usable_range)) errors.push(`${at}.usable_range: must be a number above 0`)
  for (const band of ['threshold', 'deadband']) {
    if (!isPlainObject(b[band]) || !isNonNegative(b[band].offset)) errors.push(`${at}.${band}.offset: must be a number >= 0`)
  }
}

function checkRankingRow(r, i, errors) {
  const at = `enrolment_metrics.ranking[${i}]`
  if (!isPlainObject(r)) {
    errors.push(`${at}: must be an object`)
    return
  }
  if (!isString(r.channel)) errors.push(`${at}.channel: must be a non-empty string`)
  if (!RANKING_STATUSES.includes(r.status)) errors.push(`${at}.status: must be one of ${RANKING_STATUSES.join(', ')}`)
  if (!Array.isArray(r.reasons) || !r.reasons.every(isString)) errors.push(`${at}.reasons: must be a list of strings`)
  else if (r.status === 'disqualified' && r.reasons.length === 0) errors.push(`${at}.reasons: a disqualified channel must name a reason`)
  else if (r.status !== 'disqualified' && r.reasons.length > 0) errors.push(`${at}.reasons: only a disqualified channel has reasons`)
  if (r.status === 'redundant' && !isString(r.redundant_with)) errors.push(`${at}.redundant_with: a redundant channel names the channel it duplicates`)
}

// Returns { valid, errors }. Never throws.
export function validateCalibrationProfile(p) {
  try {
    if (!isPlainObject(p)) return { valid: false, errors: ['profile: must be a JSON object'] }
    const errors = []
    if (p.record_type !== PROFILE_RECORD_TYPE) errors.push(`record_type: expected "${PROFILE_RECORD_TYPE}"`)
    if (p.schema_version !== PROFILE_SCHEMA_VERSION) errors.push(`schema_version: expected ${PROFILE_SCHEMA_VERSION} (provisional profile schema)`)
    if (errors.length) return { valid: false, errors }

    if (!['ok', 'suspect'].includes(p.session_quality)) errors.push('session_quality: must be "ok" or "suspect"')
    if (typeof p.measured !== 'boolean') errors.push('measured: must be true or false')

    const tier = p.proposed_tier
    if (!isPlainObject(tier)) {
      errors.push('proposed_tier: must be an object')
    } else {
      if (tier.tier === null) {
        if (tier.mode !== null) errors.push('proposed_tier.mode: must be null when no tier is proposed')
      } else if (!Object.hasOwn(TIERS, tier.tier)) {
        errors.push('proposed_tier.tier: must be 1, 2, 3 or null')
      } else if (tier.mode !== TIERS[tier.tier]) {
        errors.push(`proposed_tier.mode: tier ${tier.tier} is "${TIERS[tier.tier]}"`)
      }
      if (!isString(tier.reason)) errors.push('proposed_tier.reason: must be a non-empty string')
    }

    if (!Array.isArray(p.bindings)) errors.push('bindings: must be a list')
    else p.bindings.forEach((b, i) => checkBinding(b, i, errors))

    const ranking = p.enrolment_metrics?.ranking
    if (!Array.isArray(ranking)) errors.push('enrolment_metrics.ranking: must be a list')
    else ranking.forEach((r, i) => checkRankingRow(r, i, errors))
    if (errors.length) return { valid: false, errors }

    // Consistency between the parts (build_profile's own rules).
    const names = p.bindings.map((b) => b.channel)
    if (new Set(names).size !== names.length) errors.push('bindings: a channel is bound twice')
    const usable = new Set(ranking.filter((r) => r.status === 'usable').map((r) => r.channel))
    for (const name of names) if (!usable.has(name)) errors.push(`bindings: "${name}" is bound but not ranked usable`)
    if (p.session_quality === 'suspect' && (p.bindings.length > 0 || tier.tier !== null)) {
      errors.push('session_quality: a suspect profile has no bindings and no tier')
    }
    if (tier.tier === 3 && p.bindings.length !== 1) errors.push('proposed_tier: tier 3 binds exactly one channel')
    if (tier.tier === null && p.bindings.length > 0) errors.push('proposed_tier: no tier means no bindings')

    return { valid: errors.length === 0, errors }
  } catch {
    return { valid: false, errors: ['profile: rejected'] }
  }
}

// Raw JSON text -> { valid, errors, profile }. Never throws.
export function decodeCalibrationProfile(text) {
  const decoded = decodeJson(text)
  if (!decoded.ok) return { valid: false, errors: [decoded.error], profile: null }
  return { ...validateCalibrationProfile(decoded.value), profile: decoded.value }
}
