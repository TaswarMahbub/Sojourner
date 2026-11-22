// ROS REP-103 → Three.js frame mapping (handoff "Getting that sign right once").
//
// The simulation stays entirely in the ROS frame as plain numbers (x, y, heading) and is converted
// only here, at render time:
//   three.x =  ros.x
//   three.y =  ros.z          (ground plane, 0)
//   three.z = -ros.y
//   three.rotation.y = ros.yaw   (no sign flip)
// A ROS left turn takes +X_ros toward +Y_ros, i.e. +X_three toward −Z_three; a positive Three.js
// rotation about +Y sends (1,0,0) to (cos φ, 0, −sin φ), also toward −Z. So yaw passes through.
// frame.test.js proves it against three's own matrices.

/** A ROS point (z optional, default 0) as a Three.js position triple. */
export const rosToThree = ({ x, y, z = 0 }) => ({ x, y: z, z: -y })

/** ROS yaw (CCW from above, positive = left turn) as a Three.js rotation about +Y. */
export const yawToThree = (yaw) => yaw

/** Place an Object3D at a ROS ground pose. */
export function applyPose(object3d, pose) {
  const p = rosToThree(pose)
  object3d.position.set(p.x, p.y, p.z)
  object3d.rotation.set(0, yawToThree(pose.heading), 0)
}
