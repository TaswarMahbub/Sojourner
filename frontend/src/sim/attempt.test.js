import { describe, expect, it } from 'vitest'
import { Attempt } from './attempt.js'
import { CORRIDOR_WORLD as W } from './worldModel.js'
import { checkPose } from './collision.js'

const go = (tMs, linear = 0.3) => ({ tMs, accepted: true, taskState: 'manual', linear, angular: 0, stop: false })
const DOWN = -Math.PI / 2

// Feed a pose sequence as steps, with clearance computed from the world.
function walk(a, poses, t0 = 0) {
  let t = t0
  for (let i = 1; i < poses.length; i += 1) {
    t += 1000 / 120
    const c = checkPose(poses[i], W)
    a.step({ tMs: t, from: poses[i - 1], to: poses[i], clearance: c.clearance, penetration: c.penetration, blocked: false })
  }
  return t
}

// The optimal path, sampled at 1 cm, with the in-place turns as zero-length heading changes.
function optimalPoses() {
  const [p0, p1, p2] = W.optimalPath
  const h1 = Math.atan2(p1.y - p0.y, p1.x - p0.x)
  const out = []
  const leg = (a, b, h) => {
    const n = Math.ceil(Math.hypot(b.x - a.x, b.y - a.y) / 0.01)
    for (let i = 0; i <= n; i += 1) out.push({ x: a.x + ((b.x - a.x) * i) / n, y: a.y + ((b.y - a.y) * i) / n, heading: h })
  }
  leg(p0, p1, h1)
  // Nudge one micrometre past the goal so the rear edge is strictly inside the room.
  leg(p1, { x: p2.x, y: p2.y - 1e-6 }, DOWN)
  return out
}

describe('Attempt lifecycle', () => {
  it('idle → armed → running on the first accepted non-zero command', () => {
    const a = new Attempt({ world: W })
    expect(a.state).toBe('idle')
    a.command(go(0))
    expect(a.state).toBe('idle') // not armed: nothing starts
    expect(a.arm()).toBe(true)
    a.command({ ...go(10), accepted: false })
    expect(a.state).toBe('armed')
    a.command({ ...go(20), linear: 0, stop: true, taskState: 'idle' })
    expect(a.state).toBe('armed')
    a.command(go(30))
    expect(a.state).toBe('running')
    expect(a.startedMs).toBe(30)
  })

  it('emits exactly once, on room entry, with self-describing provenance', () => {
    const emitted = []
    const provenance = { source: 'synthetic', derived_from: 'contracts.md §3', generated: '2026-11-22', generator: 'test' }
    const a = new Attempt({ world: W, provenance, onEmit: (r) => emitted.push(r) })
    a.arm()
    a.command(go(0))
    walk(a, optimalPoses())
    a.step({ tMs: 1e9, from: { x: 7.5, y: -2, heading: DOWN }, to: { x: 7.5, y: -2.1, heading: DOWN }, clearance: 1, penetration: 0, blocked: false })
    a.reset(2e9)

    expect(emitted).toHaveLength(1)
    const r = emitted[0]
    expect(r.ended_by).toBe('room_entry')
    expect(r.cleared).toBe(true)
    expect(r.schema_version).toBe('1')
    expect(r.world_id).toBe('corridor-v1')
    expect(r.near_miss_threshold_m).toBe(0.05)
    expect(r.timestep_s).toBe(1 / 120)
    expect(r.provenance).toEqual(provenance)
    expect(r.provenance).not.toBe(provenance) // copied, not aliased
    expect(Object.isFrozen(r)).toBe(true)
    expect(r.collisions).toBe(0)
    expect(r.max_penetration_m).toBe(0)
    // Driving the optimal path itself scores (to sampling precision) a path efficiency of 1.
    expect(r.path_efficiency).toBeCloseTo(1, 6)
    expect(r.time_to_clear_s).toBeGreaterThan(0)
  })

  it('the in-place turn at the optimal path approach point is legal through the whole turn', () => {
    const [p0, approach] = W.optimalPath
    const h0 = Math.atan2(approach.y - p0.y, approach.x - p0.x)
    for (let k = 0; k <= 200; k += 1) {
      const c = checkPose({ ...approach, heading: h0 + ((DOWN - h0) * k) / 200 }, W)
      expect(c.penetration).toBe(0)
    }
  })

  it('an explicit reset while running emits once with ended_by "reset"', () => {
    const emitted = []
    const a = new Attempt({ world: W, onEmit: (r) => emitted.push(r) })
    a.arm()
    a.command(go(0))
    walk(a, [W.spawn, { x: 0.1, y: 0, heading: 0 }])
    const r = a.reset(500)
    expect(r.ended_by).toBe('reset')
    expect(r.cleared).toBe(false)
    expect(r.distance_m).toBeCloseTo(0.1, 12)
    expect(r.ended_at_ms).toBe(500)
    expect(emitted).toEqual([r])
    expect(a.state).toBe('idle')
    expect(a.result).toBeNull()
  })

  it('reset while armed or idle emits nothing, and nothing carries into the next attempt', () => {
    const emitted = []
    const a = new Attempt({ world: W, onEmit: (r) => emitted.push(r) })
    a.arm()
    expect(a.reset()).toBeNull()
    a.arm()
    a.command(go(0))
    walk(a, [W.spawn, { x: 0.5, y: 0, heading: 0 }])
    a.reset(1)
    a.arm()
    a.command(go(2))
    const r = a.reset(3)
    expect(emitted).toHaveLength(2)
    expect(r.distance_m).toBe(0)
    expect(r.cues).toBe(1)
  })

  it('provenance defaults to null but the field is always present', () => {
    const a = new Attempt({ world: W })
    a.arm()
    a.command(go(0))
    expect(a.reset(1)).toHaveProperty('provenance', null)
  })
})
