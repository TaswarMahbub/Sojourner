// Follows one calibration session for the control envelope, from whatever the source delivers:
// a saved profile (recorded source), or the live stream's events (docs/sim-decisions.md §2).
//
// - session_header starts a new session: everything from the previous one is dropped.
// - channel_progress events are checked one by one and against the session so far
//   (ProgressTracker, §2.4). Valid ones update that channel's latest value.
// - calibration_profile is validated, then reconciled against the final values that came before it.
// - Every other calibration event (markers, scoring, confirm_result, session_end) is ignored here.
//
// Nothing is ever dropped silently: a message that fails a check is listed in `problems`.
// Provisional values are display only (§2.2); nothing here gates or stores anything.

import { decodeJson } from '../contracts/json.js'
import { CHANNEL_PROGRESS_RECORD_TYPE, ProgressTracker, decodeChannelProgress } from '../contracts/calibrationProgress.js'
import { PROFILE_RECORD_TYPE, decodeCalibrationProfile, validateCalibrationProfile } from '../contracts/calibrationProfile.js'

export const SESSION_HEADER = 'session_header' // perception/calibration.py HEADER
export const PROBLEM_LIMIT = 20

export const EMPTY_SESSION = Object.freeze({
  header: null, // { session_id, source, measured } from session_header, if the stream sent one
  progress: [], // newest valid channel_progress per channel, in first-seen order
  profile: null, // the newest valid calibration_profile
  profileErrors: [], // validation errors of the newest profile, if it was rejected
  problems: [], // messages that failed a check, newest first
})

// What phase the envelope is showing, from a session snapshot.
export function phaseOf(session) {
  if (session.profile) return 'profile'
  if (session.progress.length === 0) return 'waiting'
  return session.progress.some((e) => e.provisional) ? 'provisional' : 'final'
}

export class CalibrationSession {
  constructor() {
    this.reset()
  }

  reset() {
    this.tracker = new ProgressTracker()
    this.state = EMPTY_SESSION
  }

  // message: an object (mock, recorded) or raw JSON text (websocket). Returns the new snapshot.
  receive(message) {
    const text = typeof message === 'string' ? message : null
    let value = message
    if (text !== null || message === null) {
      const decoded = decodeJson(text)
      if (!decoded.ok) return this.problem([decoded.error])
      value = decoded.value
    }
    switch (value?.record_type) {
      case SESSION_HEADER:
        this.reset()
        this.state = { ...EMPTY_SESSION, header: { session_id: value.session_id ?? null, source: value.source ?? null, measured: value.measured ?? null } }
        return this.state
      case CHANNEL_PROGRESS_RECORD_TYPE:
        return this.progress(text, value)
      case PROFILE_RECORD_TYPE:
        return this.profile(text, value)
      default:
        return this.state
    }
  }

  progress(text, event) {
    if (text !== null) {
      const { errors } = decodeChannelProgress(text)
      if (errors.length) return this.problem(errors, event)
    }
    const errors = this.tracker.apply(event)
    if (errors.length) return this.problem(errors, event)
    this.state = { ...this.state, progress: [...this.tracker.latest.values()] }
    return this.state
  }

  profile(text, profile) {
    const { valid, errors } = text !== null ? decodeCalibrationProfile(text) : validateCalibrationProfile(profile)
    if (!valid) {
      this.state = { ...this.state, profileErrors: errors }
      return this.state
    }
    const reconciliation = this.tracker.apply(profile)
    this.state = { ...this.state, profile, profileErrors: [] }
    return reconciliation.length ? this.problem(reconciliation, profile) : this.state
  }

  problem(errors, message = null) {
    const where = message?.channel ?? message?.record_type ?? 'message'
    const entries = errors.map((error) => ({ where, error }))
    this.state = { ...this.state, problems: [...entries, ...this.state.problems].slice(0, PROBLEM_LIMIT) }
    return this.state
  }
}
