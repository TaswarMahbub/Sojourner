import { describe, expect, it } from 'vitest'
import { ARENA_HALF_M, Simulation } from './simulation.js'
import { CORRIDOR_WORLD } from './worldModel.js'

const T0 = 1795276800000
const DOWN = -Math.PI / 2

// Startup, release the stop, close the cutoff, connect the drill, and arm — as in simulation.test.js.
function armed(options = {}) {
  let unix = T0
  const sim = new Simulation({ bootNonce: 42, unixNow: () => unix, ...options })
  const tick = (ms) => {
    unix += ms
    sim.tick(ms)
  }
  sim.finishStartup()
  sim.setEstopPressed(false)
  sim.closeCutoff()
  sim.connect()
  tick(300)
  sim.requestArm()
  tick(300)
  return { sim, tick }
}

// Drive until a predicate holds, checking every 100 ms (one command cycle). Bounded.
function driveUntil(sim, tick, drive, done, maxMs = 60000) {
  sim.setDrive(drive)
  for (let t = 0; t < maxMs && !done(sim); t += 100) tick(100)
  sim.setDrive(null)
}

describe('Simulation fixed timestep', () => {
  it('rover motion does not depend on how elapsed time is sliced into ticks', () => {
    const runs = [1000, 100, 7, 1].map((slice) => {
      let unix = T0
      const sim = new Simulation({ bootNonce: 42, unixNow: () => unix, world: CORRIDOR_WORLD })
      const tick = (total) => {
        for (let done = 0; done < total; done += slice) {
          const ms = Math.min(slice, total - done)
          unix += ms
          sim.tick(ms)
        }
      }
      sim.finishStartup()
      sim.setEstopPressed(false)
      sim.closeCutoff()
      sim.connect()
      tick(300)
      sim.requestArm()
      tick(300)
      sim.setDrive({ linear: 0.25, angular: 0.4 })
      tick(3000)
      return { pose: sim.pose, timeMs: sim.timeMs }
    })
    for (const r of runs) expect(r).toEqual(runs[0])
  })

  it('time advances in whole 1/120 s steps and the remainder carries', () => {
    const sim = new Simulation({ bootNonce: 1 })
    sim.tick(10)
    expect(sim.timeMs).toBeCloseTo(1000 / 120, 9)
    sim.tick(10)
    expect(sim.timeMs).toBeCloseTo(2000 / 120, 9)
    expect(sim.clock.alpha).toBeCloseTo(0.4, 9)
  })

  it('exposes an interpolated pose for the renderer between the last two steps', () => {
    const { sim, tick } = armed({ world: CORRIDOR_WORLD })
    sim.setDrive({ linear: 0.3, angular: 0 })
    tick(1000)
    tick(4) // half a step banked
    const p = sim.interpolatedPose()
    expect(p.x).toBeGreaterThan(sim.prevPose.x)
    expect(p.x).toBeLessThan(sim.pose.x)
  })
})

describe('Simulation world selection', () => {
  it('defaults to the legacy arena: pose still clamps at ARENA_HALF_M and no attempt exists', () => {
    const { sim, tick } = armed()
    expect(sim.world).toBeNull()
    expect(sim.attempt).toBeNull()
    sim.setDrive({ linear: 0.3, angular: 0 })
    tick(7000)
    expect(sim.pose.x).toBe(ARENA_HALF_M)
  })

  it('the corridor world is opt-in and the rover drives past the old arena edge', () => {
    const { sim, tick } = armed({ world: CORRIDOR_WORLD })
    expect(sim.view().worldId).toBe('corridor-v1')
    sim.setDrive({ linear: 0.3, angular: 0 })
    tick(7000)
    expect(sim.pose.x).toBeGreaterThan(ARENA_HALF_M)
    expect(sim.attempt.state).toBe('running')
  })
})

