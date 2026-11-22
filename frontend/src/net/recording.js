// Recorded motor-command files (src/fixtures/commands/). A recording is what the bridge would have
// delivered: every datagram, valid or not, unmodified, in arrival order, with the sequence number
// it carried and when it arrived (docs/sim-decisions.md §3). It holds no verdicts:
// only motorReceiver.js judges a datagram, so replaying a file re-derives every outcome.
//
// The file says where it came from in `provenance`, and that object is carried into every
// metrics object an attempt emits (answers §4), so a synthetic-derived number can never be
// mistaken for a hardware-derived one.
//
// rover.setup lists the rover-local steps (not network traffic) done at time 0, before the first
// datagram, so the replay receiver starts in the same state the recording did.

import { STEPS_PER_S, Simulation } from '../sim/simulation.js'
import { CORRIDOR_WORLD } from '../sim/worldModel.js'

export const RECORDING_TYPE = 'motor_command_recording'
export const RECORDING_FORMAT_VERSION = 1
export const PROVENANCE_SOURCES = ['synthetic', 'hardware']
export const ROVER_SETUP_STEPS = ['startup_complete', 'estop_sensor_released', 'cutoff_acknowledged']

const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
const nonEmpty = (v) => typeof v === 'string' && v.length > 0

// Checks the file's own shape, not the datagrams': a recording may (and should) contain datagrams
// the receiver will refuse. Returns a list of problems; empty means valid.
export function validateRecording(r) {
  const errors = []
  if (!isObject(r)) return ['recording: must be an object']
  if (r.record_type !== RECORDING_TYPE) errors.push(`record_type: must be ${RECORDING_TYPE}`)
  if (r.format_version !== RECORDING_FORMAT_VERSION) errors.push(`format_version: must be ${RECORDING_FORMAT_VERSION}`)
  if (!nonEmpty(r.scenario)) errors.push('scenario: must be a non-empty string')

  const p = r.provenance
  if (!isObject(p)) errors.push('provenance: must be an object')
  else {
    if (!PROVENANCE_SOURCES.includes(p.source)) errors.push(`provenance.source: must be one of ${PROVENANCE_SOURCES.join(', ')}`)
    for (const field of ['derived_from', 'generated', 'generator']) if (!nonEmpty(p[field])) errors.push(`provenance.${field}: must be a non-empty string`)
  }

  const rover = r.rover
  if (!isObject(rover)) errors.push('rover: must be an object')
  else {
    if (!Number.isSafeInteger(rover.boot_nonce) || rover.boot_nonce < 1) errors.push('rover.boot_nonce: must be a positive integer')
    if (!nonEmpty(rover.controller_id)) errors.push('rover.controller_id: must be a non-empty string')
    if (!Array.isArray(rover.setup) || rover.setup.some((s) => !ROVER_SETUP_STEPS.includes(s))) {
      errors.push(`rover.setup: must list steps from ${ROVER_SETUP_STEPS.join(', ')}`)
    }
  }

  if (!Array.isArray(r.datagrams) || r.datagrams.length === 0) errors.push('datagrams: must be a non-empty array')
  else {
    let last = -Infinity
    r.datagrams.forEach((d, i) => {
      if (!isObject(d)) return errors.push(`datagrams[${i}]: must be an object`)
      if (typeof d.raw !== 'string') errors.push(`datagrams[${i}].raw: must be the datagram text`)
      if (!Number.isFinite(d.receive_ms) || d.receive_ms < 0) errors.push(`datagrams[${i}].receive_ms: must be a non-negative number`)
      else if (d.receive_ms < last) errors.push(`datagrams[${i}].receive_ms: arrivals must be in order`)
      else last = d.receive_ms
      if (d.sequence !== null && !Number.isSafeInteger(d.sequence)) errors.push(`datagrams[${i}].sequence: must be an integer, or null if the datagram has none`)
    })
    if (!Number.isFinite(r.end_ms) || r.end_ms < last) errors.push('end_ms: must be a number at or after the last arrival')
  }
  return errors
}

// Replays a recording into a fresh corridor simulation, delivering each datagram on the 1/120 s
// step it arrived in, then stepping on to end_ms. No drill sender runs: the datagrams in the file
// are the only traffic. Returns the simulation and the receiver's outcome for each datagram.
// onStep(sim), if given, runs after every simulation step.
export function replayRecording(recording, { world = CORRIDOR_WORLD, onAttemptEmit = null, onStep = null } = {}) {
  const sim = new Simulation({
    bootNonce: recording.rover.boot_nonce,
    world,
    provenance: recording.provenance,
    onAttemptEmit,
  })
  const setup = {
    startup_complete: () => sim.finishStartup(),
    estop_sensor_released: () => sim.setEstopPressed(false),
    cutoff_acknowledged: () => sim.closeCutoff(),
  }
  recording.rover.setup.forEach((step) => setup[step]())
  const stepAt = (ms) => Math.round((ms * STEPS_PER_S) / 1000)
  const step = () => {
    sim.step()
    if (onStep) onStep(sim)
  }
  const outcomes = recording.datagrams.map((d) => {
    while (sim.stepCount < stepAt(d.receive_ms)) step()
    return sim.deliver(d.raw, 'file', 'recorded')
  })
  while (sim.stepCount < stepAt(recording.end_ms)) step()
  return { sim, outcomes }
}
