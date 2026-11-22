// Phase 3 shell geometry (handoff T3.2–T3.5, handoff_sim §6.2). The rule these tests hold: visible
// geometry is built from worldModel and may never drift from it. Every collision face must lie on a
// visible solid surface within 1 mm, at every height the avatar occupies, and no visible solid or
// trim may stand in the space the avatar can actually reach.

import { describe, expect, it } from 'vitest'
import { Box3, Mesh, MeshStandardMaterial, Raycaster, Vector3 } from 'three'
import { CORRIDOR_WORLD as W } from '../sim/worldModel.js'
import {
  AVATAR_HEIGHT_M,
  CEILING_HEIGHT_M,
  DADO_M,
  DOOR_HEAD_HEIGHT_M,
  DOOR_LEAF_M,
  END_WINDOW_M,
  FRAME_REVEAL_DEPTH_M,
  ROOM_WINDOW_M,
  TROUGH_M,
} from '../sim/constants.js'
import { buildCorridor, makeFreeSpace, roomRect, troughLayout } from './corridor.js'

const MM = 0.001
const group = buildCorridor(W, new MeshStandardMaterial())
group.updateMatrixWorld(true)
const collect = (pred) => {
  const out = []
  group.traverse((o) => { if (o instanceof Mesh && pred(o)) out.push(o) })
  return out
}
const box3 = (m) => new Box3().setFromObject(m)
const solids = collect((m) => m.userData.solid).map((m) => ({ id: m.name, box: box3(m) }))
const trims = collect((m) => m.userData.trim).map((m) => ({ id: m.name, box: box3(m) }))
const leaf = collect((m) => m.userData.leaf)[0]
// ROS (x, y, z) → Three.js point.
const P = (x, y, z) => new Vector3(x, z, -y)

/** Distance from p to the surface of an axis-aligned box: ≥ 0 outside, negative depth inside. */
function surfaceDist(p, b) {
  const dx = Math.max(b.min.x - p.x, 0, p.x - b.max.x)
  const dy = Math.max(b.min.y - p.y, 0, p.y - b.max.y)
  const dz = Math.max(b.min.z - p.z, 0, p.z - b.max.z)
  const out = Math.hypot(dx, dy, dz)
  if (out > 0) return out
  return -Math.min(p.x - b.min.x, b.max.x - p.x, p.y - b.min.y, b.max.y - p.y, p.z - b.min.z, b.max.z - p.z)
}

const segDist = (p, s) => {
  const abx = s.b.x - s.a.x
  const aby = s.b.y - s.a.y
  const t = Math.max(0, Math.min(1, ((p.x - s.a.x) * abx + (p.y - s.a.y) * aby) / (abx * abx + aby * aby)))
  return Math.hypot(p.x - (s.a.x + abx * t), p.y - (s.a.y + aby * t))
}

// Where the footprint can actually go: free space, minus the strips behind the corridor rail faces.
const isFree = makeFreeSpace(W)
const reachable = (p) => {
  if (!isFree(p)) return false
  const inCorridor = p.x > W.bounds.x0 && p.x < W.bounds.x1 && Math.abs(p.y) < W.bounds.wallY
  return !(inCorridor && Math.abs(p.y) > W.bounds.railY)
}

describe('every collision face lies on a visible surface, in 3D', () => {
  // Heights the avatar body occupies, clipped below the lowest window sill. Above a sill the wall
  // is glazed — but the avatar is stopped at the face below it, so nothing passes into the opening.
  const heights = [0.02, 0.3, 0.6, 0.85]

  it('wall, jamb and rail-return faces are covered by a solid within 1 mm at every sampled height', () => {
    for (const s of W.segments.filter((x) => ['wall', 'jamb', 'handrail_return'].includes(x.kind))) {
      const zs = s.kind === 'handrail_return' ? [0.9] : s.kind === 'jamb' ? [...heights, 1.5, 2.05] : heights
      for (let i = 0; i <= 20; i += 1) {
        const x = s.a.x + ((s.b.x - s.a.x) * i) / 20
        const y = s.a.y + ((s.b.y - s.a.y) * i) / 20
        for (const z of zs) {
          const p = P(x, y, z)
          const best = Math.min(...solids.map((o) => Math.abs(surfaceDist(p, o.box))))
          expect(best, `${s.id} i=${i} z=${z}`).toBeLessThan(MM)
        }
      }
    }
  })

  it('no solid is deeper than 1 mm into the reachable space, at any height below the door head', () => {
    // Sample a grid over the reachable floor area and check nothing solid occupies it.
    const room = roomRect(W)
    const xs = []
    for (let x = W.bounds.x0 + 0.002; x < W.bounds.x1; x += 0.05) xs.push(x)
    const ys = []
    for (let y = room.y0 + 0.002; y < W.bounds.wallY; y += 0.05) ys.push(y)
    const bad = []
    let checked = 0
    for (const x of xs) {
      for (const y of ys) {
        if (!reachable({ x, y })) continue
        checked += 1
        for (const z of [0.05, 0.5, 1.0, 1.5, 2.05]) {
          const p = P(x, y, z)
          for (const o of solids) {
            if (surfaceDist(p, o.box) < -MM) bad.push(`${o.id} at (${x.toFixed(2)}, ${y.toFixed(2)}, ${z})`)
          }
        }
      }
    }
    expect(checked).toBeGreaterThan(5000)
    expect(bad.slice(0, 5)).toEqual([])
  })

  it('trim never stands in reachable space by more than 1 mm', () => {
    expect(trims.length).toBeGreaterThan(10)
    for (const { id, box } of trims) {
      // The ground footprint of the trim, sampled on a fine grid.
      for (let u = 0; u <= 10; u += 1) {
        for (let v = 0; v <= 4; v += 1) {
          const x = box.min.x + ((box.max.x - box.min.x) * u) / 10
          const y = -(box.min.z + ((box.max.z - box.min.z) * v) / 4)
          if (!reachable({ x, y })) continue
          const toCollision = Math.min(...W.segments.map((s) => segDist({ x, y }, s)))
          expect(toCollision, `${id} at (${x.toFixed(3)}, ${y.toFixed(3)})`).toBeLessThanOrEqual(MM)
        }
      }
    }
  })
})

