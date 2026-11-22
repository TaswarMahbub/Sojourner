// Calibration progress events: PROVISIONAL, not part of docs/contracts.md.
//
// Defined in docs/sim-decisions.md §2. The live calibration stream (ws://127.0.0.1:8766, one JSON
// object per text message) sends the same events as `calibrate --events json`, plus one
// `channel_progress` event per channel per probe rep, so the envelope can redraw while the session
// runs.
//
// Rest is the noise denominator and it is measured last, so values during probing are provisional.
// The measurement fields keep the same names in both phases (effective_std, snr); the status is
// carried separately, in `provisional` and `basis`:
//   interrep_quiet  rolling noise estimate from the quiet gaps between reps
//   prior_default   population prior; no usable quiet gap yet
//   final_rest      the measured final rest (provisional: false)
//
// §2.4: when the final rest lands, the stream re-emits one channel_progress per channel with
// provisional: false and basis: "final_rest", BEFORE the calibration_profile. ProgressTracker
// below checks that order, and that the final numbers agree with the stored profile, so the
// operator never sees numbers the profile disagrees with.
//
// Provisional values are operator feedback only (§2.2): nothing here gates anything.
//
// OPEN, asked of Mridhul, accepted either way until he answers (see OPEN_QUESTIONS):
// - whether a final_rest event carries probe and rep_index, or null for both;
// - whether rep_index counts from 0 or 1;
// - whether peak_deviation can be negative (a channel that moves in the negative direction);
// - how snr is defined: the §2.3 example is peak_deviation / effective_std, while the profile's
//   SNR is usable_range / effective_std. Not checked until that is settled.

import { decodeJson, isIntegerField } from './json.js'
import { isFiniteNumber, isPlainObject } from './cameraFrame.js'
import { UNITS } from './calibrationProfile.js'

export const CHANNEL_PROGRESS_RECORD_TYPE = 'channel_progress'
export const BASES = ['interrep_quiet', 'prior_default', 'final_rest']
export const FINAL_BASIS = 'final_rest'
// The probes in perception/calibration.py PROBES, in protocol order.
export const PROBE_NAMES = ['brows', 'jaw', 'smile', 'turn', 'nod', 'tilt']

export const OPEN_QUESTIONS = [
  'final_rest events: are probe and rep_index present, or null?',
  'rep_index: counts from 0 or 1?',
  'peak_deviation: can it be negative?',
  'snr: peak_deviation / effective_std, or usable_range / effective_std as in the profile?',
]

const isString = (v) => typeof v === 'string' && v.length > 0
const isNonNegativeInteger = (v) => Number.isSafeInteger(v) && v >= 0

// Returns a list of problems with one event; empty means valid. Unknown fields are allowed,
// because the format is still changing.
export function validateChannelProgress(e) {
  if (!isPlainObject(e)) return ['channel_progress: must be an object']
  const errors = []
  if (e.record_type !== CHANNEL_PROGRESS_RECORD_TYPE) errors.push(`record_type: must be ${CHANNEL_PROGRESS_RECORD_TYPE}`)
  if (!isNonNegativeInteger(e.t_ms)) errors.push('t_ms: must be an integer >= 0')
  if (!isString(e.channel)) errors.push('channel: must be a non-empty string')
  if (!UNITS.includes(e.unit)) errors.push(`unit: must be one of ${UNITS.join(', ')}`)

  if (typeof e.provisional !== 'boolean') errors.push('provisional: must be true or false')
  if (!BASES.includes(e.basis)) errors.push(`basis: must be one of ${BASES.join(', ')}`)
  else if (typeof e.provisional === 'boolean' && e.provisional !== (e.basis !== FINAL_BASIS)) {
    errors.push(e.provisional
      ? 'provisional: a final_rest value is not provisional'
      : `provisional: must be true for basis ${e.basis}; only final_rest is final`)
  }

  // A final event may or may not name a probe and rep (OPEN); a provisional one must.
  const final = e.basis === FINAL_BASIS
  if (!(final && e.probe === null) && !PROBE_NAMES.includes(e.probe)) {
    errors.push(`probe: must be one of ${PROBE_NAMES.join(', ')}${final ? ', or null on a final_rest event' : ''}`)
  }
  if (!(final && e.rep_index === null) && !isNonNegativeInteger(e.rep_index)) {
    errors.push(`rep_index: must be an integer >= 0${final ? ', or null on a final_rest event' : ''}`)
  }

  if (!isFiniteNumber(e.peak_deviation)) errors.push('peak_deviation: must be a finite number')
  if (!isFiniteNumber(e.effective_std) || e.effective_std <= 0) errors.push('effective_std: must be a number above 0')
  if (!isFiniteNumber(e.snr) || e.snr < 0) errors.push('snr: must be a number >= 0')
  return errors
}

