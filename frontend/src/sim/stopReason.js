// Names WHY the rover is not moving, for the state readout (handoff "Rules you must not bend":
// show the rejection — `watchdog`, `estop latch`, `stale challenge`, `not armed`).
//
// The receiver is the only judge (invariant I5). Nothing here changes a verdict: these functions
// read receiver fields and its pure check methods after the fact, and translate the verdict the
// receiver already reached into a short reason. The receiver does not record why a latch was set,
// so the simulation tracks the fault cause itself (simulation.js: a fault latch that appears during
// a clock advance, with no datagram in hand, can only be the watchdog).

/** Reasons, as shown on screen. One word or short phrase each, lower case, stable. */
import { verifyCommand } from '../contracts/motor.js'

export const REASONS = Object.freeze({
  firmwareFrozen: 'firmware frozen',
  startupLatch: 'startup latch',
  physicalEstop: 'physical estop',
  noMotorPower: 'no motor power',
  cutoffOpen: 'cutoff open',
  estopLatch: 'estop latch',
  watchdog: 'watchdog',
  faultLatch: 'fault latch',
  notArmed: 'not armed',
  staleChallenge: 'stale challenge',
  wrongGeneration: 'wrong rearm generation',
  expired: 'expired',
  motionBudget: 'challenge budget spent',
  malformed: 'malformed',
  unauthenticated: 'unauthenticated',
  wrongBoot: 'wrong boot',
  oldSession: 'old session',
  replay: 'replay or out of order',
  invalid: 'invalid for session',
  ignored: 'ignored',
})

/**
 * Why the receiver will not move the rover right now, or null when motion would be accepted.
 * State only; independent of any datagram.
 * @param {import('./motorReceiver.js').MotorReceiver} rx
 * @param {string|null} faultCause REASONS.watchdog or REASONS.faultLatch, tracked by the caller
 */
export function haltReason(rx, faultCause = null) {
  if (rx.firmwareFrozen) return REASONS.firmwareFrozen
  if (rx.startupLatch) return REASONS.startupLatch
  if (rx.physicalEstop || rx.sensorState !== 'released') return REASONS.physicalEstop
  if (!rx.busPowered) return REASONS.noMotorPower
  if (rx.cutoffOpen) return REASONS.cutoffOpen
  if (rx.softwareEstopLatch) return REASONS.estopLatch
  if (rx.faultLatch) return faultCause ?? REASONS.faultLatch
  if (!rx.armed) return REASONS.notArmed
  return null
}

/**
 * Why a delivered datagram did not produce motion, given the receiver's outcome. Called right after
 * handleRaw(), so the receiver's state is the state the verdict was reached in. Returns null for
 * accepted outcomes.
 * @param {string} outcome the receiver's return value
 * @param {object|null} message the parsed datagram, or null when it did not parse
 * @param {import('./motorReceiver.js').MotorReceiver} rx
 * @param {string|null} faultCause
 */
export function rejectionReason(outcome, message, rx, faultCause = null) {
  if (outcome.startsWith('accepted')) return null
  if (outcome === 'frozen') return REASONS.firmwareFrozen
  if (outcome === 'malformed') return REASONS.malformed
  const m = message && typeof message === 'object' ? message : null
  if (outcome === 'rejected_motion' || outcome === 'rejected_rearm') {
    const halted = haltReason(rx, faultCause)
    // A rearm exists to clear latches, so a latch is not why a rearm failed.
    if (outcome === 'rejected_motion' && halted && halted !== REASONS.notArmed) return halted
    if (m && !rx.fresh(m)) return REASONS.staleChallenge
    if (m && !rx.generationOk(m)) return REASONS.wrongGeneration
    if (m && rx.unixExpired(m)) return REASONS.expired
    if (halted) return halted
    if (rx.remainingMotionMs() < 1) return REASONS.motionBudget
    return REASONS.invalid
  }
  // Datagrams that claim the active session but fail authentication or validation make the
  // receiver command a stop ('invalid_stop'); ones that do not claim it are simply 'ignored'.
  const forged = m && !verifyCommand(rx.key, m)
  if (outcome === 'invalid_stop') return forged ? REASONS.unauthenticated : REASONS.invalid
  if (outcome === 'stale_stop' || outcome === 'expired_stop') return null // a stop was applied; not a rejection of motion
  if (outcome === 'ignored' && m) {
    if (forged) return REASONS.unauthenticated
    if (m.boot_nonce !== rx.bootNonce) return REASONS.wrongBoot
    if (rx.activeSession !== null && m.session_id < rx.activeSession) return REASONS.oldSession
    if (rx.lastSequence !== null && m.session_id === rx.activeSession && m.sequence <= rx.lastSequence) return REASONS.replay
    return REASONS.unauthenticated
  }
  return REASONS.ignored
}
