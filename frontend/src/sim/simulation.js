// The 2D simulator: one simulated receiver boot, the drill sender, the rover's position, and the
// command history. Time is simulated: tick(ms) banks elapsed time in a fixed-timestep accumulator
// (clock.js) and the physics, receiver clock and status cadence all advance in whole 1/120 s steps,
// so a dropped or long frame can never change how far the rover moves (invariant I3).
//
// World: by default the legacy 3 × 3 m arena (pose clamped to ±ARENA_HALF_M), which the existing
// 2D panel draws. Pass { world: CORRIDOR_WORLD } to opt into the analytic corridor (worldModel.js):
// the rover starts at the world's spawn, collides with the walls, handrails and door jambs as an
// oriented 0.650 × 0.845 m rectangle (collision.js), and an attempt records the five metrics and
// emits them once on room entry or reset (attempt.js).
//
// The rover moves only while the receiver reports torque (motorsHaveTorque), using the velocities
// the receiver applied. A datagram the receiver ignored or rejected never moves the rover
// (contract: the simulator "does not animate that datagram as rover motion").
//
// The receiver publishes a status every 100 ms. The status is a drill status: it comes from this
// simulated receiver and cannot arm a physical rover.

import { MotorReceiver } from './motorReceiver.js'
import { DrillSender } from './drillSender.js'
import { utf8 } from './hmac.js'
import { signCommand } from '../contracts/motor.js'
import { FixedClock, interpolatePose } from './clock.js'
import { MAX_ANGULAR_RAD_S, MAX_LINEAR_MPS, SIM_STEP_S } from './constants.js'
import { checkPose, sweep } from './collision.js'
import { Attempt } from './attempt.js'

// The drill key and controller. They are not rover secrets and must never be provisioned.
export const DRILL_KEY = utf8('sojourner-simulator-drill-key-not-for-hardware')
export const DRILL_CONTROLLER = 'sim-drill'

export const STATUS_PERIOD_MS = 100
// Legacy arena half-width, used only when no world is passed to the constructor.
export const ARENA_HALF_M = 1.5
export const STEPS_PER_S = Math.round(1 / SIM_STEP_S) // 120
// Status cadence is checked against step-multiple times; this absorbs float error in the difference.
const TIME_EPSILON_MS = 1e-6
const clamp = (v, lim) => Math.max(-lim, Math.min(lim, v))
export const HISTORY_LIMIT = 60

// What each receiver outcome means, for the history.
export const OUTCOMES = {
  accepted_start: { kind: 'accepted', text: 'Accepted: session started (stopped, disarmed)' },
  accepted_rearm: { kind: 'accepted', text: 'Accepted: rearmed (latches cleared, still stopped)' },
  accepted_motion: { kind: 'accepted', text: 'Accepted: motion applied' },
  accepted_stop: { kind: 'accepted', text: 'Accepted: stop applied' },
  stale_stop: { kind: 'stop', text: 'Older safety stop applied, not accepted' },
  expired_stop: { kind: 'stop', text: 'Expired safety stop applied, not accepted' },
  rejected_motion: { kind: 'rejected', text: 'Rejected motion: safe output commanded' },
  rejected_rearm: { kind: 'rejected', text: 'Rejected rearm: not armed' },
  invalid_stop: { kind: 'rejected', text: 'Invalid for the active session: safe output commanded' },
  ignored: { kind: 'ignored', text: 'Ignored: no effect' },
  malformed: { kind: 'ignored', text: 'Malformed: ignored' },
  frozen: { kind: 'ignored', text: 'Firmware frozen: not processed' },
}

let nextEntryId = 1