describe('Simulation in the corridor world', () => {
  it('stalls against the end wall instead of passing through it, and records one collision', () => {
    const { sim, tick } = armed({ world: CORRIDOR_WORLD })
    sim.setDrive({ linear: 0.3, angular: 0 })
    tick(45000) // 12 m of commanded travel; the end wall is 11.4 m away
    expect(sim.contact).toBe('end_wall')
    expect(sim.pose.x + 0.4225).toBeLessThanOrEqual(11.4)
    expect(sim.pose.x + 0.4225).toBeGreaterThan(11.4 - 0.001 - 1e-9)
    expect(sim.clearance.penetration).toBe(0)
    expect(sim.attempt.metrics.collisions).toBe(1)
  })

  it('never moves for rejected datagrams', () => {
    const { sim, tick } = armed({ world: CORRIDOR_WORLD })
    const before = sim.pose
    sim.disconnect()
    for (const kind of ['stale', 'generation', 'badmac', 'wrongboot', 'malformed']) {
      sim.inject(kind)
      tick(20)
    }
    expect(sim.pose).toEqual(before)
    expect(sim.attempt.state).toBe('armed') // a rejected motion command does not start an attempt
  })

  it('a driven traversal through the doorway ends the attempt on room entry and emits once', () => {
    const emitted = []
    const provenance = { source: 'synthetic', derived_from: 'contracts.md §3', generated: '2026-11-22', generator: 'simulation.corridor.test.js' }
    const { sim, tick } = armed({ world: CORRIDOR_WORLD, provenance, onAttemptEmit: (r) => emitted.push(r) })

    driveUntil(sim, tick, { linear: 0.3, angular: 0 }, (s) => s.pose.x >= 7.3)
    driveUntil(sim, tick, { linear: 0.05, angular: 0 }, (s) => s.pose.x >= 7.495)
    tick(300)
    driveUntil(sim, tick, { linear: 0, angular: -0.5 }, (s) => s.pose.heading <= DOWN + 0.12)
    driveUntil(sim, tick, { linear: 0, angular: -0.05 }, (s) => s.pose.heading <= DOWN + 0.004)
    tick(300)
    driveUntil(sim, tick, { linear: 0.15, angular: 0 }, (s) => s.attempt.state === 'emitted', 20000)

    expect(emitted).toHaveLength(1)
    const r = emitted[0]
    expect(r.ended_by).toBe('room_entry')
    expect(r.cleared).toBe(true)
    expect(r.collisions).toBe(0)
    expect(r.near_misses).toBe(0)
    expect(r.min_clearance_m).toBeGreaterThan(0.05)
    expect(r.min_clearance_m).toBeLessThan(0.0825 + 1e-9)
    expect(r.path_efficiency).toBeGreaterThan(1)
    expect(r.path_efficiency).toBeLessThan(1.1)
    expect(r.time_to_clear_s).toBeGreaterThan(r.distance_m / 0.3)
    expect(r.cues).toBeGreaterThanOrEqual(5)
    expect(r.provenance).toEqual(provenance)
    expect(sim.view().attemptResult).toBe(r)
  })

  it('resetAttempt emits a running attempt, returns to spawn, and re-arms', () => {
    const { sim, tick } = armed({ world: CORRIDOR_WORLD })
    sim.setDrive({ linear: 0.3, angular: 0 })
    tick(1000)
    sim.setDrive(null)
    const r = sim.resetAttempt()
    expect(r.ended_by).toBe('reset')
    expect(r.distance_m).toBeGreaterThan(0)
    expect(sim.pose).toEqual(CORRIDOR_WORLD.spawn)
    expect(sim.attempt.state).toBe('armed')
  })

  it('the same input produces the same metrics object every time', () => {
    const run = () => {
      const { sim, tick } = armed({ world: CORRIDOR_WORLD })
      sim.setDrive({ linear: 0.3, angular: 0.2 })
      tick(4000)
      sim.setDrive({ linear: 0.2, angular: -0.3 })
      tick(3000)
      return sim.resetAttempt()
    }
    expect(run()).toEqual(run())
  })

  it('hesitation comes from receiver state: rest while armed to the next accepted motion', () => {
    const { sim, tick } = armed({ world: CORRIDOR_WORLD })
    expect(sim.cueAvailable()).toBe(true)
    tick(1000)
    sim.setDrive({ linear: 0.2, angular: 0 })
    tick(500)
    expect(sim.cueAvailable()).toBe(false) // moving
    sim.setDrive(null)
    tick(500) // drill sends a stop; the receiver comes to rest
    expect(sim.cueAvailable()).toBe(true)
    sim.setDrive({ linear: 0, angular: 0.6 })
    tick(500)
    const r = sim.resetAttempt()
    expect(r.hesitation_s).toHaveLength(2)
    expect(r.hesitation_s[0]).toBeGreaterThanOrEqual(1.0)
    expect(r.hesitation_s[0]).toBeLessThan(1.45)
    expect(r.hesitation_s[1]).toBeGreaterThan(0.2)
    expect(r.hesitation_s[1]).toBeLessThan(0.6)
  })

  it('a latch ends availability and drops the pending dwell', () => {
    const { sim, tick } = armed({ world: CORRIDOR_WORLD })
    sim.setDrive({ linear: 0.2, angular: 0 })
    tick(300) // attempt running
    sim.setDrive(null)
    tick(300)
    expect(sim.cueAvailable()).toBe(true)
    sim.softwareEstop()
    tick(20)
    expect(sim.cueAvailable()).toBe(false)
    sim.setDrive({ linear: 0.2, angular: 0 })
    tick(500) // latched: motion is refused, so nothing commits
    const r = sim.resetAttempt()
    expect(r.hesitation_s).toHaveLength(1) // only the first start from rest
  })
})
