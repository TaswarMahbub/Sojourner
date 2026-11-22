// One unswept kinematic step of the skid-steer model: turn first, then advance along the new
// heading. Pure — no world, no collision, no receiver. integrateInWorld feeds the result to the
// swept collision check; anything that needs to predict where a command would take the rover
// (without moving it) calls this directly so the prediction and the simulation cannot diverge.
//
// The operation order is load-bearing: test:artless pins the floating-point output byte-for-byte.
export function stepPose(pose, v, w, dt) {
  const heading = pose.heading + w * dt
  return { x: pose.x + v * Math.cos(heading) * dt, y: pose.y + v * Math.sin(heading) * dt, heading }
}
