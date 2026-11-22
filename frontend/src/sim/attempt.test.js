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

// A polyline sampled at 1 cm, each leg driven at its own heading (heading changes are free here:
// these tests exercise the attempt's bookkeeping, not the drive).
function polylinePoses(points, headings = []) {
  const out = []
  for (let k = 1; k < points.length; k += 1) {
    const a = points[k - 1]
    const b = points[k]
    const h = headings[k - 1] ?? Math.atan2(b.y - a.y, b.x - a.x)
    const n = Math.ceil(Math.hypot(b.x - a.x, b.y - a.y) / 0.01)
    for (let i = k === 1 ? 0 : 1; i <= n; i += 1) out.push({ x: a.x + ((b.x - a.x) * i) / n, y: a.y + ((b.y - a.y) * i) / n, heading: h })
  }
  return out
}

// The optimal path, nudged one micrometre past the goal so the rear edge is strictly inside the room.
function optimalPoses() {
  const [p0, p1, p2] = W.optimalPath
  // The goal is defined at heading −Y, so the final leg is driven at −Y.
  return polylinePoses([p0, p1, { x: p2.x, y: p2.y - 1e-6 }], [undefined, DOWN])
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
    // Tracing the optimal path itself scores (to sampling precision) a path efficiency of 1. It is
    // the inscribed circle's path, so the rectangle tracing it may touch the near jamb: not checked.
    expect(r.path_efficiency).toBeCloseTo(1, 6)
    expect(r.time_to_clear_s).toBeGreaterThan(0)
  })

  it('a route the rectangle can actually drive scores a path efficiency above 1', () => {
    // Straight to the door centreline, turn in place clear of the rail line, straight in.
    const approach = { x: 7.5, y: -W.bounds.railY + Math.hypot(0.845, 0.65) / 2 }
    const goal = { x: 7.5, y: W.optimalPath[2].y - 1e-6 }
    const emitted = []
    const a = new Attempt({ world: W, onEmit: (r) => emitted.push(r) })
    a.arm()
    a.command(go(0))
    const poses = polylinePoses([{ x: 0, y: 0 }, approach, goal])
    walk(a, poses)
    expect(emitted).toHaveLength(1)
    expect(emitted[0].collisions).toBe(0)
    expect(emitted[0].max_penetration_m).toBe(0)
    expect(emitted[0].path_efficiency).toBeGreaterThan(1)
    expect(emitted[0].path_efficiency).toBeLessThan(1.05)
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