export class Simulation {
  /**
   * @param {object} [options]
   * @param {number} [options.bootNonce]
   * @param {() => number} [options.unixNow]
   * @param {object|null} [options.world] a worldModel world (e.g. CORRIDOR_WORLD); null keeps the legacy arena
   * @param {object|null} [options.provenance] command-source provenance, carried into emitted metrics
   * @param {(result: object) => void} [options.onAttemptEmit] receives each emitted metrics object
   * @param {number} [options.maxFrameMs] accumulator ceiling per tick. Infinity by default: tick(ms) is a
   *   request to advance simulated time by ms, and the real-time caller already bounds ms.
   */
  constructor({
    bootNonce = 1 + Math.floor(Math.random() * 1e6),
    unixNow = () => Date.now(),
    world = null,
    provenance = null,
    onAttemptEmit = null,
    maxFrameMs = Infinity,
  } = {}) {
    this.unixNow = unixNow
    this.world = world
    this.clock = new FixedClock({ stepS: SIM_STEP_S, maxAccumulatorS: maxFrameMs / 1000 })
    this.stepCount = 0
    this.receiver = new MotorReceiver(bootNonce, { key: DRILL_KEY, controllerId: DRILL_CONTROLLER })
    this.sender = new DrillSender({ key: DRILL_KEY, controllerId: DRILL_CONTROLLER, unixNow })
    this.timeMs = 0
    this.lastStatusMs = -STATUS_PERIOD_MS
    this.status = null
    this.drive = null // { linear, angular } while the operator holds a drive control
    this.senderEnabled = false
    this.lastRaw = null
    this.pose = world ? { ...world.spawn } : { x: 0, y: 0, heading: 0 } // meters and radians; heading 0 faces +X
    this.prevPose = this.pose // pose at the previous step, for render interpolation
    this.trail = [{ x: this.pose.x, y: this.pose.y }]
    this.history = []
    this.clearance = world ? checkPose(this.pose, world) : null
    this.contact = null // id of the segment that blocked the last step, or null
    this.attempt = world ? new Attempt({ world, provenance, onEmit: onAttemptEmit, stepS: SIM_STEP_S }) : null
    if (this.attempt) this.attempt.arm()
  }

  // ---- Local rover controls. These model physical inputs on the rover, not network traffic. ----

  finishStartup() { this.receiver.completeStartup(); this.publishStatus() }
  setEstopPressed(pressed) { this.receiver.setSensor(pressed ? 'asserted' : 'released'); this.publishStatus() }
  closeCutoff() { const ok = this.receiver.acknowledgeCutoff(); this.publishStatus(); return ok }
  cutMotorPower() { this.receiver.loseMotorPower(); this.publishStatus() }
  restoreMotorPower() { this.receiver.restoreMotorPower(); this.publishStatus() }
  freezeFirmware() { this.receiver.freezeFirmware() }
  unfreezeFirmware() { this.receiver.unfreezeFirmware() }
  reboot() {
    this.receiver.brownoutReset(1 + Math.floor(Math.random() * 1e6))
    this.stepCount = 0
    this.timeMs = 0
    this.lastStatusMs = -STATUS_PERIOD_MS
    this.publishStatus()
  }

  // ---- Operator controls on the (simulated) laptop. ----

  connect() { this.senderEnabled = true }
  disconnect() { this.senderEnabled = false; this.drive = null }
  requestArm() { this.sender.requestArm() }
  setDrive(drive) { this.drive = drive }
  softwareEstop() {
    const out = this.sender.softwareEstop(this.timeMs)
    if (out) this.send(out.message, 'drill', out.why)
    else this.log({ label: 'Software e-stop not sent', source: 'drill', kind: 'ignored', text: 'No current authenticated status to copy from' })
  }

  // ---- Hostile datagrams, injected to show that the receiver refuses them. ----

