// The intent cone (handoff "Two scene elements that are instrumentation", T8.4): the predicted
// path of the current command over the next 2.0 s, drawn as a flat unlit ribbon on the floor,
// as wide as the footprint, so it shows clearance rather than a centre line. Where the path
// would meet a wall it stops, and the stop is marked: a cone drawn through a wall would be a
// false statement about the world, and a cone cut short at a door jamb is what makes 8 cm of
// clearance legible.
//
// Prediction rules (Mridhul, 22 Nov 2026):
// - The same kinematics as the simulation: stepPose from src/sim/, at the same 1/120 s dt. Not a
//   copy, not an approximation.
// - Invariant I3, clarified: the cone integrates a COPY of the pose, is pure, and never writes
//   back to simulation state.
// - Walls: the existing collision sweep (sim/collision.js). The step is kept at 1/120 s, but the
//   sweep may run every SWEEP_EVERY_STEPS steps to keep the cost down; the cost is measured.
//
// PENDING: stepPose is not on mridhul/sim-3d yet. Until it is, predictPath takes it as a
// parameter; once pushed, it is imported here and the parameter goes.
//
// Excluded from bloom and DOF: the ribbon is tagged as an instrument (userData.instrument) for the
// post chain to skip. Frame mapping only through frame.js (REP-103 → three.js at render time).

import { BufferAttribute, BufferGeometry, DoubleSide, Group, Mesh, MeshBasicMaterial } from 'three'
import { sweep as collisionSweep } from '../sim/collision.js'
import { SIM_STEP_S } from '../sim/constants.js'
import { rosToThree } from './frame.js'

// SOURCE: handoff T8.4, "forward-integrate the current command for 2.0 s".
export const CONE_HORIZON_S = 2.0
export const CONE_STEPS = Math.round(CONE_HORIZON_S / SIM_STEP_S) // 240
// Sweep every 12 steps (0.1 s) instead of every step. PROPOSED, to be confirmed by measurement:
// Mridhul's perf caveat, 22 Nov. The sweep interpolates between the two poses at 1 mm, so a wall
// is never skipped; the cost is that between sweeps the path is checked along a chord of the arc.
export const SWEEP_EVERY_STEPS = 12
// One ribbon cross-section every 4 steps: 61 sections over 2 s. Display only.
export const RIBBON_EVERY_STEPS = 4
// Just above the floor, so the ribbon never z-fights with it. Display only.
export const RIBBON_LIFT_M = 0.004
export const STOP_BAR_DEPTH_M = 0.04

// Task states in which the cone may show: the ones that carry motion (contracts.md §3 MOVE_TASKS).
export const CONE_TASKS = ['manual', 'shared']

// Whether the cone shows: an accepted command is moving the rover in a motion task. Hidden in
// hold, idle, estop, fault, rearm, and whenever the output is at rest.
export function coneVisible(view) {
  return CONE_TASKS.includes(view.taskState) && (view.output.linear !== 0 || view.output.angular !== 0)
}

/**
 * Predict the path of a constant command from a pose. Pure: `pose` is copied, never written.
 * @param {{x:number,y:number,heading:number}} pose  start (the simulation's interpolated pose)
 * @param {{linear:number, angular:number}} command  the command the receiver is applying
 * @param {object} world  a worldModel world
 * @param {object} options
 * @param {(pose, v, w, dt) => object} options.stepPose  the simulation's own step (src/sim)
 * @returns {{ poses: object[], stopped: boolean, stopPose: object|null, segmentId: string|null, steps: number, sweeps: number }}
 *   poses: one per RIBBON_EVERY_STEPS steps, starting at the start pose, ending at the last clear pose.
 */
export function predictPath(pose, command, world, { stepPose, sweep = collisionSweep, steps = CONE_STEPS, sweepEvery = SWEEP_EVERY_STEPS, ribbonEvery = RIBBON_EVERY_STEPS } = {}) {
  if (typeof stepPose !== 'function') throw new Error('predictPath needs the simulation\'s stepPose')
  const start = { x: pose.x, y: pose.y, heading: pose.heading }
  const poses = [start]
  let checked = start // the last pose the sweep cleared
  let current = start
  let sweeps = 0
  for (let i = 1; i <= steps; i += 1) {
    current = stepPose(current, command.linear, command.angular, SIM_STEP_S)
    if (i % sweepEvery === 0 || i === steps) {
      sweeps += 1
      const s = sweep(checked, current, world)
      if (s.hit) {
        poses.push(s.pose)
        return { poses, stopped: true, stopPose: s.pose, segmentId: s.segmentId, steps: i, sweeps }
      }
      checked = current
    }
    if (i % ribbonEvery === 0 || i % sweepEvery === 0 || i === steps) poses.push(current)
  }
  return { poses, stopped: false, stopPose: null, segmentId: null, steps, sweeps }
}

