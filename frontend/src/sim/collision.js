// Analytic 2D collision: the avatar footprint as an ORIENTED RECTANGLE (not a capsule — the test
// is 82.5 mm per side and a capsule would either pass through walls or refuse the door) against
// the world's wall segments. No BVH, no physics engine, no solver. Exact and deterministic.
//
// Swept test: each step is split into sub-steps with at most SWEEP_RESOLUTION_M (1 mm) of corner
// travel. Each sub-step is checked as the convex hull of its start and end footprints. For
// translation the hull IS the swept area, so a translating footprint cannot tunnel through
// anything. For rotation the arc between two samples bulges beyond the hull by at most the chord
// sagitta, (1 mm)² / (8 · 0.533 m) ≈ 0.23 µm, which is far below every threshold in use.

import { SWEEP_RESOLUTION_M } from './constants.js'

const EPS = 1e-12

const sub = (a, b) => ({ x: a.x - b.x, y: a.y - b.y })
const dot = (a, b) => a.x * b.x + a.y * b.y
const cross = (a, b) => a.x * b.y - a.y * b.x

/** Footprint corners, counter-clockwise: front-right, front-left, rear-left, rear-right. */
export function footprintCorners(pose, footprint) {
  const c = Math.cos(pose.heading)
  const s = Math.sin(pose.heading)
  const hl = footprint.length / 2
  const hw = footprint.width / 2
  // forward = (c, s), left = (−s, c)
  const at = (f, l) => ({ x: pose.x + f * c - l * s, y: pose.y + f * s + l * c })
  return [at(hl, -hw), at(hl, hw), at(-hl, hw), at(-hl, -hw)]
}

export function pointSegmentDistance(p, a, b) {
  const ab = sub(b, a)
  const len2 = dot(ab, ab)
  const t = len2 > 0 ? Math.max(0, Math.min(1, dot(sub(p, a), ab) / len2)) : 0
  return Math.hypot(p.x - (a.x + ab.x * t), p.y - (a.y + ab.y * t))
}

/** True when the closed segments p1p2 and q1q2 share a point. */
export function segmentsIntersect(p1, p2, q1, q2) {
  const r = sub(p2, p1)
  const s = sub(q2, q1)
  const d1 = cross(r, sub(q1, p1))
  const d2 = cross(r, sub(q2, p1))
  const d3 = cross(s, sub(p1, q1))
  const d4 = cross(s, sub(p2, q1))
  if (((d1 > EPS && d2 < -EPS) || (d1 < -EPS && d2 > EPS)) && ((d3 > EPS && d4 < -EPS) || (d3 < -EPS && d4 > EPS))) return true
  const onSeg = (a, b, p) => Math.min(a.x, b.x) - EPS <= p.x && p.x <= Math.max(a.x, b.x) + EPS && Math.min(a.y, b.y) - EPS <= p.y && p.y <= Math.max(a.y, b.y) + EPS
  if (Math.abs(d1) <= EPS && onSeg(p1, p2, q1)) return true
  if (Math.abs(d2) <= EPS && onSeg(p1, p2, q2)) return true
  if (Math.abs(d3) <= EPS && onSeg(q1, q2, p1)) return true
  if (Math.abs(d4) <= EPS && onSeg(q1, q2, p2)) return true
  return false
}

export function segmentSegmentDistance(p1, p2, q1, q2) {
  if (segmentsIntersect(p1, p2, q1, q2)) return 0
  return Math.min(pointSegmentDistance(p1, q1, q2), pointSegmentDistance(p2, q1, q2), pointSegmentDistance(q1, p1, p2), pointSegmentDistance(q2, p1, p2))
}

/** Point inside (or on) a convex polygon of either winding. */
export function pointInConvexPolygon(p, poly) {
  let sign = 0
  for (let i = 0; i < poly.length; i += 1) {
    const a = poly[i]
    const b = poly[(i + 1) % poly.length]
    const c = cross(sub(b, a), sub(p, a))
    if (Math.abs(c) <= EPS) continue
    const sgn = c > 0 ? 1 : -1
    if (sign === 0) sign = sgn
    else if (sgn !== sign) return false
  }
  return true
}

/** Whole footprint inside a convex polygon (all four corners in). */
export function footprintInsidePolygon(pose, footprint, poly) {
  return footprintCorners(pose, footprint).every((c) => pointInConvexPolygon(c, poly))
}

// Projection of a point set onto an axis.
function project(points, axis) {
  let min = Infinity
  let max = -Infinity
  for (const p of points) {
    const d = dot(p, axis)
    if (d < min) min = d
    if (d > max) max = d
  }
  return [min, max]
}

function edgeNormals(poly) {
  const axes = []
  for (let i = 0; i < poly.length; i += 1) {
    const e = sub(poly[(i + 1) % poly.length], poly[i])
    const len = Math.hypot(e.x, e.y)
    if (len > EPS) axes.push({ x: -e.y / len, y: e.x / len })
  }
  return axes
}