  inject(kind) {
    const s = this.status
    if (kind === 'replay') {
      if (!this.lastRaw) return
      return this.deliver(this.lastRaw, 'injected', 'Replay of the last datagram')
    }
    if (!s) return
    const base = {
      schema_version: '4',
      controller_id: DRILL_CONTROLLER,
      boot_nonce: s.boot_nonce,
      session_id: s.session_id ?? 1,
      sequence: (s.last_sequence ?? 0) + 1,
      freshness_id: s.freshness_id,
      rearm_generation: s.rearm_generation,
      status_challenge: 1,
      issued_at_ms: this.unixNow(),
      expires_at_ms: this.unixNow() + 250,
      task_state: 'manual',
      linear_mps: 0.3,
      angular_rad_s: 0,
      stop: false,
    }
    const signed = (m) => ({ ...m, mac: signCommand(DRILL_KEY, m) })
    if (kind === 'stale') return this.deliver(JSON.stringify(signed({ ...base, freshness_id: Math.max(1, s.freshness_id - 1) })), 'injected', 'Full-speed motion sealed under an old challenge')
    if (kind === 'generation') return this.deliver(JSON.stringify(signed({ ...base, rearm_generation: s.rearm_generation + 1 })), 'injected', 'Full-speed motion with the wrong rearm generation')
    if (kind === 'badmac') {
      const m = signed(base)
      return this.deliver(JSON.stringify({ ...m, mac: (m.mac[0] === 'f' ? 'e' : 'f') + m.mac.slice(1) }), 'injected', 'Full-speed motion with a forged MAC')
    }
    if (kind === 'wrongboot') return this.deliver(JSON.stringify(signed({ ...base, boot_nonce: s.boot_nonce + 1 })), 'injected', 'Full-speed motion for another boot')
    if (kind === 'malformed') return this.deliver(`{"boot_nonce": ${s.boot_nonce}, "session_id": ${s.session_id ?? 1}, "sequence": `, 'injected', 'Truncated datagram naming the active session')
  }

  // ---- Attempts (corridor world only). ----

  // The cue source signals that a cue can now be committed (feeds the hesitation metric).
  markCueAvailable() { if (this.attempt) this.attempt.cueAvailable(this.timeMs) }

  // Emits the running attempt (if any), returns the rover to spawn, and re-arms. Receiver untouched.
  resetAttempt() {
    if (!this.attempt) return null
    const emitted = this.attempt.reset(this.timeMs)
    this.pose = { ...this.world.spawn }
    this.prevPose = this.pose
    this.trail = [{ x: this.pose.x, y: this.pose.y }]
    this.clearance = checkPose(this.pose, this.world)
    this.contact = null
    this.attempt.arm()
    return emitted
  }

  // ---- Time. ----

  // Bank ms of elapsed time and run however many whole fixed steps it covers; the remainder carries.
  tick(ms) {
    const steps = this.clock.advance(ms / 1000)
    for (let i = 0; i < steps; i += 1) this.step()
  }

  // Pose for the renderer: between the last two steps, by the clock's leftover fraction.
  interpolatedPose() { return interpolatePose(this.prevPose, this.pose, this.clock.alpha) }

  step() {
    this.stepCount += 1
    this.timeMs = (this.stepCount * 1000) / STEPS_PER_S // exact at every 12-step (100 ms) boundary
    this.receiver.setMonotonic(Math.floor(this.timeMs))
    this.prevPose = this.pose
    this.integrate(SIM_STEP_S)
    if (this.timeMs - this.lastStatusMs >= STATUS_PERIOD_MS - TIME_EPSILON_MS) {
      this.publishStatus()
      if (this.senderEnabled) {
        const out = this.sender.next(this.receiver.monotonicMs, this.drive)
        if (out) this.send(out.message, 'drill', out.why)
      }
    }
  }

  publishStatus() {
    this.lastStatusMs = this.timeMs
    const status = this.receiver.status() // null while the firmware is frozen
    if (!status) return
    this.status = status
    this.sender.receiveStatus(status, this.receiver.monotonicMs)
  }

  integrate(dt) {
    if (!this.receiver.motorsHaveTorque()) return
    if (this.world) return this.integrateInWorld(dt)
    const v = this.receiver.linearMps
    const w = this.receiver.angularRadS
    const p = this.pose
    const heading = p.heading + w * dt
    const x = Math.max(-ARENA_HALF_M, Math.min(ARENA_HALF_M, p.x + v * Math.cos(heading) * dt))
    const y = Math.max(-ARENA_HALF_M, Math.min(ARENA_HALF_M, p.y + v * Math.sin(heading) * dt))
    this.pose = { x, y, heading }
    const last = this.trail[this.trail.length - 1]
    if (Math.hypot(x - last.x, y - last.y) > 0.02) this.trail = [...this.trail.slice(-300), { x, y }]
  }

