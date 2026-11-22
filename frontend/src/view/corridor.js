// Grey-box corridor and room (handoff Phase 2, T2.2), built FROM worldModel — never alongside it.
// Every wall, rail and opening here is placed from the world's segment list and its constants, so
// the visible geometry cannot drift from the collision geometry the metrics use.
//
//  - Long corridor walls: from world.wallFaces, extruded outward by the wall thickness.
//  - Back, end and room walls: from the 'wall' segments, extruded to their solid side.
//  - The room's door-wall segments are not drawn separately: they are the far face of the corridor
//    wall box, which already occupies that volume.
//  - Handrails: from the 'handrail' segments, at HANDRAIL_HEIGHT_M, with the rail face on the
//    collision line and the standoff behind it. Returns from the 'handrail_return' segments.
//  - Jambs are the end faces of the wall boxes either side of the opening; a lintel closes the
//    wall above the door head.
// Phase 3 replaces this with the full architecture; the construction rule stays the same.

import { BoxGeometry, CylinderGeometry, Group, Mesh, PlaneGeometry } from 'three'
import {
  CEILING_HEIGHT_M,
  DOOR_HEAD_HEIGHT_M,
  HANDRAIL_DIAMETER_M,
  HANDRAIL_HEIGHT_M,
  ROOM_CEILING_HEIGHT_M,
  WALL_THICKNESS_M,
} from '../sim/constants.js'
import { rosToThree } from './frame.js'

const EPS = 1e-3

/** Axis-aligned free space of the world: corridor, door reveal, room. Strict interior. */
export function makeFreeSpace(world) {
  const { x0, x1, wallY } = world.bounds
  const [jA, jB] = world.doorway.jambs
  const rx = world.room.map((p) => p.x)
  const ry = world.room.map((p) => p.y)
  const room = { x0: Math.min(...rx), x1: Math.max(...rx), y0: Math.min(...ry), y1: Math.max(...ry) }
  const inRect = (p, r) => p.x > r.x0 && p.x < r.x1 && p.y > r.y0 && p.y < r.y1
  const corridor = { x0, x1, y0: -wallY, y1: wallY }
  const reveal = { x0: jA.x, x1: jB.x, y0: room.y1 - EPS, y1: -wallY + EPS }
  return (p) => inRect(p, corridor) || inRect(p, reveal) || inRect(p, room)
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

/** A box whose inner face lies on a ground segment, extruded `thickness` along `normal`. */
function wallBox(seg, normal, thickness, z0, z1, material) {
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
  mesh.userData = { segmentId: seg.id, face: { a: { ...seg.a }, b: { ...seg.b } }, normal, thickness }
  return mesh
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
  mesh.userData = { segmentId: seg.id }
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
 * Build the grey-box scene geometry from a worldModel world.
 * @param {object} world
 * @param {import('three').Material} material the one neutral grey material
 * @returns {import('three').Group}
 */
export function buildCorridor(world, material) {
  const group = new Group()
  group.name = 'corridor'
  const isFree = makeFreeSpace(world)
  const roomYs = world.room.map((p) => p.y)
  const roomTop = Math.max(...roomYs)

  for (const face of world.wallFaces) {
    group.add(wallBox(face, solidNormal(face, isFree), WALL_THICKNESS_M, 0, CEILING_HEIGHT_M, material))
  }
  for (const seg of world.segments) {
    if (seg.kind === 'wall') {
      if (seg.id.startsWith('room_door_wall')) continue // inside the corridor wall box already
      const inRoom = Math.max(seg.a.y, seg.b.y) <= roomTop + 1e-9
      group.add(wallBox(seg, solidNormal(seg, isFree), WALL_THICKNESS_M, 0, inRoom ? ROOM_CEILING_HEIGHT_M : CEILING_HEIGHT_M, material))
    } else if (seg.kind === 'handrail') {
      const side = Math.sign((seg.a.y + seg.b.y) / 2) // the wall is on the far side from the centreline
      group.add(railMesh(seg, { x: 0, y: side }, HANDRAIL_HEIGHT_M, material))
    } else if (seg.kind === 'handrail_return') {
      // A short bracket from the rail end back to the wall, its corridor-side face on the segment.
      const len = Math.hypot(seg.b.x - seg.a.x, seg.b.y - seg.a.y)
      const m = new Mesh(new BoxGeometry(HANDRAIL_DIAMETER_M, HANDRAIL_DIAMETER_M, len), material)
      const towardSolid = seg.id.includes('near') ? -1 : 1
      const c = rosToThree({ x: seg.a.x + (towardSolid * HANDRAIL_DIAMETER_M) / 2, y: (seg.a.y + seg.b.y) / 2, z: HANDRAIL_HEIGHT_M })
      m.position.set(c.x, c.y, c.z)
      m.name = seg.id
      group.add(m)
    }
    // 'jamb': the end faces of the wall boxes either side of the opening. Nothing extra to draw.
  }

  // Lintel: the wall above the door head, across the opening, through the wall thickness.
  const [jA, jB] = world.doorway.jambs
  group.add(wallBox({ id: 'lintel', a: jA, b: jB }, { x: 0, y: -1 }, world.doorway.revealDepth, DOOR_HEAD_HEIGHT_M, CEILING_HEIGHT_M, material))

  // Floor and ceiling, as three non-overlapping rectangles: corridor, door reveal, room.
  const { x0, x1, wallY } = world.bounds
  const rx = world.room.map((p) => p.x)
  const rects = {
    corridor: { x0, x1, y0: -wallY, y1: wallY, ceiling: CEILING_HEIGHT_M },
    reveal: { x0: jA.x, x1: jB.x, y0: roomTop, y1: -wallY, ceiling: DOOR_HEAD_HEIGHT_M },
    room: { x0: Math.min(...rx), x1: Math.max(...rx), y0: Math.min(...roomYs), y1: roomTop, ceiling: ROOM_CEILING_HEIGHT_M },
  }
  for (const [name, rect] of Object.entries(rects)) {
    group.add(horizontalPlane(rect, 0, true, material, `floor_${name}`))
    group.add(horizontalPlane(rect, rect.ceiling, false, material, `ceiling_${name}`))
  }
  return group
}