/**
 * Separating-axis overlap between a convex polygon and a segment. Returns the minimum overlap
 * (the penetration depth) when they interpenetrate, or 0 when they are separated or only touching.
 */
export function convexSegmentPenetration(poly, a, b) {
  const segPts = [a, b]
  const axes = edgeNormals(poly)
  const e = sub(b, a)
  const len = Math.hypot(e.x, e.y)
  if (len > EPS) axes.push({ x: -e.y / len, y: e.x / len })
  let minOverlap = Infinity
  for (const axis of axes) {
    const [p0, p1] = project(poly, axis)
    const [s0, s1] = project(segPts, axis)
    // Push-out distance along this axis, the shorter way. (Not min(max) − max(min): on its own
    // normal a segment projects to a point, and that formula would read a crossing as zero overlap.)
    const overlap = Math.min(p1 - s0, s1 - p0)
    if (overlap <= EPS) return 0
    if (overlap < minOverlap) minOverlap = overlap
  }
  return minOverlap
}

/** Distance from a convex polygon to a segment; 0 when they touch or overlap. */
export function convexSegmentDistance(poly, a, b) {
  if (pointInConvexPolygon(a, poly) || pointInConvexPolygon(b, poly)) return 0
  let d = Infinity
  for (let i = 0; i < poly.length; i += 1) {
    d = Math.min(d, segmentSegmentDistance(poly[i], poly[(i + 1) % poly.length], a, b))
    if (d === 0) return 0
  }
  return d
}

/** Convex hull, Andrew's monotone chain, counter-clockwise, collinear points dropped. */
export function convexHull(points) {
  const pts = [...points].sort((p, q) => p.x - q.x || p.y - q.y)
  if (pts.length < 3) return pts
  const half = (list) => {
    const h = []
    for (const p of list) {
      while (h.length >= 2 && cross(sub(h[h.length - 1], h[h.length - 2]), sub(p, h[h.length - 2])) <= EPS) h.pop()
      h.push(p)
    }
    h.pop()
    return h
  }
  return [...half(pts), ...half([...pts].reverse())]
}

/**
 * Static check of one pose against the world.
 * @returns {{ clearance: number, nearestId: string|null, penetration: number, penetratingId: string|null }}
 *   clearance: minimum distance from the footprint to any segment (0 when touching/overlapping).
 *   penetration: maximum SAT penetration depth over all segments (0 when none).
 */
export function checkPose(pose, world) {
  const corners = footprintCorners(pose, world.footprint)
  let clearance = Infinity
  let nearestId = null
  let penetration = 0
  let penetratingId = null
  for (const s of world.segments) {
    const d = convexSegmentDistance(corners, s.a, s.b)
    if (d < clearance) {
      clearance = d
      nearestId = s.id
    }
    const p = convexSegmentPenetration(corners, s.a, s.b)
    if (p > penetration) {
      penetration = p
      penetratingId = s.id
    }
  }
  return { clearance, nearestId, penetration, penetratingId }
}

/** Interpolate a pose linearly in x, y and heading (heading is not wrapped: steps are tiny). */
const lerpPose = (p, q, t) => ({ x: p.x + (q.x - p.x) * t, y: p.y + (q.y - p.y) * t, heading: p.heading + (q.heading - p.heading) * t })

/**
 * Swept check from `from` to `to` across one step.
 * @returns {{ hit: boolean, t: number, pose: object, segmentId: string|null, samples: number }}
 *   On a hit, `pose` is the last sampled pose whose sweep was clear (≤ 1 mm of corner travel short
 *   of contact) and `t` its fraction of the step. On no hit, `pose` is `to` and `t` is 1.
 */
export function sweep(from, to, world) {
  const r = Math.hypot(world.footprint.length, world.footprint.width) / 2
  const cornerTravel = Math.hypot(to.x - from.x, to.y - from.y) + Math.abs(to.heading - from.heading) * r
  const n = Math.max(1, Math.ceil(cornerTravel / SWEEP_RESOLUTION_M))
  let prev = from
  let prevCorners = footprintCorners(from, world.footprint)
  for (let i = 1; i <= n; i += 1) {
    const curr = i === n ? to : lerpPose(from, to, i / n)
    const currCorners = footprintCorners(curr, world.footprint)
    const hull = convexHull([...prevCorners, ...currCorners])
    for (const s of world.segments) {
      if (convexSegmentPenetration(hull, s.a, s.b) > 0) {
        return { hit: true, t: (i - 1) / n, pose: prev, segmentId: s.id, samples: n }
      }
    }
    prev = curr
    prevCorners = currCorners
  }
  return { hit: false, t: 1, pose: to, segmentId: null, samples: n }
}
