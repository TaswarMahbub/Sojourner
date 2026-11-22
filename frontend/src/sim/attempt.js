// Attempt lifecycle: idle → armed → running → emitted → (reset) → idle.
//
//  - arm():        ready to measure; nothing is recorded yet.
//  - running:      begins on the first accepted non-zero command while armed.
//  - emitted:      the structured metrics object is emitted ONCE, on room entry (whole footprint
//                  inside the room polygon) or on an explicit reset while running.
//  - reset():      emits if running, then clears everything. No state carries between attempts (I6).
//
// The emitted object documents its own provenance: schema version, world id, near-miss threshold,
// contact tolerance, timestep, and the command-source provenance passed in (answers §4: a metric
// derived from a synthetic file must be impossible to mistake for a hardware-derived one).

import { CONTACT_TOLERANCE_M, METRICS_SCHEMA_VERSION, NEAR_MISS_THRESHOLD_M, SIM_STEP_S } from './constants.js'
import { footprintInsidePolygon } from './collision.js'
import { MetricsRecorder, isMotionCommand } from './metrics.js'

function deepFreeze(o) {
  if (o && typeof o === 'object' && !Object.isFrozen(o)) {
    for (const v of Object.values(o)) deepFreeze(v)
    Object.freeze(o)
  }
  return o
}

const clone = (v) => (v === undefined ? null : JSON.parse(JSON.stringify(v)))

export class Attempt {
  /**
   * @param {object} options
   * @param {object} options.world worldModel world
   * @param {object|null} [options.provenance] carried verbatim into every emitted object
   * @param {(result: object) => void} [options.onEmit]
   * @param {number} [options.stepS] the simulation timestep, recorded in the object
   */
  constructor({ world, provenance = null, onEmit = null, stepS = SIM_STEP_S }) {
    if (!world) throw new TypeError('Attempt needs a world')
    this.world = world
    this.provenance = clone(provenance)
    this.onEmit = onEmit
    this.stepS = stepS
    this.metrics = new MetricsRecorder({ nearMissThresholdM: NEAR_MISS_THRESHOLD_M, contactToleranceM: CONTACT_TOLERANCE_M })
    this.state = 'idle'
    this.result = null
    this.startedMs = null
  }

  arm() {
    if (this.state === 'idle') this.state = 'armed'
    return this.state === 'armed'
  }

  /** Every command after the receiver's verdict, accepted or not. */
  command(c) {
    if (this.state === 'armed' && isMotionCommand(c)) {
      this.state = 'running'
      this.startedMs = c.tMs
    }
    if (this.state === 'armed' || this.state === 'running') this.metrics.command(c, this.state === 'running')
  }

  cueAvailable(tMs) {
    if (this.state === 'running' || this.state === 'armed') this.metrics.cueAvailable(tMs)
  }

  /**
   * One fixed simulation step, after collision response.
   * @param {{ tMs:number, from:object, to:object, clearance:number, penetration:number, blocked:boolean }} s
   */
  step(s) {
    if (this.state !== 'running') return
    this.metrics.step(s)
    if (footprintInsidePolygon(s.to, this.world.footprint, this.world.room)) {
      this.metrics.cleared(s.tMs)
      this.emit('room_entry', s.tMs)
    }
  }

  /** Explicit reset. Emits first if an attempt was running. Returns the emitted object, if any. */
  reset(tMs = null) {
    const emitted = this.state === 'running' ? this.emit('reset', tMs) : null
    this.metrics.reset()
    this.state = 'idle'
    this.result = null
    this.startedMs = null
    return emitted
  }

  emit(endedBy, tMs) {
    if (this.state !== 'running') return null
    this.metrics.finish()
    const result = deepFreeze({
      schema_version: METRICS_SCHEMA_VERSION,
      world_id: this.world.id,
      frame: this.world.frame,
      ended_by: endedBy,
      provenance: this.provenance,
      near_miss_threshold_m: NEAR_MISS_THRESHOLD_M,
      contact_tolerance_m: CONTACT_TOLERANCE_M,
      timestep_s: this.stepS,
      started_at_ms: this.startedMs,
      ended_at_ms: tMs,
      ...this.metrics.summary(this.world.optimalPathLength),
    })
    this.state = 'emitted'
    this.result = result
    if (this.onEmit) this.onEmit(result)
    return result
  }
}