  // Skid-steer about the footprint centre, swept against the world. A step that would make contact
  // stops at the last clear swept sample (within 1 mm of the surface) instead of passing through:
  // the rover stalls against the obstacle, and the contact is recorded.
  integrateInWorld(dt) {
    const v = clamp(this.receiver.linearMps, MAX_LINEAR_MPS)
    const w = clamp(this.receiver.angularRadS, MAX_ANGULAR_RAD_S)
    const from = this.pose
    const heading = from.heading + w * dt
    const target = { x: from.x + v * Math.cos(heading) * dt, y: from.y + v * Math.sin(heading) * dt, heading }
    const swept = sweep(from, target, this.world)
    const to = swept.pose
    this.pose = to
    this.contact = swept.hit ? swept.segmentId : null
    this.clearance = checkPose(to, this.world)
    this.attempt.step({
      tMs: this.timeMs,
      from,
      to,
      clearance: this.clearance.clearance,
      penetration: this.clearance.penetration,
      blocked: swept.hit,
    })
    const last = this.trail[this.trail.length - 1]
    if (Math.hypot(to.x - last.x, to.y - last.y) > 0.02) this.trail = [...this.trail.slice(-300), { x: to.x, y: to.y }]
  }

  // ---- Datagrams and history. ----

  send(message, source, why) {
    this.deliver(JSON.stringify(message), source, why)
  }

  deliver(raw, source, why) {
    const outcome = this.receiver.handleRaw(raw)
    if (source === 'drill') this.lastRaw = raw
    let label
    let m = null
    try {
      m = JSON.parse(raw)
      label = `${m.task_state} #${m.sequence}${m.stop ? '' : ` ${m.linear_mps} m/s, ${m.angular_rad_s} rad/s`}`
    } catch {
      label = 'Unparseable datagram'
    }
    if (this.attempt && m && typeof m === 'object') {
      this.attempt.command({
        tMs: this.timeMs,
        accepted: OUTCOMES[outcome]?.kind === 'accepted',
        taskState: m.task_state,
        linear: Number(m.linear_mps) || 0,
        angular: Number(m.angular_rad_s) || 0,
        stop: Boolean(m.stop),
      })
    }
    this.log({ label, source, why, outcome, ...OUTCOMES[outcome] })
    this.publishStatus()
    return outcome
  }

  // Consecutive entries with the same label shape and outcome collapse into one with a count,
  // so a stream of keep-alive stops does not push everything else off the list.
  log(entry) {
    const shape = `${entry.source}|${entry.why}|${entry.outcome}|${entry.label.replace(/#\d+/, '')}`
    const top = this.history[0]
    if (top && top.shape === shape) {
      this.history = [{ ...top, label: entry.label, count: top.count + 1, timeMs: this.timeMs }, ...this.history.slice(1)]
      return
    }
    this.history = [{ ...entry, id: nextEntryId++, shape, count: 1, timeMs: this.timeMs }, ...this.history].slice(0, HISTORY_LIMIT)
  }

  // A plain snapshot for the UI.
  view() {
    const rx = this.receiver
    return {
      timeMs: this.timeMs,
      pose: this.pose,
      prevPose: this.prevPose,
      alpha: this.clock.alpha,
      worldId: this.world ? this.world.id : null,
      clearance: this.clearance,
      contact: this.contact,
      attemptState: this.attempt ? this.attempt.state : null,
      attemptResult: this.attempt ? this.attempt.result : null,
      trail: this.trail,
      torque: rx.motorsHaveTorque(),
      output: { linear: rx.linearMps, angular: rx.angularRadS },
      local: {
        startupFinished: !rx.startupLatch,
        busPowered: rx.busPowered,
        firmwareFrozen: rx.firmwareFrozen,
        cutoffHold: rx.cutoffHold,
      },
      status: this.status,
      senderEnabled: this.senderEnabled,
      sessionTrusted: Boolean(this.sender.session?.trusted),
      armRequested: this.sender.armRequested,
      history: this.history,
    }
  }
}
