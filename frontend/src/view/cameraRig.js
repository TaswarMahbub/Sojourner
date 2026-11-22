// Phase 2 placeholder camera: a rigid follow behind the avatar, read from the INTERPOLATED pose.
// No damping, so no overshoot; no motion the judge did not cause. Phase 7 replaces this with the
// critically damped spring arm and heading lag (handoff T7.1–T7.3).
//
// The arm is shortened against worldModel's free space (not against visible meshes) so the camera
// never sits behind a wall — at spawn the back wall is only 0.60 m behind the avatar.

import { makeFreeSpace } from './corridor.js'
import { rosToThree } from './frame.js'

export const ARM_BACK_M = 2.6
export const ARM_UP_M = 1.8
export const LOOK_UP_M = 0.9
export const FOV_DEG = 55
const ARM_MIN_M = 0.15
const ARM_MARGIN_M = 0.15
const MARCH_M = 0.05

/** Camera placement for a ROS pose: { position, target } in Three.js coordinates. */
export function followCamera(pose, isFree) {
  const bx = -Math.cos(pose.heading)
  const by = -Math.sin(pose.heading)
  let arm = ARM_MIN_M
  for (let d = ARM_MIN_M; d <= ARM_BACK_M + 1e-9; d += MARCH_M) {
    const probe = { x: pose.x + bx * (d + ARM_MARGIN_M), y: pose.y + by * (d + ARM_MARGIN_M) }
    if (!isFree(probe)) break
    arm = d
  }
  return {
    arm,
    position: rosToThree({ x: pose.x + bx * arm, y: pose.y + by * arm, z: ARM_UP_M }),
    target: rosToThree({ x: pose.x, y: pose.y, z: LOOK_UP_M }),
  }
}

export function createFollowRig(camera, world) {
  const isFree = makeFreeSpace(world)
  return {
    update(pose) {
      const { position, target } = followCamera(pose, isFree)
      camera.position.set(position.x, position.y, position.z)
      camera.lookAt(target.x, target.y, target.z)
    },
  }
}
