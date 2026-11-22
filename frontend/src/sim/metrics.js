// The five numbers (handoff "What to record, per attempt"). Inputs come only from the simulation's
// accepted commands, its fixed-step poses, and collision.js against worldModel — never from a
// rendered mesh (invariant I2). The recorder holds no clock of its own: every event carries the
// simulation time, so the same recorded input yields the same metrics on every machine.
//
//  1. Time to clear: first accepted non-zero command → whole footprint inside the room polygon.
//  2. Collisions and near-misses: a collision is the onset of contact (step blocked, or clearance
//     ≤ CONTACT_TOLERANCE_M); it stays one event until clearance rises back above the tolerance, so
//     one scrape is one event. A near-miss is an episode below NEAR_MISS_THRESHOLD_M (0.05 m) that
//     ends without contact. An episode that reaches contact counts as a collision, not both.
//  3. Path efficiency: distance travelled ÷ worldModel optimal-path length.
//  4. Cues per metre: accepted command transitions ÷ distance travelled. A transition is an accepted
//     command whose (task_state, linear, angular, stop) differs from the previous accepted one, so
//     keep-alive repeats are not cues.
//  5. Hesitation: per committed cue, the dwell since a cue became available; list plus median.
//     Derived from receiver state, not signalled by any UI (ruled by Mridhul, 22 Nov 2026):
//       available = receiver armed, no latch active, output velocities both zero (at rest,
//                   awaiting the next cue). The caller samples this every fixed step.
//       committed = the next ACCEPTED command with a non-zero velocity.
//       dwell     = committed time − the step at which availability last began.
//     If availability ends without a commit (an e-stop, a fault, a disarm), the pending mark is
//     dropped and the next availability starts a fresh one.
//     This is exact for discrete cueing, where every cue starts from rest. It is DEGENERATE in
//     continuous manual mode: while a control is held the output never returns to zero, so few or
//     no dwells are recorded and the median is dominated by the first start from rest.

import { CONTACT_TOLERANCE_M, NEAR_MISS_THRESHOLD_M } from './constants.js'

export function median(values) {
  if (values.length === 0) return null
  const s = [...values].sort((a, b) => a - b)
  const m = s.length >> 1
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}

const commandKey = (c) => `${c.taskState}|${c.stop ? 1 : 0}|${c.stop ? 0 : c.linear}|${c.stop ? 0 : c.angular}`

export const isMotionCommand = (c) => Boolean(c.accepted) && !c.stop && (c.linear !== 0 || c.angular !== 0)

export class MetricsRecorder {
  constructor({ nearMissThresholdM = NEAR_MISS_THRESHOLD_M, contactToleranceM = CONTACT_TOLERANCE_M } = {}) {
    this.nearMissThresholdM = nearMissThresholdM
    this.contactToleranceM = contactToleranceM
    this.reset()
  }

  reset() {
    this.firstMotionMs = null
    this.clearedMs = null
    this.distanceM = 0
    this.collisions = 0
    this.nearMisses = 0
    this.minClearanceM = Infinity
    this.maxPenetrationM = 0
    this.inContact = false
    this.inNear = false
    this.nearTouched = false
    this.cues = 0
    this.lastAcceptedKey = null
    this.wasAvailable = false
    this.cueAvailableMs = null
    this.hesitationsS = []
  }

  /**
   * An accepted or rejected command, after the receiver's verdict. Only accepted commands count.
   * @param {{ tMs:number, accepted:boolean, taskState:string, linear:number, angular:number, stop:boolean }} c
   * @param {boolean} counting whether the attempt is running (transitions before it are tracked, not counted)
   */
  command(c, counting) {
    if (!c.accepted) return
    const motion = isMotionCommand(c)
    if (counting && this.firstMotionMs === null && motion) this.firstMotionMs = c.tMs
    if (counting && motion && this.cueAvailableMs !== null) {
      this.hesitationsS.push((c.tMs - this.cueAvailableMs) / 1000)
      this.cueAvailableMs = null
    }
    const key = commandKey(c)
    if (key !== this.lastAcceptedKey) {
      this.lastAcceptedKey = key
      if (counting) this.cues += 1
    }
  }

  /**
   * Cue availability, sampled from receiver state once per fixed step (see header, item 5).
   * A rising edge opens a dwell; a falling edge with no commit in between drops it.
   */
  availability(available, tMs) {
    if (available && !this.wasAvailable) this.cueAvailableMs = tMs
    else if (!available && this.wasAvailable) this.cueAvailableMs = null
    this.wasAvailable = available
  }

  /**
   * One fixed step.
   * @param {{ from:{x,y}, to:{x,y}, clearance:number, penetration:number, blocked:boolean }} s
   */
  step(s) {
    this.distanceM += Math.hypot(s.to.x - s.from.x, s.to.y - s.from.y)
    if (s.clearance < this.minClearanceM) this.minClearanceM = s.clearance
    if (s.penetration > this.maxPenetrationM) this.maxPenetrationM = s.penetration

    const contact = s.blocked || s.clearance <= this.contactToleranceM
    if (contact && !this.inContact) this.collisions += 1
    this.inContact = contact

    const near = contact || s.clearance < this.nearMissThresholdM
    if (near && !this.inNear) this.nearTouched = false
    if (contact) this.nearTouched = true
    if (!near && this.inNear) this.closeNearEpisode()
    this.inNear = near
  }

  closeNearEpisode() {
    if (!this.nearTouched) this.nearMisses += 1
    this.nearTouched = false
  }

  cleared(tMs) {
    if (this.clearedMs === null) this.clearedMs = tMs
  }

  /** Close any open near-miss episode. Call once, at attempt end. */
  finish() {
    if (this.inNear) {
      this.closeNearEpisode()
      this.inNear = false
    }
  }

  /** The five metrics, plus the inputs a reader needs to check them. */
  summary(optimalPathM) {
    const cleared = this.clearedMs !== null
    return {
      cleared,
      time_to_clear_s: cleared && this.firstMotionMs !== null ? (this.clearedMs - this.firstMotionMs) / 1000 : null,
      collisions: this.collisions,
      near_misses: this.nearMisses,
      min_clearance_m: Number.isFinite(this.minClearanceM) ? this.minClearanceM : null,
      max_penetration_m: this.maxPenetrationM,
      distance_m: this.distanceM,
      optimal_path_m: optimalPathM,
      path_efficiency: cleared && optimalPathM > 0 ? this.distanceM / optimalPathM : null,
      cues: this.cues,
      cues_per_m: this.distanceM > 0 ? this.cues / this.distanceM : null,
      hesitation_s: [...this.hesitationsS],
      hesitation_median_s: median(this.hesitationsS),
    }
  }
}
