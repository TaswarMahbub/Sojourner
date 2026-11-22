// Fixed-timestep accumulator (invariant I3). Real elapsed time goes in; whole 1/120 s steps come
// out; the remainder carries to the next call and is exposed as `alpha` so the renderer can
// interpolate pose between the last two steps. Render interpolates; render never integrates.
//
// The accumulator is kept in units of steps rather than seconds, so a whole number of milliseconds
// maps to a whole number of steps without float drift (300 ms is exactly 36 steps, every time).

import { MAX_ACCUMULATOR_S, SIM_STEP_S } from './constants.js'

// Absorbs float error in elapsed × rate (e.g. 0.3 × 120 = 35.99999999999999).
const STEP_EPSILON = 1e-9

export class FixedClock {
  /**
   * @param {object} [options]
   * @param {number} [options.stepS] fixed step, seconds (default 1/120)
   * @param {number} [options.maxAccumulatorS] ceiling on banked time, seconds (default 0.25).
   *   Pass Infinity where the caller already bounds elapsed time and wants every millisecond run.
   */
  constructor({ stepS = SIM_STEP_S, maxAccumulatorS = MAX_ACCUMULATOR_S } = {}) {
    if (!(stepS > 0)) throw new RangeError('stepS must be positive')
    this.stepS = stepS
    this.rate = 1 / stepS
    this.maxSteps = maxAccumulatorS * this.rate
    this.accumulator = 0 // in steps, always in [0, 1) between calls
    this.steps = 0 // total steps issued
  }

  /**
   * Bank elapsed real time and return how many whole steps to run now.
   * @param {number} elapsedS seconds since the last call; negative or non-finite counts as zero
   */
  advance(elapsedS) {
    const e = Number.isFinite(elapsedS) && elapsedS > 0 ? elapsedS : 0
    this.accumulator = Math.min(this.accumulator + e * this.rate, this.maxSteps)
    let n = Math.floor(this.accumulator + STEP_EPSILON)
    if (n < 0) n = 0
    this.accumulator = Math.max(0, this.accumulator - n)
    this.steps += n
    return n
  }

  /** Leftover fraction of a step, in [0, 1): the renderer's interpolation factor. */
  get alpha() {
    return this.accumulator
  }

  /** Simulated time issued so far, seconds. Exact multiples of the step. */
  get timeS() {
    return this.steps / this.rate
  }

  reset() {
    this.accumulator = 0
    this.steps = 0
  }
}

/** Linear pose interpolation for render. Yaw takes the short way round. */
export function interpolatePose(prev, curr, alpha) {
  let dYaw = curr.heading - prev.heading
  dYaw = Math.atan2(Math.sin(dYaw), Math.cos(dYaw))
  return {
    x: prev.x + (curr.x - prev.x) * alpha,
    y: prev.y + (curr.y - prev.y) * alpha,
    heading: prev.heading + dYaw * alpha,
  }
}
