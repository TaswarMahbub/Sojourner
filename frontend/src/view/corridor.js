// Corridor and room architecture (handoff Phase 2 T2.2, Phase 3 T3.2–T3.4), built FROM worldModel —
// never alongside it. Every wall, rail and opening is placed from the world's segment list and the
// shared constants, so the visible geometry cannot drift from the collision geometry the metrics use.
//
//  - Long corridor walls: from world.wallFaces, extruded outward by the wall thickness.
//  - Back, end and room walls: from the 'wall' segments, extruded to their solid side. The end wall
//    and the room's far wall carry window apertures; the aperture is cut ABOVE the sill, so at every
//    height the avatar can reach, the collision face is still a visible solid face.
//  - The room's door-wall segments are the far face of the corridor wall box, already drawn.
//  - Handrails: from the 'handrail' segments, at HANDRAIL_HEIGHT_M, with the rail face on the
//    collision line. Returns from the 'handrail_return' segments.
//  - Jambs are the end faces of the wall boxes either side of the opening: the real 0.12 m reveal.
//    A lintel closes the wall above the 2.10 m door head.
//  - Trim (skirting, dado band, architrave) sits behind the rail face on the long corridor walls.
//    On a wall whose face IS a collision surface it is a flush band under the 1 mm tolerance.
//  - Door leaf: opened ~180° into the room, flat against the room side of the door wall. Not in the
//    collision world (ruling 5).
//  - Suspended ceiling at 2.70 m with a 600 mm tee grid and recessed trough housings cut into it.
//
// Every structural box is tagged userData.solid; every trim box userData.trim. The geometry tests
// walk those tags. Mesh names are segment ids, so the camera rig can hide a wall by id.

import {
  BackSide,
  BoxGeometry,
  CylinderGeometry,
  Group,
  InstancedMesh,
  Matrix4,
  Mesh,
  PlaneGeometry,
  Quaternion,
  Shape,
  ShapeGeometry,
  Vector3,
} from 'three'
import {
  ARCHITRAVE_M,
  CEILING_HEIGHT_M,
  CEILING_TEE_M,
  CEILING_TILE_M,
  DADO_M,
  DOOR_HEAD_HEIGHT_M,
  DOOR_LEAF_HEIGHT_M,
  DOOR_LEAF_M,
  DOOR_LEAF_THICKNESS_M,
  DOOR_STOP_GAP_M,
  END_WINDOW_M,
  FLUSH_TRIM_PROUD_M,
  HANDRAIL_DIAMETER_M,
  HANDRAIL_HEIGHT_M,
  ROOM_CEILING_HEIGHT_M,
  ROOM_TASK_LIGHT,
  ROOM_WINDOW_M,
  SKIRTING_M,
  TROUGH_M,
  WALL_THICKNESS_M,
} from '../sim/constants.js'
import { rosToThree } from './frame.js'

const EPS = 1e-3

/** Axis-aligned free space of the world: corridor, door reveal, room. Strict interior. */
export function makeFreeSpace(world) {
  const { x0, x1, wallY } = world.bounds
  const [jA, jB] = world.doorway.jambs
  const room = roomRect(world)
  const inRect = (p, r) => p.x > r.x0 && p.x < r.x1 && p.y > r.y0 && p.y < r.y1
  const corridor = { x0, x1, y0: -wallY, y1: wallY }
  const reveal = { x0: jA.x, x1: jB.x, y0: room.y1 - EPS, y1: -wallY + EPS }
  return (p) => inRect(p, corridor) || inRect(p, reveal) || inRect(p, room)
}

export function roomRect(world) {
  const rx = world.room.map((p) => p.x)
  const ry = world.room.map((p) => p.y)
  return { x0: Math.min(...rx), x1: Math.max(...rx), y0: Math.min(...ry), y1: Math.max(...ry) }
}

/**
 * Trough housings: TROUGH_M.count along the corridor centreline, every TROUGH_M.spacing, phased so
 * one trough is centred on the doorway (x = 1.5, 4.5, 7.5, 10.5). That trough sits square in front
 * of the far jamb's reveal face — the face the camera sees head-on on the approach — so the reveal
 * reads brighter than the wall beside it, which it lights only at a grazing angle (handoff T5.6).
 */