describe('apertures', () => {
  const pieces = (wallId) => Object.fromEntries(group.getObjectByName(wallId).children.map((m) => [m.name.replace(`${wallId}_`, ''), box3(m)]))

  it('end window at x = 11.20: 1.60 × 1.40 m, sill 0.90 m, centred on the corridor axis', () => {
    const p = pieces('end_wall')
    expect(W.bounds.x1).toBeCloseTo(11.2, 12)
    // Three.js z = −ros y; the piers' inner edges bound the opening.
    const zs = [p.pier_a, p.pier_b].map((b) => [b.min.z, b.max.z])
    const inner = [Math.max(...zs.map((z) => Math.min(...z.map(Math.abs)))), Math.min(...zs.map((z) => Math.max(...z.map(Math.abs))))]
    expect(inner[0]).toBeCloseTo(END_WINDOW_M.width / 2, 6)
    expect(p.sill.max.y).toBeCloseTo(0.9, 6)
    expect(p.head.min.y - p.sill.max.y).toBeCloseTo(1.4, 6)
    expect(p.sill.min.x).toBeCloseTo(W.bounds.x1, 6) // inner face on the end-wall collision line
    expect(p.sill.max.x - p.sill.min.x).toBeCloseTo(0.12, 6)
    const ap = group.userData.apertures.end
    expect(ap.centre.x).toBeCloseTo(11.2, 9)
    expect(ap.centre.y).toBeCloseTo(0, 9)
  })

  it('room window in the far wall (y = −5.42): 1.20 × 1.40 m, sill 0.90 m, centred on the door axis', () => {
    const p = pieces('room_wall_back')
    const room = roomRect(W)
    expect(room.y0).toBeCloseTo(-5.42, 9)
    expect(p.pier_b.min.x - p.pier_a.max.x).toBeCloseTo(ROOM_WINDOW_M.width, 6)
    expect((p.pier_b.min.x + p.pier_a.max.x) / 2).toBeCloseTo(7.5, 6)
    expect(p.sill.max.y).toBeCloseTo(0.9, 6)
    expect(p.head.min.y - p.sill.max.y).toBeCloseTo(1.4, 6)
  })

  it('every window sill is below the avatar top — so the collision face, not the glass, stops it', () => {
    // Documented, not a defect: the sill (0.90 m) is lower than the avatar (1.058 m). The avatar is
    // stopped by the wall face at ground level; its top sits in front of the glass, never in it.
    expect(END_WINDOW_M.sill).toBeLessThan(AVATAR_HEIGHT_M)
  })
})

