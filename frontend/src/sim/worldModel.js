// The authoritative world, as analytic 2D geometry in the ground plane. Nothing here references a
// mesh. The visible scene is later built FROM these numbers, never alongside them (invariant I2).
//
// Frame: ROS REP-103, +X down the corridor, +Y left. Spawn is the origin, facing +X. The doorway
// is in the right-hand (−Y) wall at DOORWAY_CENTRE_X_M and opens into the clinician room.
//
//              +Y
//               ^      left handrail face, y = +1.01 (continuous)
//   back wall   |  ======================================================  end wall (window)
//   x = −0.60   |  spawn (0,0) → +X
//               |  =========================|  door  |==================   right handrail face, y = −1.01
//                                           |reveal  |                     right wall face,   y = −1.10
//                                 ----------+        +----------           room wall,         y = −1.22
//                                 |        clinician room          |
//                                 |  3.60 m along × 4.20 m deep    |
//                                 ----------------------------------
//
// Handrails (answers §6): the rail face, not the wall face, is the limiting surface, so collision
// segments along the long walls are inset 0.09 m. The rails are interrupted across the door opening
// (ADA 404.2.3), and each rail end returns to the wall flush with the jamb, so the opening stays
// 0.815 m clear.

import {
  AVATAR_LENGTH_M,
  AVATAR_WIDTH_M,
  CORRIDOR_CLEAR_WIDTH_M,
  CORRIDOR_LENGTH_M,
  DOORWAY_CENTRE_X_M,
  DOORWAY_CLEAR_WIDTH_M,
  FRAME_REVEAL_DEPTH_M,
  HANDRAIL_PROJECTION_M,
  ROOM_DEPTH_M,
  ROOM_WIDTH_ALONG_CORRIDOR_M,
  SPAWN_SETBACK_M,
} from './constants.js'

const seg = (id, kind, x1, y1, x2, y2) => Object.freeze({ id, kind, a: Object.freeze({ x: x1, y: y1 }), b: Object.freeze({ x: x2, y: y2 }) })

const pathLength = (pts) => {
  let d = 0
  for (let i = 1; i < pts.length; i += 1) d += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y)
  return d
}

/** Exact swing radius from the footprint, not the rounded SWING_RADIUS_M display constant. */
export const swingRadius = (length = AVATAR_LENGTH_M, width = AVATAR_WIDTH_M) => Math.hypot(length, width) / 2

function deepFreeze(o) {
  for (const v of Object.values(o)) if (v && typeof v === 'object' && !Object.isFrozen(v)) deepFreeze(v)
  return Object.freeze(o)
}