export function troughLayout(world) {
  const n = TROUGH_M.count
  const k0 = Math.ceil((world.bounds.x0 + TROUGH_M.length / 2 - world.doorway.centreX) / TROUGH_M.spacing)
  return Array.from({ length: n }, (_, i) => {
    const cx = world.doorway.centreX + (k0 + i) * TROUGH_M.spacing
    return { id: `trough_${i}`, cx, cy: 0, x0: cx - TROUGH_M.length / 2, x1: cx + TROUGH_M.length / 2, y0: -TROUGH_M.width / 2, y1: TROUGH_M.width / 2 }
  })
}

/** The room task-light housing: its long side runs across the room (along Y), over the couch. */
export function roomTaskLightRect() {
  const { x, y, length, width } = ROOM_TASK_LIGHT
  return { id: 'room_task_light', cx: x, cy: y, x0: x - width / 2, x1: x + width / 2, y0: y - length / 2, y1: y + length / 2 }
}

/** Unit normal pointing to a segment's solid side (away from free space). */
function solidNormal(seg, isFree) {
  const dx = seg.b.x - seg.a.x
  const dy = seg.b.y - seg.a.y
  const len = Math.hypot(dx, dy)
  const n = { x: -dy / len, y: dx / len }
  const mid = { x: (seg.a.x + seg.b.x) / 2, y: (seg.a.y + seg.b.y) / 2 }
  return isFree({ x: mid.x + n.x * EPS, y: mid.y + n.y * EPS }) ? { x: -n.x, y: -n.y } : n
}

const neg = (n) => ({ x: -n.x, y: -n.y })

/** The part of a segment between distances s0 and s1 along it. */
function subSegment(seg, s0, s1, id) {
  const len = Math.hypot(seg.b.x - seg.a.x, seg.b.y - seg.a.y)
  const ux = (seg.b.x - seg.a.x) / len
  const uy = (seg.b.y - seg.a.y) / len
  return { id, a: { x: seg.a.x + ux * s0, y: seg.a.y + uy * s0 }, b: { x: seg.a.x + ux * s1, y: seg.a.y + uy * s1 } }
}

/** A box whose inner face lies on a ground segment, extruded `thickness` along `normal`. */
function wallBox(seg, normal, thickness, z0, z1, material, tag = 'solid') {
  const dx = seg.b.x - seg.a.x
  const dy = seg.b.y - seg.a.y
  const length = Math.hypot(dx, dy)
  const mesh = new Mesh(new BoxGeometry(length, z1 - z0, thickness), material)
  const c = rosToThree({
    x: (seg.a.x + seg.b.x) / 2 + (normal.x * thickness) / 2,
    y: (seg.a.y + seg.b.y) / 2 + (normal.y * thickness) / 2,
    z: (z0 + z1) / 2,
  })
  mesh.position.set(c.x, c.y, c.z)
  mesh.rotation.y = Math.atan2(dy, dx)
  mesh.name = seg.id
  mesh.userData = { [tag]: true, segmentId: seg.id, face: { a: { ...seg.a }, b: { ...seg.b } }, normal, thickness, z0, z1 }
  return mesh
}

/** Trim on a host wall: tagged with the host's id so it hides with the wall. */
function trimBox(seg, host, normal, proud, z0, z1, material) {
  const m = wallBox(seg, normal, proud, z0, z1, material, 'trim')
  m.userData.host = host
  return m
}

/**
 * A wall with a window aperture: left and right full-height piers, a sill piece below and a head
 * piece above. Returned as a Group named by the segment id so it can be hidden as one wall.
 */
function wallWithAperture(seg, normal, thickness, z0, z1, ap, material) {
  const len = Math.hypot(seg.b.x - seg.a.x, seg.b.y - seg.a.y)
  const c = len / 2
  const w0 = c - ap.width / 2
  const w1 = c + ap.width / 2
  const head = ap.sill + ap.height
  const group = new Group()
  group.name = seg.id
  const piece = (s0, s1, za, zb, suffix) => {
    const m = wallBox(subSegment(seg, s0, s1, `${seg.id}_${suffix}`), normal, thickness, za, zb, material)
    m.userData.segmentId = seg.id
    group.add(m)
  }
  piece(0, w0, z0, z1, 'pier_a')
  piece(w1, len, z0, z1, 'pier_b')
  piece(w0, w1, z0, ap.sill, 'sill')
  piece(w0, w1, head, z1, 'head')
  const mid = subSegment(seg, w0, w1, 'aperture')
  group.userData = {
    aperture: {
      ...ap,
      head,
      centre: { x: (mid.a.x + mid.b.x) / 2, y: (mid.a.y + mid.b.y) / 2 },
      // Outward = the solid side of the wall face; the glazing and the sky sit out there.
      outward: normal,
      thickness,
      along: { x: (seg.b.x - seg.a.x) / len, y: (seg.b.y - seg.a.y) / len },
    },
  }
  return group
}