// Raw message text, as the stream delivers it. Returns { valid, errors, event }.
export function decodeChannelProgress(text) {
  const decoded = decodeJson(text)
  if (!decoded.ok) return { valid: false, errors: [decoded.error], event: null }
  const errors = validateChannelProgress(decoded.value)
  // Integers written as floats (12840.0) are rejected, as elsewhere in the frontend.
  for (const key of ['t_ms', 'rep_index']) {
    if (Number.isSafeInteger(decoded.value?.[key]) && !isIntegerField(decoded.value, key)) {
      errors.push(`${key}: must be written as an integer, not a float`)
    }
  }
  return { valid: errors.length === 0, errors, event: errors.length === 0 ? decoded.value : null }
}

// Follows one calibration session's events in arrival order and checks §2.4:
// - t_ms never goes backwards;
// - once a channel's final_rest value has arrived, no provisional value follows it;
// - when the calibration_profile arrives, every channel that had progress has a final value, and
//   each final effective_std equals the profile's rest effective_std for that channel.
// `latest` holds the newest valid event per channel, for display.
export class ProgressTracker {
  constructor() {
    this.latest = new Map() // channel -> newest valid channel_progress
    this.finals = new Map() // channel -> its final_rest event
    this.lastTms = -Infinity
    this.profileSeen = false
  }

  // Takes one decoded event. Returns its problems; empty means it fits the session so far.
  apply(event) {
    if (event?.record_type === 'calibration_profile') return this.applyProfile(event)
    if (event?.record_type !== CHANNEL_PROGRESS_RECORD_TYPE) return [] // other calibration events pass through
    const errors = validateChannelProgress(event)
    if (errors.length) return errors
    if (this.profileSeen) errors.push(`${event.channel}: channel_progress after the calibration_profile`)
    if (event.t_ms < this.lastTms) errors.push(`t_ms: ${event.t_ms} is before the previous event (${this.lastTms})`)
    if (event.provisional && this.finals.has(event.channel)) {
      errors.push(`${event.channel}: provisional value after its final_rest value`)
    }
    if (!event.provisional && this.finals.has(event.channel)) errors.push(`${event.channel}: second final_rest value`)
    if (errors.length) return errors
    this.lastTms = event.t_ms
    this.latest.set(event.channel, event)
    if (!event.provisional) this.finals.set(event.channel, event)
    return []
  }

  applyProfile(profile) {
    this.profileSeen = true
    const errors = []
    const stored = profile?.enrolment_metrics?.channels ?? {}
    for (const channel of this.latest.keys()) {
      const final = this.finals.get(channel)
      if (!final) {
        errors.push(`${channel}: calibration_profile arrived before this channel's final_rest value`)
        continue
      }
      const storedStd = stored[channel]?.rest?.effective_std
      if (storedStd !== undefined && storedStd !== final.effective_std) {
        errors.push(`${channel}: final effective_std ${final.effective_std} differs from the profile's ${storedStd}`)
      }
    }
    return errors
  }
}
