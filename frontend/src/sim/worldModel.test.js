import { describe, expect, it } from 'vitest'
import { CORRIDOR_WORLD as W, createCorridorWorld } from './worldModel.js'
import { checkPose } from './collision.js'
import { DOORWAY_CLEARANCE_PER_SIDE_M, NEAR_MISS_THRESHOLD_M } from './constants.js'

const byId = (id) => W.segments.find((s) => s.id === id)

describe('corridor world', () => {
  it('is deterministic and immutable', () => {
    expect(createCorridorWorld()).toEqual(W)
    expect(Object.isFrozen(W)).toBe(true)
    expect(Object.isFrozen(W.segments[0].a)).toBe(true)
  })

  it('spawns at the origin facing +X, in a 12.00 m corridor', () => {
    expect(W.spawn).toEqual({ x: 0, y: 0, heading: 0 })
    expect(W.bounds.x1 - W.bounds.x0).toBeCloseTo(12, 12)
  })

  it('puts the doorway 7.50 m from spawn in the right-hand wall, 0.815 m clear', () => {
    const [j0, j1] = W.doorway.jambs
    expect((j0.x + j1.x) / 2).toBeCloseTo(7.5, 12)
    expect(j1.x - j0.x).toBeCloseTo(0.815, 12)
    expect(j0.y).toBeLessThan(0) // right-hand = −Y in REP-103
    expect(byId('jamb_far').a.x - byId('jamb_near').a.x).toBeCloseTo(0.815, 12)
    expect(Math.abs(byId('jamb_near').b.y - byId('jamb_near').a.y)).toBeCloseTo(0.12, 12)
  })

  it('insets the long-wall collision segments to the handrail face: 2.02 m effective', () => {
    expect(byId('rail_left').a.y - byId('rail_right_near').a.y).toBeCloseTo(2.02, 12)
    expect(W.bounds.wallY * 2).toBeCloseTo(2.2, 12)
  })

  it('interrupts the right rail across the door opening, returning flush with the jambs', () => {
    const [j0, j1] = W.doorway.jambs
    for (const s of W.segments.filter((x) => x.kind === 'handrail' && x.a.y < 0)) {
      const lo = Math.min(s.a.x, s.b.x)
      const hi = Math.max(s.a.x, s.b.x)
      expect(hi <= j0.x + 1e-12 || lo >= j1.x - 1e-12).toBe(true)
    }
    expect(byId('rail_right_near_return').a.x).toBe(j0.x)
    expect(byId('rail_right_far_return').a.x).toBe(j1.x)
  })

  it('builds a room 3.60 m along the corridor and 4.20 m deep behind the door', () => {
    const xs = W.room.map((p) => p.x)
    const ys = W.room.map((p) => p.y)
    expect(Math.max(...xs) - Math.min(...xs)).toBeCloseTo(3.6, 12)
    expect(Math.max(...ys) - Math.min(...ys)).toBeCloseTo(4.2, 12)
    expect((Math.max(...xs) + Math.min(...xs)) / 2).toBeCloseTo(7.5, 12)
    expect(Math.max(...ys)).toBeCloseTo(-1.22, 12)
  })

  it('leaves room to turn in place at spawn without a near-miss', () => {
    for (let k = 0; k < 72; k += 1) {
      const { clearance, penetration } = checkPose({ ...W.spawn, heading: (k * Math.PI) / 36 }, W)
      expect(penetration).toBe(0)
      expect(clearance).toBeGreaterThan(NEAR_MISS_THRESHOLD_M)
    }
  })

  it('the door aperture for the footprint centre is exactly 2 × 82.5 mm', () => {
    expect(W.inscribedRadius).toBeCloseTo(0.325, 12)
    expect(W.centreAperture.width).toBeCloseTo(0.165, 12)
    expect(W.centreAperture.width).toBeCloseTo(2 * DOORWAY_CLEARANCE_PER_SIDE_M, 12)
    expect(W.centreAperture.x1 - W.centreAperture.x0).toBeCloseTo(0.165, 12)
  })

  it('optimal path is the inscribed-circle taut path bending at the inflated near-jamb corner', () => {
    const [start, bend, goal] = W.optimalPath
    expect(start).toEqual({ x: 0, y: 0 })
    expect(bend.x).toBeCloseTo(7.4175, 12)
    expect(bend.y).toBeCloseTo(-0.685, 12)
    expect(goal.x).toBeCloseTo(7.5, 12)
    expect(goal.y).toBeCloseTo(-1.22 - 0.845 / 2, 12)
    expect(Math.hypot(bend.x, bend.y)).toBeCloseTo(7.4491, 4)
    expect(Math.hypot(goal.x - bend.x, goal.y - bend.y)).toBeCloseTo(0.961, 4)
    expect(W.optimalPathLength).toBeCloseTo(8.4101, 4)
    expect(W.straightLineFloor).toBeCloseTo(7.6777, 4)
    expect(W.optimalPathLength).toBeGreaterThan(W.straightLineFloor)
    // The final leg passes through the centre aperture.
    const tAtDoor = (-1.1 - bend.y) / (goal.y - bend.y)
    const xAtDoor = bend.x + (goal.x - bend.x) * tAtDoor
    expect(xAtDoor).toBeGreaterThanOrEqual(W.centreAperture.x0)
    expect(xAtDoor).toBeLessThanOrEqual(W.centreAperture.x1)
  })

  it('the goal pose has the whole footprint in the room and is itself legal', () => {
    const goal = W.optimalPath[2]
    const c = checkPose({ x: goal.x, y: goal.y - 1e-6, heading: -Math.PI / 2 }, W)
    expect(c.penetration).toBe(0)
    expect(c.clearance).toBeGreaterThan(0.08)
  })
})