/** A horizontal cylinder along a ground segment, its near face on the segment, at height z. */
function railMesh(seg, normal, z, material) {
  const dx = seg.b.x - seg.a.x
  const dy = seg.b.y - seg.a.y
  const length = Math.hypot(dx, dy)
  const r = HANDRAIL_DIAMETER_M / 2
  const mesh = new Mesh(new CylinderGeometry(r, r, length, 12), material)
  const c = rosToThree({ x: (seg.a.x + seg.b.x) / 2 + normal.x * r, y: (seg.a.y + seg.b.y) / 2 + normal.y * r, z })
  mesh.position.set(c.x, c.y, c.z)
  // The cylinder's axis is local +Y: roll it onto local X, then yaw it onto the segment.
  mesh.rotation.set(0, Math.atan2(dy, dx), Math.PI / 2, 'YXZ')
  mesh.name = seg.id
  mesh.userData = { segmentId: seg.id, rail: true }
  return mesh
}

function horizontalPlane(rect, z, facingUp, material, name) {
  const mesh = new Mesh(new PlaneGeometry(rect.x1 - rect.x0, rect.y1 - rect.y0), material)
  const c = rosToThree({ x: (rect.x0 + rect.x1) / 2, y: (rect.y0 + rect.y1) / 2, z })
  mesh.position.set(c.x, c.y, c.z)
  mesh.rotation.x = facingUp ? -Math.PI / 2 : Math.PI / 2
  mesh.name = name
  return mesh
}

/**
 * A downward-facing ceiling plane over `rect` at height z with rectangular holes cut in it.
 * Built in local (u, v) = (ros x, −ros y) and rotated +90° about X, which maps local (u, v, 0) to
 * Three.js (u, 0, v) and the local +Z normal to −Y: facing down.
 */
function holedCeiling(rect, holes, z, material, name) {
  const loop = (r, cw) => {
    const pts = [[r.x0, -r.y1], [r.x1, -r.y1], [r.x1, -r.y0], [r.x0, -r.y0]]
    return cw ? pts.reverse() : pts
  }
  const shape = new Shape()
  loop(rect, false).forEach(([u, v], i) => (i ? shape.lineTo(u, v) : shape.moveTo(u, v)))
  for (const h of holes) {
    const hole = new Shape()
    loop(h, true).forEach(([u, v], i) => (i ? hole.lineTo(u, v) : hole.moveTo(u, v)))
    shape.holes.push(hole)
  }
  const mesh = new Mesh(new ShapeGeometry(shape), material)
  mesh.rotation.x = Math.PI / 2
  mesh.position.y = z
  mesh.name = name
  mesh.userData = { ceiling: true, height: z }
  return mesh
}

/** A recessed housing above a ceiling hole: an open-bottomed box seen from inside, plus a diffuser. */
function housing(h, ceilingZ, mats) {
  const group = new Group()
  group.name = h.id
  const w = h.x1 - h.x0
  const d = h.y1 - h.y0
  const box = new Mesh(new BoxGeometry(w, TROUGH_M.depth, d), mats.recess)
  const c = rosToThree({ x: h.cx, y: h.cy, z: ceilingZ + TROUGH_M.depth / 2 })
  box.position.set(c.x, c.y, c.z)
  box.name = `${h.id}_recess`
  group.add(box)
  // Diffuser panel just below the top of the recess, facing down. The emissive practical (6.3).
  const diffuser = horizontalPlane({ x0: h.x0 + 0.01, x1: h.x1 - 0.01, y0: h.y0 + 0.01, y1: h.y1 - 0.01 }, ceilingZ + TROUGH_M.depth - 0.005, false, mats.diffuser, `${h.id}_diffuser`)
  diffuser.userData = { diffuser: true, rect: h, z: diffuser.position.y }
  group.add(diffuser)
  group.userData = { housing: true, rect: h, ceilingZ }
  return group
}

