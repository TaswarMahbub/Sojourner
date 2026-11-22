import { describe, expect, it } from 'vitest'
import { CORRIDOR_WORLD as W } from '../sim/worldModel.js'
import { checkPose } from '../sim/collision.js'
import { AVATAR_WIDTH_M } from '../sim/constants.js'
import { CONE_REFRESH_MS, CONE_STEPS, SWEEP_EVERY_STEPS, coneVisible, createConeScheduler, createIntentCone, predictPath, ribbonEdges } from './intentCone.js'

// TEST DOUBLE ONLY, until Mridhul's stepPose (src/sim) is pushed: the handoff T1.3 equations.
// The cone itself never contains kinematics; it takes the simulation's stepPose. When stepPose
// lands, these tests import it instead, and a test checks the cone against the simulation.
const stepPoseDouble = (p, v, w, dt) => {
  const heading = p.heading + w * dt
  return { x: p.x + v * Math.cos(heading) * dt, y: p.y + v * Math.sin(heading) * dt, heading }
}
const opts = { stepPose: stepPoseDouble }

describe('predictPath', () => {
  it('needs the simulation\'s stepPose: it has no kinematics of its own', () => {
    expect(() => predictPath(W.spawn, { linear: 0.3, angular: 0 }, W)).toThrow(/stepPose/)
  })

  it('integrates 2.0 s at 1/120 s from a copy of the pose, never writing to it', () => {
    const start = Object.freeze({ ...W.spawn })
    const path = predictPath(start, { linear: 0.3, angular: 0 }, W, opts)
    expect(path.stopped).toBe(false)
    expect(path.steps).toBe(CONE_STEPS)
    expect(path.poses[0]).toEqual(start)
    expect(path.poses.at(-1).x - start.x).toBeCloseTo(0.6, 9) // 0.3 m/s for 2 s
    expect(start).toEqual(W.spawn)
  })

  it('sweeps every SWEEP_EVERY_STEPS steps, not every step', () => {
    const path = predictPath(W.spawn, { linear: 0.3, angular: 0 }, W, opts)
    expect(path.sweeps).toBe(CONE_STEPS / SWEEP_EVERY_STEPS)
  })

  it('stops at a wall, before it, and names the wall', () => {
    const nearEnd = { x: 10.6, y: 0, heading: 0 }
    const path = predictPath(nearEnd, { linear: 0.3, angular: 0 }, W, opts)
    expect(path.stopped).toBe(true)
    expect(path.segmentId).toBe('end_wall')
    expect(path.poses.at(-1)).toEqual(path.stopPose)
    expect(checkPose(path.stopPose, W).penetration).toBe(0)
    expect(path.stopPose.x + 0.4225).toBeLessThanOrEqual(11.4)
  })

  it('a pure turn on the spot stays in place and turns left for positive angular', () => {
    const path = predictPath(W.spawn, { linear: 0, angular: 0.5 }, W, opts)
    const end = path.poses.at(-1)
    expect(end.x).toBeCloseTo(W.spawn.x, 12)
    expect(end.heading - W.spawn.heading).toBeCloseTo(1.0, 9)
  })
})

describe('coneVisible', () => {
  const view = (taskState, linear, angular = 0) => ({ taskState, output: { linear, angular } })
  it('shows while a motion task is moving the rover', () => {
    expect(coneVisible(view('manual', 0.2))).toBe(true)
    expect(coneVisible(view('shared', 0, 0.6))).toBe(true)
  })
  it('is hidden in hold, in any stop, and at rest', () => {
    for (const t of ['hold', 'idle', 'estop', 'fault', 'rearm']) expect(coneVisible(view(t, 0.2))).toBe(false)
    expect(coneVisible(view('manual', 0))).toBe(false)
  })
})

describe('ribbon', () => {
  it('is the footprint width, centred on the path, left = +Y in the body frame', () => {
    const { left, right } = ribbonEdges({ x: 1, y: 0, heading: 0 }, AVATAR_WIDTH_M)
    expect(left).toEqual({ x: 1, y: AVATAR_WIDTH_M / 2 })
    expect(right.y).toBeCloseTo(-AVATAR_WIDTH_M / 2, 12)
  })

  it('draws the predicted path, maps through frame.js, and marks a wall stop', () => {
    const cone = createIntentCone({ width: AVATAR_WIDTH_M })
    const [ribbon, stopBar] = cone.object.children
    expect(cone.object.userData.instrument).toBe(true)

    const clear = predictPath(W.spawn, { linear: 0.3, angular: 0 }, W, opts)
    cone.update(clear)
    expect(ribbon.geometry.drawRange.count).toBe((clear.poses.length - 1) * 6)
    expect(stopBar.visible).toBe(false)
    const pos = ribbon.geometry.attributes.position.array
    expect(pos[2]).toBeCloseTo(-(W.spawn.y + AVATAR_WIDTH_M / 2), 6) // three.z = −ros.y, left edge

    cone.update(predictPath({ x: 10.6, y: 0, heading: 0 }, { linear: 0.3, angular: 0 }, W, opts))
    expect(stopBar.visible).toBe(true)
    cone.setVisible(true)
    expect(cone.object.visible).toBe(true)
    cone.dispose()
  })
})

describe('createConeScheduler (10 Hz)', () => {
  const drive = { linear: 0.3, angular: 0 }
  const counting = () => {
    let calls = 0
    return { compute: () => ({ n: ++calls }), get calls() { return calls } }
  }

  it('reuses the path for the same command within 100 ms of simulation time', () => {
    const s = createConeScheduler()
    const c = counting()
    const first = s.pathAt(1000, drive, c.compute)
    for (let t = 1000; t < 1100; t += 1000 / 60) expect(s.pathAt(t, drive, c.compute)).toBe(first)
    expect(c.calls).toBe(1)
    s.pathAt(1100, drive, c.compute)
    expect(c.calls).toBe(2)
  })

  it('recomputes at once when the command changes', () => {
    const s = createConeScheduler()
    const c = counting()
    s.pathAt(1000, drive, c.compute)
    s.pathAt(1010, { linear: 0.3, angular: 0.6 }, c.compute)
    expect(c.calls).toBe(2)
  })

  it('recomputes after reset, and if simulation time goes backwards (a new attempt)', () => {
    const s = createConeScheduler()
    const c = counting()
    s.pathAt(1000, drive, c.compute)
    s.reset()
    s.pathAt(1010, drive, c.compute)
    s.pathAt(0, drive, c.compute)
    expect(c.calls).toBe(3)
  })

  it('refreshes at the status period', () => {
    expect(CONE_REFRESH_MS).toBe(100)
  })
})
