import { describe, expect, it } from 'vitest'
import {
  checkPose,
  convexHull,
  convexSegmentDistance,
  convexSegmentPenetration,
  footprintCorners,
  footprintInsidePolygon,
  segmentsIntersect,
  sweep,
} from './collision.js'
import { CORRIDOR_WORLD as W } from './worldModel.js'

const FP = { length: 0.845, width: 0.65 }
const DOWN = -Math.PI / 2

describe('footprint geometry', () => {
  it('is an oriented 0.845 × 0.650 m rectangle centred on the pivot', () => {
    const c = footprintCorners({ x: 1, y: 2, heading: 0 }, FP)
    expect(c[0]).toEqual({ x: 1 + 0.4225, y: 2 - 0.325 })
    expect(c[2]).toEqual({ x: 1 - 0.4225, y: 2 + 0.325 })
    const r = footprintCorners({ x: 0, y: 0, heading: Math.PI / 2 }, FP)
    expect(r[0].x).toBeCloseTo(0.325) // front-right of a left-facing avatar is at +X
    expect(r[0].y).toBeCloseTo(0.4225)
  })

  it('every corner is one swing radius from the pivot at any heading', () => {
    for (let h = 0; h < 6.3; h += 0.37) {
      for (const p of footprintCorners({ x: 3, y: -1, heading: h }, FP)) {
        expect(Math.hypot(p.x - 3, p.y + 1)).toBeCloseTo(Math.hypot(0.845, 0.65) / 2, 12)
      }
    }
  })
})

describe('primitives', () => {
  it('segment intersection, including touching endpoints', () => {
    expect(segmentsIntersect({ x: 0, y: 0 }, { x: 1, y: 1 }, { x: 0, y: 1 }, { x: 1, y: 0 })).toBe(true)
    expect(segmentsIntersect({ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 0 }, { x: 2, y: 5 })).toBe(true)
    expect(segmentsIntersect({ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 0, y: 1 }, { x: 1, y: 1 })).toBe(false)
  })

  it('SAT penetration depth and distance for a square against a segment', () => {
    const sq = [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }, { x: 0, y: 1 }]
    expect(convexSegmentPenetration(sq, { x: -1, y: 0.9 }, { x: 2, y: 0.9 })).toBeCloseTo(0.1, 12)
    expect(convexSegmentPenetration(sq, { x: -1, y: 1.2 }, { x: 2, y: 1.2 })).toBe(0)
    expect(convexSegmentDistance(sq, { x: -1, y: 1.2 }, { x: 2, y: 1.2 })).toBeCloseTo(0.2, 12)
    expect(convexSegmentDistance(sq, { x: 0.2, y: 0.2 }, { x: 0.3, y: 0.3 })).toBe(0) // fully inside
  })

  it('convex hull drops interior points', () => {
    const h = convexHull([{ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 1, y: 1 }, { x: 2, y: 2 }, { x: 0, y: 2 }])
    expect(h).toHaveLength(4)
  })
})

describe('against the corridor world', () => {
  it('spawn clearance is to the back wall: 0.80 − 0.4225 m', () => {
    const r = checkPose(W.spawn, W)
    expect(r.nearestId).toBe('back_wall')
    expect(r.clearance).toBeCloseTo(0.8 - 0.4225, 12)
    expect(r.penetration).toBe(0)
  })

  it('a centred, square avatar in the door reveal has exactly 82.5 mm to each jamb', () => {
    const r = checkPose({ x: 7.5, y: -1.16, heading: DOWN }, W)
    expect(r.penetration).toBe(0)
    expect(r.clearance).toBeCloseTo(0.0825, 12)
    // The rail ends flush with the jambs, so a rail end can tie with the jamb face.
    expect(r.nearestId).toMatch(/^(jamb_|rail_right_)/)
  })

  it('a skewed avatar in the reveal penetrates a jamb, and depth is reported', () => {
    // Cross-section through the centre is 0.650 / cos θ; past ~37° it exceeds the 0.815 m opening.
    const r = checkPose({ x: 7.5, y: -1.16, heading: DOWN + (40 * Math.PI) / 180 }, W)
    expect(r.penetration).toBeGreaterThan(0)
    expect(r.clearance).toBe(0)
  })

  it('the handrail, not the wall, limits lateral motion', () => {
    const r = checkPose({ x: 3, y: 1.01 - 0.325 - 0.01, heading: 0 }, W)
    expect(r.nearestId).toBe('rail_left')
    expect(r.clearance).toBeCloseTo(0.01, 12)
  })

  it('a swept step cannot tunnel: a 1 m jump straight through the end wall is caught', () => {
    const from = { x: W.bounds.x1 - 0.4225 - 0.05, y: 0, heading: 0 }
    const s = sweep(from, { x: from.x + 1, y: 0, heading: 0 }, W)
    expect(s.hit).toBe(true)
    expect(s.segmentId).toBe('end_wall')
    const left = checkPose(s.pose, W)
    expect(left.penetration).toBe(0)
    expect(left.clearance).toBeLessThanOrEqual(0.001 + 1e-12) // stops within 1 mm of contact
  })

  it('a swept turn in place catches a corner clipping the rail', () => {
    const from = { x: 3, y: 1.01 - 0.4, heading: 0 } // side clearance 0.075, swing radius 0.533
    const s = sweep(from, { ...from, heading: 0.5 }, W)
    expect(s.hit).toBe(true)
    expect(s.segmentId).toBe('rail_left')
  })

  it('a clear step passes through unchanged', () => {
    const to = { x: 0.0025, y: 0, heading: 0.008 }
    const s = sweep(W.spawn, to, W)
    expect(s.hit).toBe(false)
    expect(s.pose).toBe(to)
  })

  it('room entry needs the whole footprint inside', () => {
    expect(footprintInsidePolygon({ x: 7.5, y: -1.6425 - 1e-9, heading: DOWN }, FP, W.room)).toBe(true)
    expect(footprintInsidePolygon({ x: 7.5, y: -1.6, heading: DOWN }, FP, W.room)).toBe(false)
  })
})