/** Build the corridor world. Pure and deterministic: same constants, same world, every machine. */
export function createCorridorWorld() {
  const x0 = -SPAWN_SETBACK_M // back wall
  const x1 = x0 + CORRIDOR_LENGTH_M // end wall (window)
  const wallY = CORRIDOR_CLEAR_WIDTH_M / 2 // wall faces at ±1.10
  const railY = wallY - HANDRAIL_PROJECTION_M // rail faces at ±1.01
  const jambL = DOORWAY_CENTRE_X_M - DOORWAY_CLEAR_WIDTH_M / 2 // spawn-side jamb
  const jambR = DOORWAY_CENTRE_X_M + DOORWAY_CLEAR_WIDTH_M / 2 // far jamb
  const roomTop = -wallY - FRAME_REVEAL_DEPTH_M // room-side face of the door wall, −1.22
  const roomBottom = roomTop - ROOM_DEPTH_M
  const roomX0 = DOORWAY_CENTRE_X_M - ROOM_WIDTH_ALONG_CORRIDOR_M / 2
  const roomX1 = DOORWAY_CENTRE_X_M + ROOM_WIDTH_ALONG_CORRIDOR_M / 2

  // Every surface the footprint can touch. Order is fixed so results are reproducible.
  const segments = [
    seg('back_wall', 'wall', x0, -wallY, x0, wallY),
    seg('end_wall', 'wall', x1, -wallY, x1, wallY),
    seg('rail_left', 'handrail', x0, railY, x1, railY),
    seg('rail_right_near', 'handrail', x0, -railY, jambL, -railY),
    seg('rail_right_near_return', 'handrail_return', jambL, -railY, jambL, -wallY),
    seg('jamb_near', 'jamb', jambL, -wallY, jambL, roomTop),
    seg('jamb_far', 'jamb', jambR, roomTop, jambR, -wallY),
    seg('rail_right_far_return', 'handrail_return', jambR, -wallY, jambR, -railY),
    seg('rail_right_far', 'handrail', jambR, -railY, x1, -railY),
    seg('room_door_wall_near', 'wall', roomX0, roomTop, jambL, roomTop),
    seg('room_door_wall_far', 'wall', jambR, roomTop, roomX1, roomTop),
    seg('room_wall_near', 'wall', roomX0, roomTop, roomX0, roomBottom),
    seg('room_wall_back', 'wall', roomX0, roomBottom, roomX1, roomBottom),
    seg('room_wall_far', 'wall', roomX1, roomBottom, roomX1, roomTop),
  ]

  // Wall faces for the renderer. The long-wall faces sit behind the rails and are not collision
  // surfaces; the renderer draws walls here and rails at the rail line.
  const wallFaces = [
    seg('face_left', 'wall_face', x0, wallY, x1, wallY),
    seg('face_right_near', 'wall_face', x0, -wallY, jambL, -wallY),
    seg('face_right_far', 'wall_face', jambR, -wallY, x1, -wallY),
  ]

  const room = [
    { x: roomX0, y: roomTop },
    { x: roomX0, y: roomBottom },
    { x: roomX1, y: roomBottom },
    { x: roomX1, y: roomTop },
  ]

  // Optimal legal path: the taut path for the footprint's INSCRIBED CIRCLE (r = half the width,
  // 0.325 m). Ruled by Mridhul, 22 Nov 2026. Any pose the rectangle can legally reach, its inscribed
  // circle can too, so this is a lower-bound reference rather than a manoeuvre the avatar must fly.
  // (The earlier reference line used turns in place, which no real drive achieves at zero cost; it
  // was longer than achievable paths, so efficiency compressed toward 1 and could fall below it.)
  //
  // Construction: inflate every obstacle by r and pull a string from spawn to the goal. The only
  // obstacle the string touches is the near side of the door opening, where the right rail ends at
  // the near jamb, (jambL, −railY). Its inflated corner is taken as the POINT (jambL + r, −railY + r).
  //
  // APPROXIMATION, biased: the true inflated corner is a quarter-circle of radius r about the rail
  // end, which the string would hug; the point corner sits outside that arc, so the denominator is
  // a few cm long and path efficiency reads slightly OPTIMISTIC (closer to 1). An exceptionally
  // tight drive could in principle score a hair under 1.0.
  //
  // Goal: heading −Y on the door centreline, rear edge of the footprint on the room-side face of
  // the door wall — the first pose with the whole footprint inside the room.
  const inscribedR = AVATAR_WIDTH_M / 2
  const spawn = { x: 0, y: 0, heading: 0 }
  const bend = { x: jambL + inscribedR, y: -railY + inscribedR }
  const goal = { x: DOORWAY_CENTRE_X_M, y: roomTop - AVATAR_LENGTH_M / 2 }
  const optimalPath = [{ x: spawn.x, y: spawn.y }, bend, goal]
  // The door opening as seen by the footprint centre: the gap between the inflated jambs.
  // Must equal 2 × DOORWAY_CLEARANCE_PER_SIDE_M (0.165 m).
  const centreAperture = { x0: jambL + inscribedR, x1: jambR - inscribedR, width: DOORWAY_CLEAR_WIDTH_M - 2 * inscribedR }
  // Spawn → goal ignoring walls. Illegal; reference only, never a denominator.
  const straightLineFloor = Math.hypot(goal.x - spawn.x, goal.y - spawn.y)

  return deepFreeze({
    id: 'corridor-v1',
    frame: 'REP-103',
    spawn,
    footprint: { length: AVATAR_LENGTH_M, width: AVATAR_WIDTH_M },
    bounds: { x0, x1, wallY, railY },
    doorway: {
      centreX: DOORWAY_CENTRE_X_M,
      clearWidth: DOORWAY_CLEAR_WIDTH_M,
      jambs: [{ x: jambL, y: -wallY }, { x: jambR, y: -wallY }],
      revealDepth: FRAME_REVEAL_DEPTH_M,
    },
    segments,
    wallFaces,
    room,
    inscribedRadius: inscribedR,
    centreAperture,
    optimalPath,
    optimalPathLength: pathLength(optimalPath),
    straightLineFloor,
  })
}

export const CORRIDOR_WORLD = createCorridorWorld()