// The two edges of the ribbon at one pose, in ROS ground coordinates: half the width each side,
// perpendicular to the heading. Left is +Y in the body frame (REP-103).
export function ribbonEdges(pose, width) {
  const h = width / 2
  const lx = -Math.sin(pose.heading) * h
  const ly = Math.cos(pose.heading) * h
  return { left: { x: pose.x + lx, y: pose.y + ly }, right: { x: pose.x - lx, y: pose.y - ly } }
}

const MAX_SECTIONS = Math.ceil(CONE_STEPS / RIBBON_EVERY_STEPS) + Math.ceil(CONE_STEPS / SWEEP_EVERY_STEPS) + 2

/**
 * The cone's three.js objects. Buffers are allocated once and refilled each frame.
 * update(path) redraws from predictPath's result; setVisible(bool) shows or hides it.
 */
export function createIntentCone({ width, color = 0x3a5ccc, stopColor = 0xb02a22 }) {
  const positions = new Float32Array(MAX_SECTIONS * 2 * 3)
  const index = []
  for (let i = 0; i < MAX_SECTIONS - 1; i += 1) {
    const a = 2 * i
    index.push(a, a + 1, a + 2, a + 1, a + 3, a + 2)
  }
  const geometry = new BufferGeometry()
  geometry.setAttribute('position', new BufferAttribute(positions, 3))
  geometry.setIndex(index)
  geometry.setDrawRange(0, 0)
  const material = new MeshBasicMaterial({ color, transparent: true, opacity: 0.35, depthWrite: false, side: DoubleSide })
  const ribbon = new Mesh(geometry, material)
  ribbon.frustumCulled = false

  const stopPositions = new Float32Array(4 * 3)
  const stopGeometry = new BufferGeometry()
  stopGeometry.setAttribute('position', new BufferAttribute(stopPositions, 3))
  stopGeometry.setIndex([0, 1, 2, 1, 3, 2])
  const stopMaterial = new MeshBasicMaterial({ color: stopColor, depthWrite: false, side: DoubleSide })
  const stopBar = new Mesh(stopGeometry, stopMaterial)
  stopBar.frustumCulled = false
  stopBar.visible = false

  const object = new Group()
  object.name = 'intentCone'
  object.userData.instrument = true // the post chain skips instruments (no bloom, no DOF)
  object.add(ribbon, stopBar)
  object.visible = false

  const put = (array, i, p) => {
    const t = rosToThree({ x: p.x, y: p.y, z: RIBBON_LIFT_M })
    array[i * 3] = t.x
    array[i * 3 + 1] = t.y
    array[i * 3 + 2] = t.z
  }

  return {
    object,
    update(path) {
      const n = Math.min(path.poses.length, MAX_SECTIONS)
      for (let i = 0; i < n; i += 1) {
        const { left, right } = ribbonEdges(path.poses[i], width)
        put(positions, 2 * i, left)
        put(positions, 2 * i + 1, right)
      }
      geometry.attributes.position.needsUpdate = true
      geometry.setDrawRange(0, Math.max(0, n - 1) * 6)
      stopBar.visible = path.stopped
      if (path.stopped) {
        const p = path.stopPose
        const { left, right } = ribbonEdges(p, width)
        const ahead = { x: Math.cos(p.heading) * STOP_BAR_DEPTH_M, y: Math.sin(p.heading) * STOP_BAR_DEPTH_M }
        put(stopPositions, 0, left)
        put(stopPositions, 1, right)
        put(stopPositions, 2, { x: left.x + ahead.x, y: left.y + ahead.y })
        put(stopPositions, 3, { x: right.x + ahead.x, y: right.y + ahead.y })
        stopGeometry.attributes.position.needsUpdate = true
      }
    },
    setVisible(visible) {
      object.visible = visible
    },
    dispose() {
      geometry.dispose()
      material.dispose()
      stopGeometry.dispose()
      stopMaterial.dispose()
    },
  }
}