/** Split [lo, hi] by removing every (a, b) interval that the line actually passes through. */
function clipInterval(lo, hi, cuts) {
  let parts = [[lo, hi]]
  for (const [a, b] of cuts) {
    parts = parts.flatMap(([p, q]) => (b <= p || a >= q ? [[p, q]] : [[p, a], [b, q]].filter(([s, t]) => t - s > 1e-6)))
  }
  return parts
}

/** The exposed tee grid of a suspended ceiling, as one InstancedMesh, clipped around housings. */
function teeGrid(rect, holes, z, phaseX, phaseY, material, name) {
  const runs = [] // { axis: 'x'|'y', at, from, to }
  const linesAt = (lo, hi, phase) => {
    const out = []
    const k0 = Math.ceil((lo - phase) / CEILING_TILE_M + 1e-9)
    for (let k = k0; phase + k * CEILING_TILE_M < hi - 1e-9; k += 1) out.push(phase + k * CEILING_TILE_M)
    return out
  }
  for (const y of linesAt(rect.y0, rect.y1, phaseY)) {
    const cuts = holes.filter((h) => y > h.y0 && y < h.y1).map((h) => [h.x0, h.x1])
    for (const [a, b] of clipInterval(rect.x0, rect.x1, cuts)) runs.push({ axis: 'x', at: y, from: a, to: b })
  }
  for (const x of linesAt(rect.x0, rect.x1, phaseX)) {
    const cuts = holes.filter((h) => x > h.x0 && x < h.x1).map((h) => [h.y0, h.y1])
    for (const [a, b] of clipInterval(rect.y0, rect.y1, cuts)) runs.push({ axis: 'y', at: x, from: a, to: b })
  }
  const mesh = new InstancedMesh(new BoxGeometry(1, 1, 1), material, runs.length)
  const m = new Matrix4()
  const q = new Quaternion()
  const s = new Vector3()
  runs.forEach((r, i) => {
    const len = r.to - r.from
    const mid = (r.from + r.to) / 2
    const p = rosToThree(r.axis === 'x' ? { x: mid, y: r.at, z: z - CEILING_TEE_M.drop / 2 } : { x: r.at, y: mid, z: z - CEILING_TEE_M.drop / 2 })
    // Three.js X = ROS x; Three.js Z = −ROS y. A run along ROS y is a run along Three.js Z.
    if (r.axis === 'x') s.set(len, CEILING_TEE_M.drop, CEILING_TEE_M.width)
    else s.set(CEILING_TEE_M.width, CEILING_TEE_M.drop, len)
    m.compose(new Vector3(p.x, p.y, p.z), q, s)
    mesh.setMatrixAt(i, m)
  })
  mesh.instanceMatrix.needsUpdate = true
  mesh.name = name
  mesh.userData = { teeGrid: true, runs }
  return mesh
}

/**
 * Resolve the material set. A single Material (the Phase 2 call shape, and the tests') is used for
 * every surface; a recess clone is made because the housing interior is drawn back-faced.
 */
function resolveMaterials(m) {
  const set = m.isMaterial ? { surface: m } : { ...m }
  const owned = []
  for (const k of ['trim', 'ceiling', 'door', 'rail', 'diffuser', 'grid']) set[k] = set[k] ?? set.surface
  if (!set.recess) {
    set.recess = (set.ceiling ?? set.surface).clone()
    set.recess.side = BackSide
    owned.push(set.recess)
  }
  return { mats: set, owned }
}

/**
 * Build the scene architecture from a worldModel world.
 * @param {object} world
 * @param {import('three').Material | Record<string, import('three').Material>} materials
 * @returns {import('three').Group}
 */
