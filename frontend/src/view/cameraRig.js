// Phase 2 placeholder camera: a rigid follow behind the avatar, read from the INTERPOLATED pose.
// No damping, so no overshoot; no motion the judge did not cause. Phase 7 replaces this with the
// critically damped spring arm and heading lag (handoff T7.1–T7.3).
//
// The arm is shortened against worldModel's free space (not against visible meshes) so the camera
// stays inside the building where it can — at spawn the back wall is only 0.80 m behind the avatar.
//
// MINIMUM ARM (issue I-1). The arm never shortens below ARM_MIN_M: the swing radius plus a margin,
// so the camera is horizontally outside every corner of the footprint at any heading and the
// subject can never be cut by the near plane. When free space runs out before that (the avatar
// backed up to a wall), the camera is allowed to sit behind the wall, and the rig reports which
// wall meshes the arm passes through so the view can hide them for that frame. Clipping through a
// wall is preferred to clipping through the subject. View-only: nothing here reaches the metrics.

import { SWING_RADIUS_M } from '../sim/constants.js'
import { makeFreeSpace } from './corridor.js'
import { rosToThree } from './frame.js'

export const ARM_BACK_M = 2.6
export const ARM_UP_M = 1.8
export const LOOK_UP_M = 0.9
export const FOV_DEG = 55
export const NEAR_M = 0.05
/** Swing radius + 0.17 m: outside the footprint at any heading, with room for the near plane. */
export const ARM_MIN_M = SWING_RADIUS_M + 0.17
const ARM_MARGIN_M = 0.1
const MARCH_M = 0.01

/** Proper intersection of ground segments p–q and a–b. */
function crosses(p, q, a, b) {
  const d = (u, v, w) => (v.x - u.x) * (w.y - u.y) - (v.y - u.y) * (w.x - u.x)
  const d1 = d(a, b, p)
  const d2 = d(a, b, q)
  const d3 = d(p, q, a)
  const d4 = d(p, q, b)
  return d1 * d2 < 0 && d3 * d4 < 0
}

/**
 * Wall meshes whose face the ground segment from the avatar to the camera crosses. Mesh names in
 * corridor.js are the world segment ids, so these ids are directly the meshes to hide.
 */
export function wallsBetween(world, from, to) {
  const faces = [...world.wallFaces, ...world.segments.filter((s) => s.kind === 'wall')]
  return faces.filter((s) => crosses(from, to, s.a, s.b)).map((s) => s.id)
}

/** Camera placement for a ROS pose: { arm, freeArm, position, target, ground } (Three.js coords). */
export function followCamera(pose, isFree) {
  const bx = -Math.cos(pose.heading)
  const by = -Math.sin(pose.heading)
  const at = (d) => ({ x: pose.x + bx * d, y: pose.y + by * d })

  // Longest arm, out to ARM_BACK_M, whose probe (arm + margin) is still inside the building.
  let freeArm = 0
  for (let d = 0; d <= ARM_BACK_M + 1e-9; d += MARCH_M) {
    if (!isFree(at(d + ARM_MARGIN_M))) break
    freeArm = d
  }
  const arm = Math.max(ARM_MIN_M, Math.min(ARM_BACK_M, freeArm))
  const ground = at(arm)
  return {
    arm,
    freeArm,
    ground,
    position: rosToThree({ x: ground.x, y: ground.y, z: ARM_UP_M }),
    target: rosToThree({ x: pose.x, y: pose.y, z: LOOK_UP_M }),
  }
}

/**
 * @param {import('three').PerspectiveCamera} camera
 * @param {object} world
 * @param {import('three').Object3D} [walls] group whose children are named by segment id
 */
export function createFollowRig(camera, world, walls = null) {
  const isFree = makeFreeSpace(world)
  let hidden = []
  return {
    update(pose) {
      const { position, target, ground } = followCamera(pose, isFree)
      camera.position.set(position.x, position.y, position.z)
      camera.lookAt(target.x, target.y, target.z)
      if (!walls) return
      for (const o of hidden) o.visible = true
      hidden = []
      if (isFree(ground)) return
      for (const id of wallsBetween(world, pose, ground)) {
        const o = walls.getObjectByName(id)
        if (o) { o.visible = false; hidden.push(o) }
      }
    },
  }
}