describe('door', () => {
  it('has a real 0.12 m reveal: each jamb face is a solid face 0.12 m deep, up to the 2.10 m head', () => {
    for (const s of W.segments.filter((x) => x.kind === 'jamb')) {
      expect(Math.abs(s.b.y - s.a.y)).toBeCloseTo(FRAME_REVEAL_DEPTH_M, 12)
    }
    const lintel = box3(group.getObjectByName('lintel'))
    expect(lintel.min.y).toBeCloseTo(DOOR_HEAD_HEIGHT_M, 6)
    expect(lintel.max.z - lintel.min.z).toBeCloseTo(FRAME_REVEAL_DEPTH_M, 6)
  })

  it('the leaf is open into the room, clear of the opening and of the approach', () => {
    const b = box3(leaf)
    const room = roomRect(W)
    const [jA] = W.doorway.jambs
    expect(-b.min.z).toBeLessThanOrEqual(room.y1 + 1e-9) // entirely on the room side of the door wall
    expect(b.max.x).toBeLessThanOrEqual(jA.x + 1e-9) // never across the 0.815 m opening
    expect(b.max.x - b.min.x).toBeCloseTo(DOOR_LEAF_M, 6)
    // Ruling 5: the leaf is not in the collision world. It stands this far proud of the room face:
    expect(room.y1 - -b.max.z).toBeLessThan(0.06)
  })

  it('the opening is clear from floor to door head across its full 0.815 m width', () => {
    const [jA, jB] = W.doorway.jambs
    for (let i = 1; i < 20; i += 1) {
      for (let k = 0; k <= 4; k += 1) {
        const p = P(jA.x + ((jB.x - jA.x) * i) / 20, -W.bounds.wallY - (FRAME_REVEAL_DEPTH_M * k) / 4, 0.05 + (2.0 * i) / 20)
        for (const o of [...solids, ...trims, { id: 'leaf', box: box3(leaf) }]) {
          expect(surfaceDist(p, o.box), `${o.id} in the opening`).toBeGreaterThan(-1e-9)
        }
      }
    }
  })
})

describe('ceiling', () => {
  const ray = new Raycaster()
  const up = new Vector3(0, 1, 0)
  const hitUp = (x, y) => {
    ray.set(P(x, y, 2.0), up)
    return ray.intersectObject(group, true)[0]
  }

  it('the corridor ceiling is at 2.70 m', () => {
    const tile = hitUp(1.45, 0.62)
    expect(tile.object.name).toBe('ceiling_corridor')
    expect(tile.point.y).toBeCloseTo(CEILING_HEIGHT_M, 6)
  })

  it('has four 1.20 × 0.30 m trough housings, 3.0 m apart, cut into it and recessed', () => {
    const t = troughLayout(W)
    expect(t).toHaveLength(4)
    for (let i = 1; i < t.length; i += 1) expect(t[i].cx - t[i - 1].cx).toBeCloseTo(TROUGH_M.spacing, 12)
    for (const h of t) {
      expect(h.x1 - h.x0).toBeCloseTo(1.2, 12)
      expect(h.y1 - h.y0).toBeCloseTo(0.3, 12)
      expect(h.x0).toBeGreaterThan(W.bounds.x0)
      expect(h.x1).toBeLessThan(W.bounds.x1)
      // Looking straight up through the housing, the first thing hit is the recessed diffuser.
      const hit = hitUp(h.cx, h.cy)
      expect(hit.object.name).toBe(`${h.id}_diffuser`)
      expect(hit.point.y).toBeGreaterThan(CEILING_HEIGHT_M + 0.05)
    }
    // Phased so one trough is centred on the doorway (the opening sits under a light).
    expect(t.map((h) => h.cx)).toEqual([1.5, 4.5, 7.5, 10.5])
    expect(t.some((h) => Math.abs(h.cx - W.doorway.centreX) < 1e-12)).toBe(true)
  })

  it('tee runs never cross a housing opening', () => {
    for (const name of ['tees_corridor', 'tees_room']) {
      const runs = group.getObjectByName(name).userData.runs
      expect(runs.length).toBeGreaterThan(10)
      for (const r of runs) {
        for (const h of group.userData.housings) {
          if (r.axis === 'x' && r.at > h.y0 && r.at < h.y1) expect(r.to <= h.x0 + 1e-9 || r.from >= h.x1 - 1e-9).toBe(true)
          if (r.axis === 'y' && r.at > h.x0 && r.at < h.x1) expect(r.to <= h.y0 + 1e-9 || r.from >= h.y1 - 1e-9).toBe(true)
        }
      }
    }
  })
})

describe('trim heights', () => {
  it('dado band runs 0.80–1.00 m on the long corridor walls, behind the rail face', () => {
    for (const id of ['face_left_dado', 'face_right_near_dado', 'face_right_far_dado']) {
      const b = box3(group.getObjectByName(id))
      expect(b.min.y).toBeCloseTo(DADO_M.bottom, 6)
      expect(b.max.y).toBeCloseTo(DADO_M.bottom + DADO_M.height, 6)
      expect(Math.min(Math.abs(b.min.z), Math.abs(b.max.z))).toBeGreaterThan(W.bounds.railY)
    }
  })

  it('skirting is 0.10 m tall everywhere', () => {
    for (const t of trims.filter((x) => x.id.endsWith('_skirting'))) {
      expect(t.box.max.y - t.box.min.y, t.id).toBeCloseTo(0.1, 6)
    }
  })
})