export function buildCorridor(world, materials) {
  const { mats, owned } = resolveMaterials(materials)
  const group = new Group()
  group.name = 'corridor'
  group.userData.ownedMaterials = owned
  const isFree = makeFreeSpace(world)
  const room = roomRect(world)
  const { x0, x1, wallY, railY } = world.bounds
  const [jA, jB] = world.doorway.jambs

  const walls = new Group()
  walls.name = 'walls'
  const trim = new Group()
  trim.name = 'trim'
  group.add(walls, trim)

  // --- Walls -------------------------------------------------------------------------------------
  for (const face of world.wallFaces) {
    walls.add(wallBox(face, solidNormal(face, isFree), WALL_THICKNESS_M, 0, CEILING_HEIGHT_M, mats.surface))
  }
  for (const seg of world.segments) {
    if (seg.kind === 'wall') {
      if (seg.id.startsWith('room_door_wall')) continue // inside the corridor wall box already
      const n = solidNormal(seg, isFree)
      const inRoom = Math.max(seg.a.y, seg.b.y) <= room.y1 + 1e-9
      const top = inRoom ? ROOM_CEILING_HEIGHT_M : CEILING_HEIGHT_M
      if (seg.id === 'end_wall') walls.add(wallWithAperture(seg, n, WALL_THICKNESS_M, 0, top, END_WINDOW_M, mats.surface))
      else if (seg.id === 'room_wall_back') walls.add(wallWithAperture(seg, n, WALL_THICKNESS_M, 0, top, ROOM_WINDOW_M, mats.surface))
      else walls.add(wallBox(seg, n, WALL_THICKNESS_M, 0, top, mats.surface))
    } else if (seg.kind === 'handrail') {
      const side = Math.sign((seg.a.y + seg.b.y) / 2) // the wall is on the far side from the centreline
      group.add(railMesh(seg, { x: 0, y: side }, HANDRAIL_HEIGHT_M, mats.rail))
    } else if (seg.kind === 'handrail_return') {
      // A short bracket from the rail end back to the wall, its corridor-side face on the segment.
      const len = Math.hypot(seg.b.x - seg.a.x, seg.b.y - seg.a.y)
      const m = new Mesh(new BoxGeometry(HANDRAIL_DIAMETER_M, HANDRAIL_DIAMETER_M, len), mats.rail)
      const towardSolid = seg.id.includes('near') ? -1 : 1
      const c = rosToThree({ x: seg.a.x + (towardSolid * HANDRAIL_DIAMETER_M) / 2, y: (seg.a.y + seg.b.y) / 2, z: HANDRAIL_HEIGHT_M })
      m.position.set(c.x, c.y, c.z)
      m.name = seg.id
      m.userData = { solid: true, segmentId: seg.id }
      group.add(m)
    }
    // 'jamb': the end faces of the wall boxes either side of the opening — the 0.12 m reveal.
  }

  // Lintel: the wall above the door head, across the opening, through the wall thickness.
  walls.add(wallBox({ id: 'lintel', a: jA, b: jB }, { x: 0, y: -1 }, world.doorway.revealDepth, DOOR_HEAD_HEIGHT_M, CEILING_HEIGHT_M, mats.surface))

  // --- Trim --------------------------------------------------------------------------------------
  // Long corridor walls: the avatar is held 0.09 m off them by the rail, so trim may stand proud.
  for (const face of world.wallFaces) {
    const toFree = neg(solidNormal(face, isFree))
    trim.add(trimBox({ ...face, id: `${face.id}_skirting` }, face.id, toFree, SKIRTING_M.proud, 0, SKIRTING_M.height, mats.trim))
    trim.add(trimBox({ ...face, id: `${face.id}_dado` }, face.id, toFree, DADO_M.proud, DADO_M.bottom, DADO_M.bottom + DADO_M.height, mats.trim))
  }
  // Walls whose face is itself a collision surface: flush bands only.
  for (const seg of world.segments.filter((s) => s.kind === 'wall')) {
    const toFree = neg(solidNormal(seg, isFree))
    trim.add(trimBox({ ...seg, id: `${seg.id}_skirting` }, seg.id, toFree, FLUSH_TRIM_PROUD_M, 0, SKIRTING_M.height, mats.trim))
    if (seg.id === 'back_wall') {
      trim.add(trimBox({ ...seg, id: `${seg.id}_dado` }, seg.id, toFree, FLUSH_TRIM_PROUD_M, DADO_M.bottom, DADO_M.bottom + DADO_M.height, mats.trim))
    } else if (seg.id === 'end_wall') {
      // The dado band would cross the window (sill 0.90 < band top 1.00): run it on the piers only.
      const len = Math.hypot(seg.b.x - seg.a.x, seg.b.y - seg.a.y)
      const w0 = len / 2 - END_WINDOW_M.width / 2
      const w1 = len / 2 + END_WINDOW_M.width / 2
      for (const [s0, s1, k] of [[0, w0, 'a'], [w1, len, 'b']]) {
        trim.add(trimBox(subSegment(seg, s0, s1, `${seg.id}_dado_${k}`), seg.id, toFree, FLUSH_TRIM_PROUD_M, DADO_M.bottom, DADO_M.bottom + DADO_M.height, mats.trim))
      }
    }
  }

  // --- Door: architrave on the corridor face, leaf open into the room ------------------------------
  const door = new Group()
  door.name = 'door'
  group.add(door)
  const archZ1 = DOOR_HEAD_HEIGHT_M + ARCHITRAVE_M.width
  const toCorridor = { x: 0, y: 1 }
  const archSeg = (xa, xb, id) => ({ id, a: { x: xa, y: -wallY }, b: { x: xb, y: -wallY } })
  door.add(wallBox(archSeg(jA.x - ARCHITRAVE_M.width, jA.x, 'architrave_near'), toCorridor, ARCHITRAVE_M.proud, 0, archZ1, mats.trim, 'trim'))
  door.add(wallBox(archSeg(jB.x, jB.x + ARCHITRAVE_M.width, 'architrave_far'), toCorridor, ARCHITRAVE_M.proud, 0, archZ1, mats.trim, 'trim'))
  door.add(wallBox(archSeg(jA.x, jB.x, 'architrave_head'), toCorridor, ARCHITRAVE_M.proud, DOOR_HEAD_HEIGHT_M, archZ1, mats.trim, 'trim'))
  // Leaf: hinged at the near jamb on the room side, swung ~180° to lie along the room face of the
  // door wall with a 10 mm stop gap. Out of the opening and out of the approach.
  const leafY = room.y1 - DOOR_STOP_GAP_M
  const leaf = wallBox({ id: 'door_leaf', a: { x: jA.x - DOOR_LEAF_M, y: leafY }, b: { x: jA.x, y: leafY } }, { x: 0, y: -1 }, DOOR_LEAF_THICKNESS_M, 0.005, DOOR_LEAF_HEIGHT_M, mats.door, 'leaf')
  door.add(leaf)

  // --- Floor ---------------------------------------------------------------------------------------
  const rects = {
    corridor: { x0, x1, y0: -wallY, y1: wallY },
    reveal: { x0: jA.x, x1: jB.x, y0: room.y1, y1: -wallY },
    room,
  }
  for (const [name, rect] of Object.entries(rects)) {
    const f = horizontalPlane(rect, 0, true, mats.surface, `floor_${name}`)
    f.userData = { floor: true }
    group.add(f)
  }
  const seam = horizontalPlane({ x0, x1, y0: -0.002, y1: 0.002 }, 0.0015, true, mats.trim, 'floor_seam')
  group.add(seam)

  // --- Ceilings ----------------------------------------------------------------------------------
  // The reveal's ceiling is the underside of the lintel, at the 2.10 m door head.
  const ceiling = new Group()
  ceiling.name = 'ceiling'
  group.add(ceiling)
  const troughs = troughLayout(world)
  const task = roomTaskLightRect()
  ceiling.add(holedCeiling(rects.corridor, troughs, CEILING_HEIGHT_M, mats.ceiling, 'ceiling_corridor'))
  ceiling.add(holedCeiling(room, [task], ROOM_CEILING_HEIGHT_M, mats.ceiling, 'ceiling_room'))
  for (const t of troughs) ceiling.add(housing(t, CEILING_HEIGHT_M, mats))
  ceiling.add(housing(task, ROOM_CEILING_HEIGHT_M, mats))
  // Grid phase: corridor rows straddle the centreline so each trough sits in one tile row, and the
  // cross tees land on the trough ends (spacing 3.0 m = 5 tiles). The room grid is phased to its walls.
  const half = CEILING_TILE_M / 2
  ceiling.add(teeGrid(rects.corridor, troughs, CEILING_HEIGHT_M, troughs[0].x0, half, mats.grid, 'tees_corridor'))
  ceiling.add(teeGrid(room, [task], ROOM_CEILING_HEIGHT_M, room.x0, room.y0, mats.grid, 'tees_room'))

  // Hand the apertures to the lighting rig: it places glazing, sky planes and window fill from these.
  group.userData.apertures = {
    end: walls.getObjectByName('end_wall').userData.aperture,
    room: walls.getObjectByName('room_wall_back').userData.aperture,
  }
  group.userData.housings = [...troughs, task]
  group.userData.railY = railY
  return group
}

/** Dispose every geometry under the corridor and the materials it created for itself. */
export function disposeCorridor(group) {
  group.traverse((o) => o.geometry?.dispose())
  for (const m of group.userData.ownedMaterials ?? []) m.dispose()
}
